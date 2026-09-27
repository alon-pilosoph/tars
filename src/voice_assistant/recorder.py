import math
from collections import deque
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .audio import BLOCK_SECONDS, Microphone
from .config import Config, RecorderConfig
from .stt import DONE, LISTENING, MAYBE_DONE
from .turn import SmartTurn
from .vad import SileroVAD

# Speech must last this many consecutive blocks (160 ms) to count as "started talking".
START_BLOCKS = 2
# Audio kept from just before speech was detected, so the first syllable isn't clipped.
PREROLL_BLOCKS = 4
# Once someone is talking, a block counts as silence only this far below the threshold (Silero's own hysteresis),
# so a soft syllable doesn't end the sentence.
END_MARGIN = 0.15
# Below this, the end-of-turn model thinks there's more coming.
UNFINISHED = 0.5
# With a service deciding when the turn is over: silence that ends it anyway, in case the service went quiet
# (a little sooner than Flux's own timeout, stt.FLUX_TIMEOUT_MS).
BACKSTOP_S = 2.5


class UtteranceRecorder:
    """Records from when you start speaking until you stop, instead of a fixed number of seconds.

    `end_silence_s` of silence ends it. With `turn`, the end-of-turn model hears whether you sounded finished at that
    point, and if you didn't (a trailing "and, um"), it keeps listening up to `max_pause_s`: it only ever waits longer.
    """

    def __init__(self, cfg: RecorderConfig, vad: SileroVAD, turn: SmartTurn | None = None):
        self._cfg = cfg
        self._vad = vad
        self._turn = turn
        # How long ago you actually stopped talking when record() returned; used for honest latency numbers.
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
        turn_state: Callable[[], str] | None = None,
    ) -> bytes | None:
        """Return 16 kHz mono int16 PCM, or None if nobody spoke before the timeout.

        `preroll_blocks` and `tail_blocks` set how much audio to keep before speech starts and after it ends.
        `start_timeout_s` overrides the configured wait for speech to begin. `on_audio` gets the audio as it's
        recorded, from the moment speech starts (the preroll first), for streaming transcription. `on_pause` gets the
        audio so far once `answer_early_s` into a pause, and `on_resume` is called if they make a sound after it.
        With `turn_state` (a service that decides when the turn is over, like stt.FluxSession), the service ends the
        turn and says when to call on_pause and on_resume; the silence rule is only a backstop, in case it goes quiet.
        """
        self._vad.reset()
        preroll: deque[np.ndarray] = deque(maxlen=max(preroll_blocks, START_BLOCKS))
        streak = 0
        timeout_s = self._cfg.start_timeout_s if start_timeout_s is None else start_timeout_s
        for _ in range(int(timeout_s / BLOCK_SECONDS)):
            block = mic.read()
            preroll.append(block)
            streak = streak + 1 if self.is_speech(block) else 0
            if streak >= START_BLOCKS:
                break
        else:
            return None

        blocks = list(preroll)
        send = on_audio or (lambda pcm: None)
        send(np.concatenate(blocks).tobytes())
        cfg = self._cfg
        pause_blocks = math.ceil(cfg.end_silence_s / BLOCK_SECONDS)
        longest_blocks = math.ceil(cfg.max_pause_s / BLOCK_SECONDS) if self._turn else pause_blocks
        early_blocks = math.ceil(cfg.answer_early_s / BLOCK_SECONDS) if on_pause and cfg.answer_early_s else 0
        if turn_state:
            pause_blocks = longest_blocks = math.ceil(BACKSTOP_S / BLOCK_SECONDS)
            early_blocks = 0
        max_blocks = int(cfg.max_utterance_s / BLOCK_SECONDS)
        speaking, quiet = cfg.vad_threshold, cfg.vad_threshold - END_MARGIN
        # `silence` counts clearly quiet blocks since the last speech; soft ones in between neither end nor extend
        # the turn. An announced pause is undone by any sound that isn't clearly quiet.
        silence, announce_at, announced, stop_at = 0, early_blocks, False, pause_blocks
        last_voiced = len(blocks) - 1
        while silence < stop_at and len(blocks) < max_blocks:
            block = mic.read()
            blocks.append(block)
            send(block.tobytes())
            p = self._vad(block)
            if p >= quiet:
                last_voiced = len(blocks) - 1
                if announced and not turn_state:
                    on_resume and on_resume()
                    announced, announce_at = False, silence + early_blocks
            if p >= speaking:
                silence, announce_at, stop_at = 0, early_blocks, pause_blocks
            elif p < quiet:
                silence += 1
                if silence == announce_at and early_blocks and not announced:
                    on_pause(np.concatenate(blocks).tobytes())
                    announced = True
                if (
                    silence == pause_blocks
                    and self._turn
                    and not turn_state
                    and self._turn.finished(np.concatenate(blocks).tobytes()) < UNFINISHED
                ):
                    stop_at = longest_blocks
            if turn_state:
                state = turn_state()
                if state == DONE:
                    break
                if state == MAYBE_DONE and on_pause and not announced:
                    on_pause(np.concatenate(blocks).tobytes())
                    announced = True
                elif state == LISTENING and announced:
                    on_resume and on_resume()
                    announced = False

        after = len(blocks) - 1 - last_voiced
        self.trailing_silence_s = after * BLOCK_SECONDS
        # Only needed when asked for more tail than the end-of-turn wait already captured.
        for _ in range(tail_blocks - after):
            blocks.append(mic.read())
        # Drop the rest of the trailing silence; it's dead weight for transcription.
        return np.concatenate(blocks[: last_voiced + 1 + tail_blocks]).tobytes()


def make_recorder(cfg: Config, root: Path, turn_model: bool = True) -> UtteranceRecorder:
    """`turn_model`: whether to hear if people sound finished (conversations; not recording prompts)."""
    turn = None
    if turn_model and cfg.recorder.end_of_turn == "smart":
        turn = SmartTurn(root / cfg.recorder.turn_model)
    return UtteranceRecorder(cfg.recorder, SileroVAD(root / cfg.recorder.vad_model), turn)
