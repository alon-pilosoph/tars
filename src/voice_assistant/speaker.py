"""Speaker ID: compares each request's speaker embedding to enrolled people's voiceprints.

The model is WeSpeaker's ResNet34-LM, pyannote.audio's default (CC-BY-4.0, https://github.com/wenet-e2e/wespeaker),
run on onnxruntime to avoid PyTorch.
"""

import io
import threading
from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE
from .events import person_key
from .files import atomic_write
from .models import fetch

MODEL_URL = (
    "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/"
    "f0c48c298fd835726c27956a5d617bad7115627e/voxceleb_resnet34_LM.onnx"
)
MODEL_SHA256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"  # 26,530,309 bytes
MIN_SPEECH_S = 0.8


def _fbank(pcm: np.ndarray) -> np.ndarray:
    """80-bin Kaldi filterbank with mean normalization: the features the model was trained on."""
    import kaldi_native_fbank as knf  # with the voice extra, loaded only when a voice is heard

    opts = knf.FbankOptions()
    opts.frame_opts.dither = 0.0
    opts.frame_opts.samp_freq = SAMPLE_RATE
    opts.mel_opts.num_bins = 80
    fbank = knf.OnlineFbank(opts)
    fbank.accept_waveform(SAMPLE_RATE, pcm.astype(np.float32).tolist())  # Kaldi expects int16-scaled samples
    fbank.input_finished()
    feats = np.stack([fbank.get_frame(i) for i in range(fbank.num_frames_ready)])
    return feats - feats.mean(axis=0)


ANY_MIC = ""  # a voiceprint made from every mic together: used wherever someone has none of that mic's own


