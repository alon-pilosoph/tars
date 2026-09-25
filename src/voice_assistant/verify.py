"""Second check on every wake: a small offline speech recognizer confirms the words.

The wake-word model is fast but can't reliably tell "hey TARS" from "hey cars" (one consonant apart).
Vosk, restricted to a short list of phrases, can: it has to pick which one it heard, and it knows words.
"""

import json
import urllib.request
import zipfile
from collections import deque
from pathlib import Path

import numpy as np

from .audio import BLOCK_SECONDS, Microphone

MODEL_NAME = "vosk-model-small-en-us-0.15"  # 40 MB download, runs fine on a Raspberry Pi
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
# Audio handed to the recognizer when the model fires. Some wake models fire up to ~1 s after the phrase ends,
# so too short a window loses the "hey"; too long lets in more lookalikes. Measured best: 2.5 s (see config.toml).
WINDOW_S = 2.5

# Per wake phrase: what else it could be (so the recognizer has somewhere else to put a lookalike),
# and what counts as a match. "darts" is how "TARS" often comes out for a soft t; nobody says "hey darts".
PHRASES = {
    "hey tars": {
        "lookalikes": [
            "hey cars",
            "hey bars",
            "hey mars",
            "hey stars",
            "hey lars",
            "hey parts",
            "hey guitars",
            "hey guards",
            "hey tara",
            "hey jarvis",
            "hey there",
            "hey star",
            "hey tarot",
            "hey car",
            "hey bar",
            "hey tar",
            "hey",
            "tars stop",
            "stop",
        ],
        "accept": ["hey tars", "hey darts"],
        # "hey" + one of these is close to the name but nobody calls it out: worth a "Did you call me?".
        # Plausible phrases ("hey there", "hey Jarvis", "hey Tara", a bare "stars") are ignored instead.
        "ask_after_hey": [
            "tars",
            "darts",
            "cars",
            "bars",
            "mars",
            "stars",
            "parts",
            "guitars",
            "guards",
            "tarot",
            "car",
            "bar",
            "tar",
        ],
    },
}

ANSWER, ASK, IGNORE = "answer", "ask", "ignore"


def ensure_model(models_dir: Path) -> Path:
    path = models_dir / MODEL_NAME
    if not path.exists():
        print(f"Downloading the speech recognizer for wake checks to {path}...")
        models_dir.mkdir(parents=True, exist_ok=True)
        archive = models_dir / f"{MODEL_NAME}.zip"
        urllib.request.urlretrieve(MODEL_URL, archive)
        with zipfile.ZipFile(archive) as z:
            z.extractall(models_dir)
        archive.unlink()
    return path


class PhraseVerifier:
    def __init__(self, phrase: str, models_dir: Path, check_path: Path | None = None):
        from vosk import Model, SetLogLevel

        if phrase not in PHRASES:
            raise SystemExit(f"No wake check is set up for '{phrase}'; add it to PHRASES in verify.py.")
        SetLogLevel(-1)
        self._model = Model(str(ensure_model(models_dir)))
        spec = PHRASES[phrase]
        self._grammar = json.dumps(spec["accept"] + spec["lookalikes"] + ["[unk]"])
        self._accept = [a.split() for a in spec["accept"]]
        self._ask_after_hey = set(spec["ask_after_hey"])
        self.last_confidence: float | None = None
        # Optional: a learned layer on top of the recognizer (see TunedCheck); without it, a plain phrase match.
        if check_path is not None and not check_path.exists():
            raise SystemExit(f"Wake check model {check_path} not found.")
        self._tuned = TunedCheck(json.loads(check_path.read_text())) if check_path else None
        if self._tuned:
            self._grammar = self._tuned.grammar

    def heard(self, pcm: np.ndarray) -> str:
        return self._recognize(pcm, alternatives=0)["text"]

    def _recognize(self, pcm: np.ndarray, alternatives: int) -> dict:
        from vosk import KaldiRecognizer

        recognizer = KaldiRecognizer(self._model, 16000, self._grammar)
        if alternatives:
            recognizer.SetMaxAlternatives(alternatives)
        recognizer.AcceptWaveform(np.asarray(pcm, dtype=np.int16).tobytes())
        return json.loads(recognizer.FinalResult())

    def check(self, pcm: np.ndarray) -> tuple[bool, str]:
        """Is the wake phrase in this audio? Also returns what the recognizer heard, for logging."""
        if self._tuned:
            alts = self._recognize(pcm, self._tuned.max_alternatives).get("alternatives", [])
            self.last_confidence = self._tuned.confidence(alts)
            return self.last_confidence >= self._tuned.threshold, (alts[0]["text"] if alts else "") or "nothing clear"
        words = self.heard(pcm).split()
        ok = any(words[i : i + len(a)] == a for a in self._accept for i in range(len(words)))
        # [unk] is the recognizer's catch-all: speech that matched none of the phrases it listens for.
        return ok, " ".join(words) or "nothing clear"

    def decide(self, pcm: np.ndarray) -> tuple[str, str]:
        """ANSWER, ASK ("Did you call me?") or IGNORE, plus what the recognizer heard."""
        ok, heard = self.check(pcm)
        if ok:
            return ANSWER, heard
        words = heard.split()
        close = any(a == "hey" and b in self._ask_after_hey for a, b in zip(words, words[1:]))
        return (ASK if close else IGNORE), heard


