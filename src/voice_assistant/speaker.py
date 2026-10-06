"""Speaker ID: compares each request's speaker embedding to enrolled people's voiceprints.

The model is WeSpeaker's ResNet34-LM, pyannote.audio's default (CC-BY-4.0, https://github.com/wenet-e2e/wespeaker),
run on onnxruntime to avoid PyTorch.
"""

import io
from pathlib import Path

import kaldi_native_fbank as knf
import numpy as np

from .audio import SAMPLE_RATE
from .events import person_key
from .files import atomic_write
from .models import fetch

MODEL_URL = "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/main/voxceleb_resnet34_LM.onnx"
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


class SpeakerID:
    def __init__(self, model_path: Path, voiceprints_path: Path, threshold: float):
        import onnxruntime

        model_path = fetch(model_path, MODEL_URL, "the speaker model")
        # All cores, unlike the per-block models in models.onnx_session: this runs once per request, while TARS waits.
        self._session = onnxruntime.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._voiceprints_path = voiceprints_path
        self.threshold = threshold
        self.voiceprints: dict[str, np.ndarray] = {}
        self._from_clusters: set[str] = set()  # voiceprints the web UI made from named voices
        self._loaded_mtime = None
        self._reload()

    def names(self) -> list[str]:
        """Everyone with a voiceprint, including any another process just saved."""
        self._reload()
        return list(self.voiceprints)

    def _reload(self) -> None:
        """Picks up voiceprints saved by another process (the web UI enrolls people by naming their voice).
        An unreadable file keeps the current ones."""
        try:
            mtime = self._voiceprints_path.stat().st_mtime_ns
        except FileNotFoundError:
            return
        if mtime == self._loaded_mtime:
            return
        try:
            self.voiceprints, self._from_clusters = _load(self._voiceprints_path)
        except Exception as e:  # noqa: BLE001 - a half-written or foreign file
            print(f"(couldn't read the voiceprints in {self._voiceprints_path}, keeping the old ones: {e!r})")
        self._loaded_mtime = mtime

    def _save(self) -> None:
        names = list(self.voiceprints)
        data = io.BytesIO()
        np.savez(
            data,
            names=np.array(names, dtype=str),
            vectors=np.array([self.voiceprints[n] for n in names]),
            from_clusters=np.array(sorted(self._from_clusters), dtype=str),
        )
        atomic_write(self._voiceprints_path, data.getvalue())
        self._loaded_mtime = self._voiceprints_path.stat().st_mtime_ns

    def embed(self, pcm: np.ndarray) -> np.ndarray:
        feats = _fbank(pcm)[None].astype(np.float32)
        emb = self._session.run(None, {"feats": feats})[0][0]
        return emb / np.linalg.norm(emb)

    def voiceprint(self, clips: list[np.ndarray]) -> np.ndarray:
        mean = np.mean([self.embed(c) for c in clips], axis=0)
        return mean / np.linalg.norm(mean)

    def enroll(self, name: str, clips: list[np.ndarray]) -> None:
        """From recorded sentences (--enroll). Replaced if a voice with the same name is named in the web UI."""
        self._reload()
        key = person_key(name)
        self.voiceprints[key] = self.voiceprint(clips)
        self._from_clusters.discard(key)
        self._save()

    def set_cluster_voiceprints(self, voiceprints: dict[str, np.ndarray]) -> None:
        """Replaces every web-UI voiceprint with these (keyed by events.person_key). A name that's gone (renamed,
        merged away, marked not a person) stops being recognized; people enrolled with --enroll stay."""
        self._reload()
        for name in self._from_clusters - set(voiceprints):
            self.voiceprints.pop(name, None)
        self.voiceprints.update(voiceprints)
        self._from_clusters = set(voiceprints)
        self._save()

    def _match(self, pcm: bytes) -> tuple[str | None, float | None, np.ndarray | None]:
        self._reload()
        audio = np.frombuffer(pcm, dtype=np.int16)
        if len(audio) < MIN_SPEECH_S * SAMPLE_RATE:
            return None, None, None
        emb = self.embed(audio)
        scores = {name: float(emb @ vp) for name, vp in self.voiceprints.items()}
        best = max(scores, key=scores.get, default=None)
        return best, (scores[best] if best else None), emb

    def describe(self, pcm: bytes) -> tuple[str | None, float | None, np.ndarray | None]:
        """(best match if above the threshold, else None; its similarity; the embedding)."""
        best, score, emb = self._match(pcm)
        return (best if score is not None and score >= self.threshold else None), score, emb

    def identify(self, pcm: bytes, threshold: float | None = None) -> str | None:
        self._reload()
        if not self.voiceprints:
            return None
        best, score, _ = self._match(pcm)
        return best if score is not None and score >= (self.threshold if threshold is None else threshold) else None


def _load(path: Path) -> tuple[dict[str, np.ndarray], set[str]]:
    with np.load(path) as saved:
        if "names" in saved.files and "vectors" in saved.files:
            names = [person_key(str(n)) for n in saved["names"]]
            return dict(zip(names, saved["vectors"], strict=True)), {person_key(str(n)) for n in saved["from_clusters"]}
        # Legacy format: one array per person, named after them.
        return {person_key(name): saved[name] for name in saved.files}, set()