class SpeakerID:
    """Whose voice a request is, by its speaker embedding against each person's voiceprints. A voiceprint is a
    person's on one mic (a room's: its body's microphone, or the Pi's own), or on ANY_MIC (all of theirs together, or
    one enrolled before voiceprints had mics): a voice doesn't sound the same through every microphone, so a request
    is held to the voiceprint of the mic that heard it, and to their ANY_MIC one, or their others, only without."""

    def __init__(self, model_path: Path, voiceprints_path: Path, threshold: float):
        import onnxruntime

        model_path = fetch(model_path, MODEL_URL, "the speaker model", MODEL_SHA256)
        # All cores, unlike the per-block models in models.onnx_session: this runs once per request, while TARS waits.
        self._session = onnxruntime.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._voiceprints_path = voiceprints_path
        self.threshold = threshold
        self.prints: dict[tuple[str, str], np.ndarray] = {}  # (person, mic): the voiceprint
        self._from_clusters: set[tuple[str, str]] = set()  # voiceprints the web UI made from named voices
        self._loaded_mtime = None
        # Rooms identify speakers at once (one assistant per body): a reload, an enrollment or a naming in the web UI
        # swaps in whole new voiceprints under this lock, never changing the ones a match is reading.
        self._lock = threading.Lock()
        self._reload()

    @property
    def voiceprints(self) -> dict[str, list[str]]:
        """Everyone with a voiceprint, and the mics they have one for (ANY_MIC as "")."""
        out: dict[str, list[str]] = {}
        for person, mic in self.prints:
            out.setdefault(person, []).append(mic)
        return out

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
        with self._lock:
            if mtime == self._loaded_mtime:
                return
            try:
                self.prints, self._from_clusters = _load(self._voiceprints_path)
            except Exception as e:  # noqa: BLE001 - a half-written or foreign file
                print(f"(couldn't read the voiceprints in {self._voiceprints_path}, keeping the old ones: {e!r})")
            self._loaded_mtime = mtime

    def _save(self) -> None:
        keys = list(self.prints)
        made = sorted(self._from_clusters)
        data = io.BytesIO()
        np.savez(
            data,
            names=np.array([p for p, _ in keys], dtype=str),
            mics=np.array([m for _, m in keys], dtype=str),
            vectors=np.array([self.prints[k] for k in keys]),
            from_clusters=np.array([p for p, _ in made], dtype=str),
            from_cluster_mics=np.array([m for _, m in made], dtype=str),
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

    def enroll(self, name: str, clips: list[np.ndarray], mic: str = ANY_MIC) -> None:
        """From recorded sentences (--enroll, a voice session) on `mic`. Replaced if a voice with the same name is
        named in the web UI with enough requests on that mic."""
        self._reload()
        key = (person_key(name), mic)
        print_ = self.voiceprint(clips)
        with self._lock:
            self.prints = {**self.prints, key: print_}
            self._from_clusters = self._from_clusters - {key}
            self._save()

    def set_cluster_voiceprints(self, voiceprints: dict[tuple[str, str], np.ndarray]) -> None:
        """Replaces every web-UI voiceprint with these, keyed by (person, as events.person_key has them; mic). One
        that's gone (renamed, merged away, marked not a person) stops being recognized; enrolled ones stay."""
        self._reload()
        with self._lock:
            kept = {k: vp for k, vp in self.prints.items() if k not in self._from_clusters - set(voiceprints)}
            self.prints = {**kept, **voiceprints}
            self._from_clusters = set(voiceprints)
            self._save()

    def _match(self, pcm: bytes, mic: str) -> tuple[str | None, float | None, np.ndarray | None]:
        self._reload()
        audio = np.frombuffer(pcm, dtype=np.int16)
        if len(audio) < MIN_SPEECH_S * SAMPLE_RATE:
            return None, None, None
        emb = self.embed(audio)
        scores = {person: float(emb @ vp) for person, vp in _for_mic(self.prints, mic).items()}
        best = max(scores, key=scores.get, default=None)
        return best, (scores[best] if best else None), emb

    def describe(self, pcm: bytes, mic: str = ANY_MIC) -> tuple[str | None, float | None, np.ndarray | None]:
        """(best match if above the threshold, else None; its similarity; the embedding), heard on `mic`."""
        best, score, emb = self._match(pcm, mic)
        return (best if score is not None and score >= self.threshold else None), score, emb

    def identify(self, pcm: bytes, threshold: float | None = None, mic: str = ANY_MIC) -> str | None:
        self._reload()
        if not self.prints:
            return None
        best, score, _ = self._match(pcm, mic)
        return best if score is not None and score >= (self.threshold if threshold is None else threshold) else None


def _for_mic(prints: dict[tuple[str, str], np.ndarray], mic: str) -> dict[str, np.ndarray]:
    """Each person's voiceprint for a request heard on `mic`: that mic's own, else their ANY_MIC one, else the
    average of their others (another mic's is better than none)."""
    people: dict[str, dict[str, np.ndarray]] = {}
    for (person, on), vp in prints.items():  # whole: a reload swaps in new ones, never changes these
        people.setdefault(person, {})[on] = vp
    out = {}
    for person, mics in people.items():
        if mic in mics:
            out[person] = mics[mic]
        elif ANY_MIC in mics:
            out[person] = mics[ANY_MIC]
        else:
            mean = np.mean(list(mics.values()), axis=0)
            out[person] = mean / np.linalg.norm(mean)
    return out


def _load(path: Path) -> tuple[dict[tuple[str, str], np.ndarray], set[tuple[str, str]]]:
    with np.load(path) as saved:
        if "names" in saved.files and "vectors" in saved.files:
            names = [person_key(str(n)) for n in saved["names"]]
            mics = [str(m) for m in saved["mics"]] if "mics" in saved.files else [ANY_MIC] * len(names)
            made = [person_key(str(n)) for n in saved["from_clusters"]]
            made_mics = (
                [str(m) for m in saved["from_cluster_mics"]]
                if "from_cluster_mics" in saved.files
                else [ANY_MIC] * len(made)
            )
            prints = dict(zip(zip(names, mics, strict=True), saved["vectors"], strict=True))
            return prints, set(zip(made, made_mics, strict=True))
        # Legacy format: one array per person, named after them.
        return {(person_key(name), ANY_MIC): saved[name] for name in saved.files}, set()


def voiceprint_names(path: Path) -> list[str]:
    """Everyone with a voiceprint, read from the file alone, without loading the model: for a process that only
    needs to know whose voice TARS knows (the Telegram chat, for messages that wait until someone's back)."""
    try:
        return sorted({person for person, _ in _load(path)[0]})
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001 - a half-written or foreign file: as if nobody's enrolled, this once
        print(f"(couldn't read the voiceprints in {path}: {e!r})")
        return []
