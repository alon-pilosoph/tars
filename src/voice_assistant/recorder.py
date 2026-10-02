"""Records one utterance, from the start of speech until the turn is over. The end is decided either by the
recorder's own silence rules (helped by the end-of-turn model) or by a streaming service that follows the turn (Flux).
"""

import math
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import numpy as np

from .audio import BLOCK_SECONDS, Microphone
from .config import Config, RecorderConfig
from .stt import TurnState
from .turn import SmartTurn
from .vad import SileroVAD

START_BLOCKS = 2  # consecutive speech blocks (160 ms) that count as starting to talk
PREROLL_BLOCKS = 4
# Once someone is talking, a block counts as silence only this far below the threshold (Silero's own hysteresis),
# so a soft syllable doesn't end the sentence.
END_MARGIN = 0.15
UNFINISHED = 0.5
# With a service deciding the turn: silence that ends it anyway in case the service goes quiet. A little sooner than
# Flux's own timeout, stt.FLUX_TIMEOUT_MS.
BACKSTOP_S = 2.5


class _Pauses(NamedTuple):
    """How a pause is handled, in blocks of clear silence."""

    end: int  # this much ends the turn...
    longest: int  # ...or this much, if the end-of-turn model hears more coming
    early: int  # announced (on_pause) this far in; 0 = never


def _pcm(blocks: list[np.ndarray]) -> bytes:
    return np.concatenate(blocks).tobytes()


class UtteranceRecorder:
    """`end_silence_s` of silence ends a recording. With `turn`, the end-of-turn model judges at that point whether
    the speaker sounded finished, and if not (a trailing "and, um") listening goes on up to `max_pause_s`. The model
    only ever extends the wait.
    """

    def __init__(self, cfg: RecorderConfig, vad: SileroVAD, turn: SmartTurn | None = None):
        self._cfg = cfg
        self._vad = vad
        self._turn = turn
        # How long ago the speaker actually stopped when record() returned, for honest latency numbers.
        self.trailing_silence_s = 0.0

    def is_speech(self, block: np.ndarray) -> bool:
        return self._vad(block) >= self._cfg.vad_threshold

    def record(
        self,
        mic: Microphone,
        preroll_blocks: int = PREROLL_BLOCKS,
        tail_blocks: int = 2,
        start_timeout_s: float | None = None,
        on_audio: Callable[[bytes], None] | None = None,
        on_pause: Callable[[bytes], None] | None = None,
        on_resume: Callable[[], None] | None = None,
        turn_state: Callable[[], TurnState] | None = None,
    ) -> bytes | None:
        """Returns 16 kHz mono int16 PCM, or None if nobody spoke before the timeout.

        `preroll_blocks` and `tail_blocks` set how much audio to keep before speech starts and after it ends.
        `start_timeout_s` overrides the configured wait for speech to begin. `on_audio` gets the audio as it's
        recorded, from the moment speech starts (preroll first), for streaming transcription. `on_pause` gets the
        audio so far once `answer_early_s` into a pause, and `on_resume` is called if the speaker makes a sound after.
        With `turn_state` (a service that decides the turn, like stt.FluxSession), the service ends the turn and says
        when to call on_pause and on_resume; the silence rule is only a backstop.
        """
        cfg = self._cfg
        timeout_s = cfg.start_timeout_s if start_timeout_s is None else start_timeout_s
        blocks = self._wait_for_speech(mic, max(preroll_blocks, START_BLOCKS), timeout_s)
        if blocks is None:
            return None
        send = on_audio or (lambda pcm: None)
        resume = on_resume or (lambda: None)
        send(_pcm(blocks))

        end = math.ceil(cfg.end_silence_s / BLOCK_SECONDS)
        own = _Pauses(
            end,
            math.ceil(cfg.max_pause_s / BLOCK_SECONDS) if self._turn else end,
            math.ceil(cfg.answer_early_s / BLOCK_SECONDS) if on_pause and cfg.answer_early_s else 0,
        )
        backstop = math.ceil(BACKSTOP_S / BLOCK_SECONDS)
        service = turn_state  # None once the service fails
        pauses = _Pauses(backstop, backstop, 0) if service else own
        max_blocks = int(cfg.max_utterance_s / BLOCK_SECONDS)
        speaking, quiet = cfg.vad_threshold, cfg.vad_threshold - END_MARGIN
        # `silence` counts clearly quiet blocks since the last speech; soft ones in between neither end nor extend
        # the turn. An announced pause is undone by any sound that isn't clearly quiet.
        silence, announce_at, announced, stop_at = 0, pauses.early, False, pauses.end
        last_voiced, service_ended = len(blocks) - 1, False
        while silence < stop_at and len(blocks) < max_blocks:
            block = mic.read()
            blocks.append(block)
            send(block.tobytes())
            p = self._vad(block)
            if p >= quiet:
                last_voiced = len(blocks) - 1
                if announced and not service:
                    resume()
                    announced, announce_at = False, silence + pauses.early
            if p >= speaking:
                silence, announce_at, stop_at = 0, pauses.early, pauses.end
            elif p < quiet:
                silence += 1
                if silence == announce_at and pauses.early and not announced:
                    on_pause(_pcm(blocks))
                    announced = True
                if (
                    silence == pauses.end
                    and pauses.longest > pauses.end
                    and self._turn.p_finished(_pcm(blocks)) < UNFINISHED
                ):
                    stop_at = pauses.longest
            if not service:
                continue
            state = service()
            if state == TurnState.FAILED:
                service, pauses = None, own
                stop_at, announce_at = pauses.end, silence + pauses.early
            elif state == TurnState.DONE:
                service_ended = True
                break
            elif state == TurnState.MAYBE_DONE and on_pause and not announced:
                on_pause(_pcm(blocks))
                announced = True
            elif state == TurnState.LISTENING and announced:
                resume()
                announced = False

        after = len(blocks) - 1 - last_voiced
        self.trailing_silence_s = after * BLOCK_SECONDS
        # Only when asked for more tail than the silence wait captured, and never once the service has called the
        # turn: more audio would only delay the answer.
        for _ in range(0 if service_ended else tail_blocks - after):
            blocks.append(mic.read())
        return _pcm(blocks[: last_voiced + 1 + tail_blocks])

    def _wait_for_speech(self, mic: Microphone, keep: int, timeout_s: float) -> list[np.ndarray] | None:
        self._vad.reset()
        preroll: deque[np.ndarray] = deque(maxlen=keep)
        streak = 0
        for _ in range(int(timeout_s / BLOCK_SECONDS)):
            block = mic.read()
            preroll.append(block)
            streak = streak + 1 if self.is_speech(block) else 0
            if streak >= START_BLOCKS:
                return list(preroll)
        return None


def make_recorder(cfg: Config, root: Path, turn_model: bool = True) -> UtteranceRecorder:
    """`turn_model`: use the end-of-turn model (for conversations, not for recording prompts)."""
    turn = None
    if turn_model and cfg.recorder.end_of_turn == "smart":
        turn = SmartTurn(root / cfg.recorder.turn_model)
    return UtteranceRecorder(cfg.recorder, SileroVAD(root / cfg.recorder.vad_model), turn)
