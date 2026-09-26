"""Is someone talking? Silero VAD (MIT, 2 MB): a small neural speech detector, the usual one in voice assistants.

It holds up in room noise and ignores tones like TARS's own chime, where webrtcvad needed workarounds for both.
"""

from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE
from .models import onnx_session

MODEL_URL = "https://github.com/snakers4/silero-vad/raw/v6.2.3/src/silero_vad/data/silero_vad.onnx"
WINDOW, CONTEXT = 512, 64  # the model's step at 16 kHz, and how much of the previous step it sees again


class SileroVAD:
    def __init__(self, model_path: Path):
        self._session = onnx_session(model_path, MODEL_URL, "the speech detector")
        self.reset()

    def reset(self) -> None:
        """Forget what came before: each recording is judged on its own."""
        self._state = np.zeros((2, 1, 128), np.float32)
        self._context = np.zeros(CONTEXT, np.float32)
        self._pending = np.zeros(0, np.float32)
        self._last = 0.0

    def __call__(self, block: np.ndarray) -> float:
        """How likely it is that this block of 16 kHz int16 audio is speech (0-1). Feed the stream in order."""
        self._pending = np.concatenate([self._pending, block.astype(np.float32) / 32768])
        probs = []
        while len(self._pending) >= WINDOW:
            x = np.concatenate([self._context, self._pending[:WINDOW]])[None]
            self._pending = self._pending[WINDOW:]
            out, self._state = self._session.run(
                None, {"input": x, "state": self._state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)}
            )
            self._context = x[0, -CONTEXT:]
            probs.append(float(out.ravel()[0]))
        self._last = max(probs, default=self._last)
        return self._last
