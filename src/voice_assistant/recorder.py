import math
from collections import deque
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .audio import BLOCK_SECONDS, Microphone
from .config import Config, RecorderConfig
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
    ) -> bytes | None:
        """Return 16 kHz mono int16 PCM, or None if nobody spoke before the timeout.

        `preroll_blocks` and `tail_blocks` set how much audio to keep before speech starts and after it ends.
        `start_timeout_s` overrides the configured wait for speech to begin. `on_audio` gets the audio as it's
        recorded, from the moment speech starts (the preroll first), for streaming transcription.
        """
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
        pause = math.ceil(self._cfg.end_silence_s / BLOCK_SECONDS)
        silent_blocks_to_stop = pause
        max_blocks = int(self._cfg.max_utterance_s / BLOCK_SECONDS)
        speaking, quiet = self._cfg.vad_threshold, self._cfg.vad_threshold - END_MARGIN
        silence = 0
        while silence < silent_blocks_to_stop and len(blocks) < max_blocks:
            block = mic.read()
            blocks.append(block)
            send(block.tobytes())
            p = self._vad(block)
            if p >= speaking:
                silence = 0
            elif p < quiet:
                silence += 1
                if (
                    silence == pause
                    and self._turn
                    and self._turn.finished(np.concatenate(blocks).tobytes()) < UNFINISHED
                ):
                    silent_blocks_to_stop = math.ceil(self._cfg.max_pause_s / BLOCK_SECONDS)
            if silence < pause:
                silent_blocks_to_stop = pause

        self.trailing_silence_s = silence * BLOCK_SECONDS
        # Only needed when asked for more tail than the end-of-speech wait already captured.
        for _ in range(tail_blocks - silence):
            blocks.append(mic.read())
            silence += 1
        # Drop the rest of the trailing silence; it's dead weight for transcription.
        return np.concatenate(blocks[: len(blocks) - silence + tail_blocks]).tobytes()


def make_recorder(cfg: Config, root: Path) -> UtteranceRecorder:
    turn = None
    if cfg.recorder.end_of_turn == "smart":
        turn = SmartTurn(root / cfg.recorder.turn_model)
    elif cfg.recorder.end_of_turn != "silence":
        raise SystemExit(f"[recorder] end_of_turn must be silence or smart, not {cfg.recorder.end_of_turn!r}")
    return UtteranceRecorder(cfg.recorder, SileroVAD(root / cfg.recorder.vad_model), turn)