class TunedCheck:
    """How likely "hey TARS" is, from how the recognizer ranked every listed phrase (its top guesses and scores).

    Trained on the enrolled person's recordings plus synthetic voices, so a soft "t" that the recognizer
    scores as a near-tie between "tars" and "darts" or "cars" still counts, while a clear "hey cars" doesn't.
    """

    def __init__(self, spec: dict):
        self.phrases = [p.split() for p in spec["phrases"]]
        self.grammar = json.dumps(spec["phrases"] + spec["extra_grammar"])
        self.max_alternatives = spec["max_alternatives"]
        self.floor = spec["floor"]
        self.weights = np.array(spec["weights"])
        self.bias = spec["bias"]
        self.threshold = spec["threshold"]

    def features(self, alternatives: list[dict]) -> np.ndarray:
        top = alternatives[0]["confidence"] if alternatives else 0.0
        best = [self.floor] * len(self.phrases)
        for alt in alternatives:
            words = alt["text"].split()
            for i, phrase in enumerate(self.phrases):
                if any(words[j : j + len(phrase)] == phrase for j in range(len(words))):
                    best[i] = max(best[i], alt["confidence"] - top)
        unknown = not alternatives or not alternatives[0]["text"].strip() or "[unk]" in alternatives[0]["text"]
        return np.array(best + [float(unknown)])

    def confidence(self, alternatives: list[dict]) -> float:
        return float(1 / (1 + np.exp(-(self.features(alternatives) @ self.weights + self.bias))))


class RecentAudio:
    """The last WINDOW_S of mic audio, so the recognizer can hear the phrase that just woke us."""

    def __init__(self, seconds: float = WINDOW_S):
        self._blocks = deque(maxlen=max(1, round(seconds / BLOCK_SECONDS)))

    def add(self, block: np.ndarray) -> None:
        self._blocks.append(block)

    def audio(self) -> np.ndarray:
        return np.concatenate(self._blocks) if self._blocks else np.zeros(0, dtype=np.int16)


class VerifiedTrigger:
    """The wake-word model listens all the time; each wake is double-checked before the assistant answers."""

    def __init__(self, trigger, verifier: PhraseVerifier, window_s: float = WINDOW_S):
        self._trigger = trigger
        self._window_s = window_s
        self._verifier = verifier
        self.phrase = trigger.phrase
        self.last_audio: np.ndarray | None = None  # what woke us, so speaker ID can tell who said it

    def wait(self, mic: Microphone) -> str:
        """Returns ANSWER for a confirmed wake, ASK when it sounded close but not quite."""
        mic.clear()
        recent = RecentAudio(self._window_s)
        while True:
            block = mic.read()
            recent.add(block)
            if self._trigger.score(block) < self._trigger.threshold:
                continue
            self._trigger.reset()
            outcome, heard = self._verifier.decide(recent.audio())
            if outcome != IGNORE:
                self.last_audio = recent.audio()
                return outcome
            print(f"(Heard '{heard}', not '{self.phrase}'. Still listening.)")
