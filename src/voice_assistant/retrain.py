"""Retrain the double-check's learned layer on the household's own wakes: "Retrain now" on the Models page.

Each candidate is fitted from scratch, on the installed layer's own training examples (hey_tars_check_data.npz next
to it, made by training/stage2/export_data.py) plus the household's labeled wakes, except every fifth one, which is
kept for testing. It replaces the layer in use only if it answers more of those held-out wakes, or lets fewer
through that weren't for TARS, and does no worse on anything in the fixed test set: held-out voices in eight
conditions, lookalikes, and an hour each of TV and audiobooks, streamed through the wake model. Fitting takes
seconds; the recognizer runs once on each saved wake, and what it found is cached.
"""

import json
import time
import traceback
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio import read_wav
from .checks import CheckVersions
from .events import NOT_REAL, REAL, WAKE, EventLog
from .verify import IGNORE, PhraseVerifier, TunedCheck

# Per wake: what the personal layer gives each of the owner's recordings (training/stage2/train_check.py).
HOUSEHOLD_WEIGHT = 5.0
# Wakes whose id is a multiple of this are held out: always the same ones, so the test only grows.
TEST_EVERY = 5
# Names in the fixed test set (training/stage2/export_data.py).
VOICES, LOOKALIKES, QUIET = "other voices hey TARS", "other lookalikes", "clean"
CACHE = "features.npz"


def training_label(event: dict) -> str | None:
    """A person's label, else the automatic one, except where that's only the check's own verdict ("it heard
    something else"): learning from those would teach the check what it already thinks."""
    if event["label"]:
        return event["label"]
    return None if event["outcome"] == IGNORE else event["auto_label"]


@dataclass
class Score:
    """One line of the comparison, in whole clips (or wakes per hour), so "no worse" means exactly that."""

    name: str
    current: int
    candidate: int
    total: int
    lower_is_better: bool = False
    household: bool = False
    shown: Callable[[int, int], str] = lambda k, n: f"{k} of {n}"

    def better(self) -> bool:
        return self.candidate < self.current if self.lower_is_better else self.candidate > self.current

    def worse(self) -> bool:
        return self.candidate > self.current if self.lower_is_better else self.candidate < self.current

    def row(self) -> dict:
        return {
            "name": self.name,
            "current": self.shown(self.current, self.total),
            "candidate": self.shown(self.candidate, self.total),
            "lower_is_better": self.lower_is_better,
        }


def percent(k: int, n: int) -> str:
    return f"{100 * k / n:.1f}%" if n else "none"


