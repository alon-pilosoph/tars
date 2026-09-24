"""Who is talking: compares a voiceprint (speaker embedding) of each request to enrolled people.

The embedding model is WeSpeaker's ResNet34-LM, the same one pyannote.audio uses by default
(CC-BY-4.0, https://github.com/wenet-e2e/wespeaker). It runs on onnxruntime, so no PyTorch.
"""

import urllib.request
import wave
from pathlib import Path

import kaldi_native_fbank as knf
import numpy as np

from .audio import SAMPLE_RATE

MODEL_URL = "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/main/voxceleb_resnet34_LM.onnx"
# Shorter clips don't carry enough voice to identify anyone reliably.
MIN_SPEECH_S = 0.8


def _fbank(pcm: np.ndarray) -> np.ndarray:
    """80-bin Kaldi filterbank with mean normalization: the features the model was trained on."""
    opts = knf.FbankOptions()
    opts.frame_opts.dither = 0.0
    opts.frame_opts.samp_freq = SAMPLE_RATE
    opts.mel_opts.num_bins = 80
    fbank = knf.OnlineFbank(opts)
    fbank.accept_waveform(SAMPLE_RATE, pcm.astype(np.float32).tolist())  # Kaldi expects int16-scaled samples
    fbank.input_finished()
    feats = np.stack([fbank.get_frame(i) for i in range(fbank.num_frames_ready)])
    return feats - feats.mean(axis=0)


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as f:
        return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)


class SpeakerID:
    def __init__(self, model_path: Path, voiceprints_path: Path, threshold: float):
        import onnxruntime

        if not model_path.exists():
            print(f"Downloading speaker model to {model_path}...")
            model_path.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(MODEL_URL, model_path)
        self._session = onnxruntime.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._voiceprints_path = voiceprints_path
        self.threshold = threshold
        self.voiceprints: dict[str, np.ndarray] = {}
        if voiceprints_path.exists():
            with np.load(voiceprints_path) as saved:
                self.voiceprints = {name: saved[name] for name in saved.files}

    def embed(self, pcm: np.ndarray) -> np.ndarray:
        feats = _fbank(pcm)[None].astype(np.float32)
        emb = self._session.run(None, {"feats": feats})[0][0]
        return emb / np.linalg.norm(emb)

    def enroll(self, name: str, clips: list[np.ndarray]) -> None:
        """A voiceprint is the normalized average of the clips' embeddings."""
        mean = np.mean([self.embed(c) for c in clips], axis=0)
        self.voiceprints[name] = mean / np.linalg.norm(mean)
        self._voiceprints_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(self._voiceprints_path, **self.voiceprints)

    def scores(self, pcm: np.ndarray) -> dict[str, float]:
        emb = self.embed(pcm)
        return {name: float(emb @ vp) for name, vp in self.voiceprints.items()}

    def identify(self, pcm: bytes) -> str | None:
        """The best-matching enrolled person, or None if nobody matches well enough (or the clip is too short)."""
        audio = np.frombuffer(pcm, dtype=np.int16)
        if not self.voiceprints or len(audio) < MIN_SPEECH_S * SAMPLE_RATE:
            return None
        scores = self.scores(audio)
        best = max(scores, key=scores.get)
        return best if scores[best] >= self.threshold else None
