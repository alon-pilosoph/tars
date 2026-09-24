from collections import deque

import numpy as np
import webrtcvad

from .audio import BLOCK_SAMPLES, BLOCK_SECONDS, SAMPLE_RATE, Microphone
from .config import RecorderConfig

# webrtcvad only accepts 10/20/30 ms frames; four 20 ms frames fit exactly in one 80 ms block.
VAD_FRAME_SAMPLES = SAMPLE_RATE // 50
VAD_FRAMES_PER_BLOCK = BLOCK_SAMPLES // VAD_FRAME_SAMPLES
# Speech must last this many consecutive blocks (160 ms) to count as "started talking".
START_BLOCKS = 2
# Audio kept from just before speech was detected, so the first syllable isn't clipped.
PREROLL_BLOCKS = 4


class UtteranceRecorder:
    """Records from when you start speaking until you stop, instead of a fixed number of seconds."""

    def __init__(self, cfg: RecorderConfig):
        self._cfg = cfg
        self._vad = webrtcvad.Vad(cfg.vad_aggressiveness)
        # How long ago you actually stopped talking when record() returned; used for honest latency numbers.
        self.trailing_silence_s = 0.0

    def is_speech(self, block: np.ndarray) -> bool:
        raw = block.tobytes()
        frame_bytes = VAD_FRAME_SAMPLES * 2
        voiced = sum(
            self._vad.is_speech(raw[i : i + frame_bytes], SAMPLE_RATE)
            for i in range(0, VAD_FRAMES_PER_BLOCK * frame_bytes, frame_bytes)
        )
        return voiced >= VAD_FRAMES_PER_BLOCK // 2

    def record(
        self,
        mic: Microphone,
        preroll_blocks: int = PREROLL_BLOCKS,
        tail_blocks: int = 2,
        start_timeout_s: float | None = None,
    ) -> bytes | None:
        """Return 16 kHz mono int16 PCM, or None if nobody spoke before the timeout.

        `preroll_blocks` and `tail_blocks` set how much audio to keep before speech starts and after it ends.
        `start_timeout_s` overrides the configured wait for speech to begin.
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
        silent_blocks_to_stop = int(self._cfg.end_silence_s / BLOCK_SECONDS)
        max_blocks = int(self._cfg.max_utterance_s / BLOCK_SECONDS)
        silence = 0
        while silence < silent_blocks_to_stop and len(blocks) < max_blocks:
            block = mic.read()
            blocks.append(block)
            silence = 0 if self.is_speech(block) else silence + 1

        self.trailing_silence_s = silence * BLOCK_SECONDS
        # Only needed when asked for more tail than the end-of-speech wait already captured.
        for _ in range(tail_blocks - silence):
            blocks.append(mic.read())
            silence += 1
        # Drop the rest of the trailing silence; it's dead weight for transcription.
        return np.concatenate(blocks[: len(blocks) - silence + tail_blocks]).tobytes()