class Retrainer:
    """What the Models page works with (webui.Models): what's in use, retraining, and switching versions.
    `versions` is None when the double-check has no learned layer: the page still shows the wake model."""

    def __init__(
        self,
        log: EventLog,
        versions: CheckVersions | None,
        root: Path,
        verifier: Callable[[], PhraseVerifier],
        wake: dict,
    ):
        self._log, self.versions, self._root = log, versions, root
        self._make_verifier, self._verifier = verifier, None  # loads the recognizer: only when retraining
        self._wake = wake  # what the Models page shows about the wake model

    def info(self) -> dict:
        if self.versions is None:
            return {
                "active": {**self._wake, "check_model": "plain phrase match"},
                "history": [],
                "last_retrain": None,
                "trainable": 0,
            }
        in_use = self.versions.in_use()
        return {
            "active": {**self._wake, "check_model": in_use.name, "check_version": in_use.version},
            "history": self.versions.versions(),
            "last_retrain": self.versions.last_retrain(),
            "problem": self.versions.problem(),
            "trainable": len(self._labeled(check_audio=False)),
        }

    def use(self, version: str) -> None:
        if self.versions is None:
            raise KeyError(version)
        self.versions.use(version)

    def retrain(self) -> dict:
        if self.versions is None:
            return {
                "status": "skipped",
                "ts": time.time(),
                "summary": "The double-check has no learned layer to retrain: check_model is empty in config.toml.",
            }
        try:
            return self._retrain()
        except Exception as e:  # noqa: BLE001 - shown on the Models page instead of a bare error
            traceback.print_exc()
            return self._skip(f"Retraining failed: {e!r}")

    def _retrain(self) -> dict:
        installed_path = self._root / self.versions.installed
        data_path = installed_path.with_name(f"{installed_path.stem}_data.npz")
        if not data_path.exists():
            return self._skip(
                f"Retraining needs the check's own training data, {data_path.name}, next to it. "
                "training/stage2/export_data.py makes it."
            )
        installed = json.loads(installed_path.read_text())
        wakes = self._labeled()
        features = self._features(installed, [e for e, _ in wakes])
        wakes = [(e, label) for e, label in wakes if e["id"] in features]  # the ones whose audio could be read
        train = [(e, label) for e, label in wakes if e["id"] % TEST_EVERY]
        test = [(e, label) for e, label in wakes if not e["id"] % TEST_EVERY]
        if not train or not test:
            return self._skip(
                f"Not enough to learn from yet: {len(wakes)} labeled wakes, and every fifth is kept "
                "aside to test on. Keep talking to TARS, and answer the wakes on the Review tab."
            )
        from sklearn.linear_model import LogisticRegression

        start = self.versions.in_use()  # what the candidate has to beat
        with np.load(data_path) as data:
            fixed = {k: data[k] for k in data.files}
        X = np.vstack([fixed["train_X"], [features[e["id"]] for e, _ in train]])
        y = np.concatenate([fixed["train_y"], [label == REAL for _, label in train]])
        w = np.concatenate([fixed["train_w"], np.full(len(train), HOUSEHOLD_WEIGHT)])
        # As training/stage2/train_check.py fits the installed layer.
        clf = LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced").fit(X, y, sample_weight=w)
        real = sum(label == REAL for _, label in train)
        note = f"Retrained on {len(train)} of your wakes ({real} real, {len(train) - real} not)."
        candidate = {
            **installed,
            "about": note,
            "weights": clf.coef_[0].round(6).tolist(),
            "bias": round(float(clf.intercept_[0]), 6),
            "threshold": start.spec["threshold"],
        }

        scores = compare(
            start.spec,
            candidate,
            fixed,
            np.array([features[e["id"]] for e, _ in test]),
            np.array([label == REAL for _, label in test]),
        )
        swap = any(s.better() for s in scores if s.household) and not any(s.worse() for s in scores)
        result = {
            "status": "swapped" if swap else "kept",
            "labeled": len(train),
            "ts": time.time(),
            "metrics": [s.row() for s in scores],
        }
        if swap:
            version = self.versions.add(candidate, note, result, based_on=start.version)
            if version:
                return {**result, "version": version}
            result = {
                **result,
                "status": "kept",
                "summary": f"The check in use changed from {start.version} while "
                "this ran, so nothing was swapped. Retrain again to "
                "compare with the one in use now.",
            }
        self.versions.record(result)
        return result

    def _skip(self, why: str) -> dict:
        result = {"status": "skipped", "summary": why, "ts": time.time()}
        try:
            self.versions.record(result)
        except Exception:  # noqa: BLE001 - the page still gets the reason
            traceback.print_exc()
        return result

    def _labeled(self, check_audio: bool = True) -> list[tuple[dict, str]]:
        """Every wake with saved audio and a label to learn from. Near-misses aren't: the check never judges them.
        `check_audio=False` skips looking for each file, for a quick count."""
        wakes = self._log.events(limit=1_000_000_000, kind=WAKE)
        labeled = [
            (e, training_label(e))
            for e in wakes
            if e["audio"] and (not check_audio or (self._log.folder / e["audio"]).exists())
        ]
        return [(e, label) for e, label in labeled if label in (REAL, NOT_REAL)]

    def _features(self, spec: dict, events: list[dict]) -> dict[int, np.ndarray]:
        """What the recognizer makes of each wake's audio, by event id; a wake whose audio can't be read is left
        out. Cached, since the audio never changes; the cache is thrown away if the layer's phrases or settings
        are different."""
        key = json.dumps({k: spec[k] for k in ("phrases", "extra_grammar", "max_alternatives", "floor")})
        path = self.versions.folder / CACHE
        cached: dict[int, np.ndarray] = {}
        try:
            with np.load(path) as saved:
                if str(saved["key"]) == key:
                    cached = dict(zip(saved["ids"].tolist(), saved["X"], strict=True))
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001 - a damaged cache only costs running the recognizer again
            print(f"(Couldn't read {path}, making it again: {e!r})")
        missing = [e for e in events if e["id"] not in cached]
        if not missing:
            return cached
        if self._verifier is None:
            self._verifier = self._make_verifier()
        try:
            for e in missing:
                try:
                    pcm = read_wav(self._log.folder / e["audio"])
                except (OSError, EOFError, wave.Error) as err:  # cut short by a crash or a full disk
                    print(f"(Leaving out wake {e['id']}: its audio can't be read: {err!r})")
                    continue
                # float32, as they're cached: a retrain gives the same result whether they were cached or not.
                cached[e["id"]] = self._verifier.features(pcm).astype(np.float32)
        finally:  # whatever the recognizer got through is kept
            ids = [e["id"] for e in events if e["id"] in cached]
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".part")
            with open(partial, "wb") as f:
                np.savez(
                    f,
                    key=key,
                    ids=np.array(ids, np.int64),
                    X=np.array([cached[i] for i in ids], np.float32).reshape(len(ids), -1),
                )
            partial.replace(path)  # a retrain cut short never leaves half a cache
        return cached


def compare(current: dict, candidate: dict, fixed: dict, house_X: np.ndarray, house_real: np.ndarray) -> list[Score]:
    """Both layers on the household's held-out wakes, then on the fixed test set, end to end: a clip the wake model
    didn't fire on isn't answered, whatever the check would have said."""
    layers = [TunedCheck(current), TunedCheck(candidate)]

    def score(name: str, X: np.ndarray, where: np.ndarray, total: int, **kw) -> Score:
        counts = [int((layer.probability(X) >= layer.threshold)[where].sum()) if where.any() else 0 for layer in layers]
        return Score(name, *counts, total, **kw)

    X, sets, conds, fired = fixed["test_X"], fixed["test_set"], fixed["test_cond"], fixed["test_fired"]
    quiet, noisy = (sets == VOICES) & (conds == QUIET), (sets == VOICES) & (conds != QUIET)
    lookalikes = sets == LOOKALIKES
    rows = [
        score("Your held-out hey TARS", house_X, house_real, int(house_real.sum()), household=True),
        score(
            "Your held-out wakes that weren't for TARS, let through",
            house_X,
            ~house_real,
            int((~house_real).sum()),
            lower_is_better=True,
            household=True,
        ),
        score("Held-out voices, quiet", X, fired & quiet, int(quiet.sum()), shown=percent),
        score("Held-out voices, TV and chatter", X, fired & noisy, int(noisy.sum()), shown=percent),
        score(
            "Lookalikes let through", X, fired & lookalikes, int(lookalikes.sum()), lower_is_better=True, shown=percent
        ),
    ]
    for source, hours in json.loads(str(fixed["long_hours"])).items():
        rows.append(
            score(
                f"False answers per hour, {source}",
                fixed["long_X"],
                fixed["long_name"] == source,
                0,
                lower_is_better=True,
                shown=lambda k, _n, h=hours: f"{k / h:.1f}",
            )
        )
    return [r for r in rows if r.total or not r.household]
