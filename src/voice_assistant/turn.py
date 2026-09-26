"""Has the speaker finished, or only paused? A small local model listens to how the last words sounded.

A fixed silence wait has to be long enough for someone collecting their thoughts mid-sentence, so every reply waits
that long. Pipecat's Smart Turn v3 (BSD-2, a Whisper Tiny encoder with a small head, 8 MB) hears the difference
between a finished sentence and a trailing "and, um": after a short silence TARS asks it, and only keeps waiting
when the model thinks there's more coming.
"""

import urllib.request
from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE

MODEL_URL = "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx"
WINDOW_S = 8  # the model hears the last 8 seconds, zero-padded in front
# Whisper's log-mel front end, which the model was trained on (what transformers' WhisperFeatureExtractor computes).
N_FFT, HOP, N_MELS = 400, 160, 80


def _mel_filters() -> np.ndarray:
    """Slaney-style mel filterbank (librosa's default, which Whisper uses): (N_MELS, N_FFT // 2 + 1)."""

    def hz_to_mel(f):
        f = np.asarray(f, dtype=np.float64)
        mel = f / (200.0 / 3)
        log_part = f >= 1000.0
        return np.where(log_part, 15.0 + np.log(np.maximum(f, 1e-10) / 1000.0) / (np.log(6.4) / 27.0), mel)

    def mel_to_hz(m):
        m = np.asarray(m, dtype=np.float64)
        f = m * (200.0 / 3)
        return np.where(m >= 15.0, 1000.0 * np.exp((np.log(6.4) / 27.0) * (m - 15.0)), f)

    fft_freqs = np.linspace(0, SAMPLE_RATE / 2, N_FFT // 2 + 1)
    mel_f = mel_to_hz(np.linspace(hz_to_mel(0.0), hz_to_mel(SAMPLE_RATE / 2), N_MELS + 2))
    fdiff = np.diff(mel_f)
    ramps = mel_f[:, None] - fft_freqs[None, :]
    lower, upper = -ramps[:-2] / fdiff[:-1, None], ramps[2:] / fdiff[1:, None]
    weights = np.maximum(0, np.minimum(lower, upper))
    return weights * (2.0 / (mel_f[2 : N_MELS + 2] - mel_f[:N_MELS]))[:, None]


_MEL = _mel_filters()
_WINDOW = np.hanning(N_FFT + 1)[:-1]  # periodic Hann, as torch.hann_window


def features(audio: np.ndarray) -> np.ndarray:
    """(1, 80, 800) float32 log-mel features of the last 8 s of 16 kHz audio."""
    n = WINDOW_S * SAMPLE_RATE
    x = audio.astype(np.float32)  # the scale doesn't matter: it's normalized below
    x = x[-n:] if len(x) > n else np.pad(x, (n - len(x), 0))
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    padded = np.pad(x, N_FFT // 2, mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT)[::HOP]
    power = np.abs(np.fft.rfft(frames * _WINDOW, axis=1)) ** 2
    mel = _MEL @ power[:-1].T  # Whisper drops the last frame
    log = np.log10(np.maximum(mel, 1e-10))
    log = np.maximum(log, log.max() - 8.0)
    return ((log + 4.0) / 4.0)[None].astype(np.float32)


class SmartTurn:
    def __init__(self, model_path: Path):
        import onnxruntime as ort

        if not model_path.exists():
            print(f"Downloading the end-of-turn model to {model_path}...")
            model_path.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(MODEL_URL, model_path)
        options = ort.SessionOptions()
        options.inter_op_num_threads = options.intra_op_num_threads = 1
        self._session = ort.InferenceSession(str(model_path), sess_options=options)

    def finished(self, pcm: bytes) -> float:
        """How likely it is that the speaker has finished (0-1), from 16 kHz mono int16 audio of what they said."""
        audio = np.frombuffer(pcm, dtype=np.int16)
        return float(self._session.run(None, {"input_features": features(audio)})[0].ravel()[0])
