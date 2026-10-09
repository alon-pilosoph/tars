"""Second check on every wake: a small offline speech recognizer confirms the words.

The wake-word model is fast but can't reliably tell "hey TARS" from "hey cars". Vosk, restricted to a short list of
phrases, has to pick which one it heard, so it can.
"""

import json
import tempfile
import zipfile
from collections import deque
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from .audio import BLOCK_SECONDS, SAMPLE_RATE, Microphone
from .models import fetch
from .wake import DUE, Due, WakeModel, wake_word_trigger

if TYPE_CHECKING:
    from .journal import Journal
    from .versions import Pair, PairSource

MODEL_NAME = "vosk-model-small-en-us-0.15"  # 40 MB download, runs fine on a Raspberry Pi
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"

# Gives the recognizer somewhere else to put a lookalike. The learned layer scores each of these
# (training/stage2/train_check.py) and its weights follow this order, so append only.
LOOKALIKES = [
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
    "tars stop",
    "stop",
]
# "darts" is how "TARS" often comes out with a soft t, so the plain match accepts it; the learned layer tells a clear
# "hey darts" from a soft "hey TARS" by how close the two scored.
PHRASES = {
    "hey tars": {
        "accept": ["hey tars", "hey darts"],
        "lookalikes": [*LOOKALIKES, "hey"],
        # "hey" + one of these is close to the name but nobody says it: worth a "Did you call me?". Plausible
        # phrases ("hey there", "hey Jarvis", "hey Tara", a bare "stars") are ignored instead.
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
    # The interrupt, said while TARS talks. Run together it's often heard as "tar stop", which nobody says otherwise;
    # "stop" on its own, or after another word, mustn't count (the first "TARS stop" model fired on any "stop").
    "tars stop": {
        "accept": ["tars stop", "tar stop", "darts stop"],
        "lookalikes": [
            "stop",
            "stars stop",
            "star stop",
            "cars stop",
            "car stop",
            "bus stop",
            "pit stop",
            "hard stop",
            "please stop",
            "don't stop",
            "stop it",
            "hey tars",
            "tars",
            "stars",
        ],
        "ask_after_hey": [],
    },
}

ANSWER, ASK, IGNORE = "answer", "ask", "ignore"


def ensure_model(models_dir: Path) -> Path:
    path = models_dir / MODEL_NAME
    if not path.exists():
        archive = fetch(models_dir / f"{MODEL_NAME}.zip", MODEL_URL, "the speech recognizer for wake checks")
        with tempfile.TemporaryDirectory(dir=models_dir) as unpacked, zipfile.ZipFile(archive) as z:
            z.extractall(unpacked)
            (Path(unpacked) / MODEL_NAME).rename(path)
        archive.unlink()
    return path


class PhraseVerifier:
    def __init__(self, phrase: str, models_dir: Path, check_path: Path | None = None):
        from vosk import Model, SetLogLevel

        if phrase not in PHRASES:
            raise ValueError(f"No wake check is set up for '{phrase}'; add it to PHRASES in verify.py.")
        SetLogLevel(-1)
        self._model = Model(str(ensure_model(models_dir)))
        spec = PHRASES[phrase]
        # "[unk]" is Vosk's catch-all for speech that matches none of the phrases.
        self._phrase_grammar = json.dumps(spec["accept"] + spec["lookalikes"] + ["[unk]"])
        self._accept = [a.split() for a in spec["accept"]]
        self._ask_after_hey = set(spec["ask_after_hey"])
        self.last_confidence: float | None = None
        self._tuned: TunedCheck | None = None
        self.use_check(json.loads(check_path.read_text()) if check_path else None)

    def use_check(self, spec: dict | None) -> None:
        self._tuned = TunedCheck(spec) if spec else None
        self._grammar = self._tuned.grammar if self._tuned else self._phrase_grammar

    def heard(self, pcm: np.ndarray) -> str:
        return self._recognize(pcm, alternatives=0)["text"]

    def _recognize(self, pcm: np.ndarray, alternatives: int) -> dict:
        from vosk import KaldiRecognizer

        recognizer = KaldiRecognizer(self._model, SAMPLE_RATE, self._grammar)
        if alternatives:
            recognizer.SetMaxAlternatives(alternatives)
        recognizer.AcceptWaveform(np.asarray(pcm, dtype=np.int16).tobytes())
        return json.loads(recognizer.FinalResult())

    def check(self, pcm: np.ndarray) -> tuple[bool, str]:
        """Whether the wake phrase is in this audio, and what the recognizer heard."""
        if self._tuned:
            alts = self._recognize(pcm, self._tuned.max_alternatives).get("alternatives", [])
            self.last_confidence = self._tuned.confidence(alts)
            return self.last_confidence >= self._tuned.threshold, (alts[0]["text"] if alts else "") or "nothing clear"
        words = self.heard(pcm).split()
        ok = any(words[i : i + len(a)] == a for a in self._accept for i in range(len(words)))
        return ok, " ".join(words) or "nothing clear"

    def decide(self, pcm: np.ndarray) -> tuple[str, str]:
        """ANSWER, ASK ("Did you call me?") or IGNORE, plus what the recognizer heard."""
        ok, heard = self.check(pcm)
        if ok:
            return ANSWER, heard
        words = heard.split()
        close = any(a == "hey" and b in self._ask_after_hey for a, b in pairwise(words))
        return (ASK if close else IGNORE), heard


class TunedCheck:
    """How likely "hey TARS" is, from how the recognizer ranked every listed phrase (its top guesses and scores).

    Trained on the enrolled person's recordings plus synthetic voices, so a soft "t" that the recognizer scores as a
    near-tie between "tars" and "darts" or "cars" still counts, while a clear "hey cars" doesn't.
    """

    def __init__(self, spec: dict):
        self.phrases = [p.split() for p in spec["phrases"]]
        self.grammar = json.dumps(spec["phrases"] + spec["extra_grammar"])
        self.max_alternatives = spec["max_alternatives"]
        self.floor = spec["floor"]
        self.weights = np.array(spec["weights"])
        self.bias = spec["bias"]
        self.threshold = spec["threshold"]
        if self.weights.shape != (len(self.phrases) + 1,):  # one per phrase, and one for "heard nothing it knows"
            raise ValueError(f"{len(self.weights)} weights for {len(self.phrases)} phrases")

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
    def __init__(self, seconds: float):
        self._blocks = deque(maxlen=max(1, round(seconds / BLOCK_SECONDS)))

    def add(self, block: np.ndarray) -> None:
        self._blocks.append(block)

    def audio(self) -> np.ndarray:
        return np.concatenate(self._blocks) if self._blocks else np.zeros(0, dtype=np.int16)


# A score this close to the threshold without reaching it is logged as a near-miss (a possible missed wake).
NEAR_FRACTION = 0.6
NEAR_QUIET_S = 1.0  # a near-miss ends once the score has stayed below the near line this long
FOLLOW_EVERY_S = 3.0  # how often TARS looks for newly installed wake models while it listens


class NearMisses:
    """Wake scores into near-misses, shared by the assistant and the mic test so both count the same."""

    def __init__(self, threshold: float):
        self.threshold = threshold
        self.near = threshold * NEAR_FRACTION
        self.peak = 0.0
        self.rose = False  # the last score was the near-miss's new peak
        self._quiet = 0.0

    def update(self, score: float, seconds: float) -> float | None:
        """Feed one score, and how much audio it covers. Returns the peak once a near-miss has ended."""
        self.rose = False
        if score >= self.threshold:
            self.peak = 0.0
        elif score >= self.near:
            self.rose, self.peak, self._quiet = score > self.peak, max(self.peak, score), 0.0
        elif self.peak:
            self._quiet += seconds
            if self._quiet >= NEAR_QUIET_S:
                peak, self.peak = self.peak, 0.0
                return peak
        return None


class VerifiedTrigger:
    last_audio: np.ndarray | None = None  # what woke it, for speaker ID
    pair: Pair

    def __init__(
        self,
        pairs: PairSource,
        models_dir: Path,
        journal: Journal | None = None,
        make_trigger: Callable[[str, float], WakeModel] = wake_word_trigger,
        make_verifier: Callable[[str, Path], PhraseVerifier] = PhraseVerifier,
    ):
        """The wake model, threshold, check and check window come from the pair `pairs` has in use, and follow it
        while TARS runs."""
        self._pairs, self._models_dir, self._journal = pairs, models_dir, journal
        self._make_trigger, self._make_verifier = make_trigger, make_verifier
        self._stamp, self.pair, self._verifier = None, None, None
        self._follow_versions(quiet=True)
        self.phrase = self._trigger.phrase

    def wait(self, mic: Microphone, due: Due | None = None) -> str:
        """Returns ANSWER for a confirmed wake, ASK when it sounded close but not quite, DUE when `due` says a
        reminder is due."""
        mic.clear()

        def fresh() -> tuple[RecentAudio, NearMisses, None]:
            return RecentAudio(self.pair.check_window_s), NearMisses(self._trigger.threshold), None

        recent, near, peak_audio = fresh()
        follow_blocks, blocks = max(1, round(FOLLOW_EVERY_S / BLOCK_SECONDS)), 0
        while True:
            blocks += 1
            if blocks % follow_blocks == 0 and self._follow_versions():
                recent, near, peak_audio = fresh()
            block = mic.read()
            recent.add(block)
            score = self._trigger.score(block)
            peak = near.update(score, len(block) / SAMPLE_RATE)
            if near.rose:
                peak_audio = recent.audio()
            if peak is not None and self._journal:
                self._journal.near_miss(peak_audio, peak, self.pair.wake_model)
            if score < self._trigger.threshold:
                if due and due():
                    return DUE
                continue
            self._trigger.reset()
            audio = recent.audio()
            outcome, heard = self._verifier.decide(audio)
            if self._journal:
                self._journal.wake(
                    audio,
                    score,
                    outcome,
                    heard,
                    self._verifier.last_confidence,
                    self.pair.wake_model,
                    self.pair.check_model,
                )
            if outcome != IGNORE:
                self.last_audio = audio
                return outcome
            print(f"(heard '{heard}', not '{self.phrase}'; still listening)")

    def _follow_versions(self, quiet: bool = False) -> bool:
        """Switches to the pair in use if it changed; True if it did. A pair that won't load gives way to the
        installed one; only a broken installed pair (a mistake in config.toml) raises, and only at startup."""
        try:
            stamp = self._pairs.stamp()
            if stamp == self._stamp:
                return False
            self._stamp = stamp
            pair = self._pairs.in_use()
            if pair == self.pair:
                return False
            try:
                self._listen_with(pair)
            except Exception as e:  # whatever a damaged model file makes its loader raise
                installed = self._pairs.installed()
                if pair == installed:
                    raise
                print(f"(couldn't load the wake models {pair.version}, using the installed ones: {e!r})")
                if installed == self.pair:
                    return False
                self._listen_with(installed)
            if not quiet:
                print(f"(now listening with the wake models {self.pair.version})")
            return True
        except Exception as e:  # mid-run, nothing may stop TARS listening
            if self.pair is None:
                raise
            print(f"(couldn't switch the wake models, keeping {self.pair.version}: {e!r})")
            return False

    def _listen_with(self, pair: Pair) -> None:
        trigger = self._make_trigger(str(pair.model_path), pair.threshold)
        if self._verifier is None:
            self._verifier = self._make_verifier(trigger.phrase, self._models_dir)
        self._verifier.use_check(pair.check)
        self._trigger, self.pair = trigger, pair
