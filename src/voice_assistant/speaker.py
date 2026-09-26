"""Who is talking: compares a voiceprint (speaker embedding) of each request to enrolled people.

The embedding model is WeSpeaker's ResNet34-LM, the same one pyannote.audio uses by default
(CC-BY-4.0, https://github.com/wenet-e2e/wespeaker). It runs on onnxruntime, so no PyTorch.
"""

import os
import tempfile
from pathlib import Path

import kaldi_native_fbank as knf
import numpy as np

from .audio import SAMPLE_RATE
from .models import fetch

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


class SpeakerID:
    def __init__(self, model_path: Path, voiceprints_path: Path, threshold: float):
        import onnxruntime

        model_path = fetch(model_path, MODEL_URL, "the speaker model")
        self._session = onnxruntime.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._voiceprints_path = voiceprints_path
        self.threshold = threshold
        self.voiceprints: dict[str, np.ndarray] = {}
        self._from_clusters: set[str] = set()  # the voiceprints the web UI made from named voices
        self._loaded_mtime = None
        self._reload()

    def _reload(self) -> None:
        """Pick up voiceprints saved by another process (the web UI enrolls people by naming their voice).
        A file that can't be read (caught mid-write by an older version, or damaged) keeps the ones we have."""
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
        """Written to a temporary file and swapped in, so a reader never sees half a file."""
        path = self._voiceprints_path
        path.parent.mkdir(parents=True, exist_ok=True)
        names = list(self.voiceprints)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".npz")
        try:
            with os.fdopen(fd, "wb") as f:
                np.savez(
                    f,
                    names=np.array(names, dtype=str),
                    vectors=np.array([self.voiceprints[n] for n in names]),
                    from_clusters=np.array(sorted(self._from_clusters), dtype=str),
                )
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._loaded_mtime = path.stat().st_mtime_ns

    def embed(self, pcm: np.ndarray) -> np.ndarray:
        feats = _fbank(pcm)[None].astype(np.float32)
        emb = self._session.run(None, {"feats": feats})[0][0]
        return emb / np.linalg.norm(emb)

    def voiceprint(self, clips: list[np.ndarray]) -> np.ndarray:
        """The normalized average of the clips' embeddings."""
        mean = np.mean([self.embed(c) for c in clips], axis=0)
        return mean / np.linalg.norm(mean)

    def enroll(self, name: str, clips: list[np.ndarray]) -> None:
        """From recorded sentences (--enroll). Kept until a voice with the same name is named in the web UI."""
        self._reload()
        self.voiceprints[name] = self.voiceprint(clips)
        self._from_clusters.discard(name)
        self._save()

    def set_cluster_voiceprints(self, voiceprints: dict[str, np.ndarray]) -> None:
        """Replace every voiceprint the web UI made with these (one per named person). A name that's gone (renamed,
        merged away, marked not a person) stops being recognized; people enrolled with --enroll stay."""
        self._reload()
        for name in self._from_clusters - set(voiceprints):
            self.voiceprints.pop(name, None)
        self.voiceprints.update(voiceprints)
        self._from_clusters = set(voiceprints)
        self._save()

    def _match(self, pcm: bytes) -> tuple[str | None, float | None, np.ndarray | None]:
        """(best-matching person, their similarity, the embedding); (None, None, None) for a clip too short."""
        self._reload()
        audio = np.frombuffer(pcm, dtype=np.int16)
        if len(audio) < MIN_SPEECH_S * SAMPLE_RATE:
            return None, None, None
        emb = self.embed(audio)
        scores = {name: float(emb @ vp) for name, vp in self.voiceprints.items()}
        best = max(scores, key=scores.get, default=None)
        return best, (scores[best] if best else None), emb

    def describe(self, pcm: bytes) -> tuple[str | None, float | None, np.ndarray | None]:
        """(best-matching person or None, their similarity, the voice embedding), for identifying and clustering."""
        best, score, emb = self._match(pcm)
        return (best if score is not None and score >= self.threshold else None), score, emb

    def identify(self, pcm: bytes, threshold: float | None = None) -> str | None:
        """The best-matching enrolled person, or None if nobody matches well enough (or the clip is too short)."""
        self._reload()
        if not self.voiceprints:
            return None
        best, score, _ = self._match(pcm)
        return best if score is not None and score >= (self.threshold if threshold is None else threshold) else None


def _load(path: Path) -> tuple[dict[str, np.ndarray], set[str]]:
    with np.load(path) as saved:
        if "names" in saved.files and "vectors" in saved.files:
            names = [str(n) for n in saved["names"]]
            return dict(zip(names, saved["vectors"], strict=True)), {str(n) for n in saved["from_clusters"]}
        # The first format: one array per person, named after them.
        return {name: saved[name] for name in saved.files}, set()
