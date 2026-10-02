"""The wake models TARS listens with: the pair config.toml installs, and every pair `training.household` made.

A pair is the wake model (stage 1), the double-check's learned layer (stage 2), and the threshold and check window
they were tuned for. They're trained, tested and switched together, since the window depends on when the wake model
fires. Trained pairs live in <learning folder>/wake_models/<setup>/v1/, v2/... (the setup is the installed check's
folder, e.g. "generic"), each with its test results; history.json says which one is in use. `voice-assistant
--install-models` adds one, the web UI can switch to any of them, and the assistant follows within seconds without a
restart.

Nothing here can keep TARS from listening: an unreadable history, a missing or damaged pair, or pairs made from a
different installed pair all fall back to the installed pair. The next change then starts a new history, keeping the
old file aside.
"""

import fcntl
import hashlib
import json
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Config
from .files import atomic_write, fsync_folder

HISTORY = "history.json"
LOCK = ".lock"
INSTALLED = "installed"  # the version name of config.toml's own pair
MODEL, CHECK, ABOUT = "hey_tars.tflite", "hey_tars_check.json", "version.json"
UNUSABLE = (OSError, ValueError, KeyError, TypeError)  # a file that's missing, cut short or not what it should be


@dataclass(frozen=True)
class Pair:
    version: str  # "installed", "v2"...
    wake_model: str  # the files, relative to the project where possible, as the event log records them
    check_model: str  # "" for a plain phrase match
    check: dict | None  # the learned layer itself
    threshold: float
    check_window_s: float
    model_path: Path
    note: str = ""
    results: list | None = None  # how it tested against the pair it replaced
    ts: float = 0.0
    replaced: str | None = None  # the version in use when it was installed, which `results` compares it with


class PairSource(Protocol):
    def stamp(self) -> tuple:
        """Changes whenever what in_use() returns could have: cheap enough to check every few seconds."""

    def in_use(self) -> Pair: ...

    def installed(self) -> Pair:
        """config.toml's own pair, the one to fall back to."""


class FixedPair:
    """config.toml's own pair, for a setup without versions (no learned double-check, or not a microWakeWord model)."""

    def __init__(self, pair: Pair):
        self._pair = pair

    def stamp(self) -> tuple:
        return ()

    def in_use(self) -> Pair:
        return self._pair

    installed = in_use


class ModelVersions:
    def __init__(
        self,
        learning_folder: Path,
        root: Path,
        wake_model: str,
        check_model: str,
        threshold: float,
        check_window_s: float,
    ):
        """The installed pair, as config.toml names it (paths relative to `root`)."""
        self._root = root
        self._installed = {
            "wake_model": wake_model,
            "check_model": check_model,
            "threshold": threshold,
            "check_window_s": check_window_s,
        }
        self.folder = learning_folder / "wake_models" / Path(check_model).parent.name

    def stamp(self) -> tuple:
        paths = [self.folder / HISTORY] + [self._root / self._installed[k] for k in ("wake_model", "check_model")]
        return tuple(_mtime(p) for p in paths)

    def in_use(self) -> Pair:
        """The pair in use, read and checked; the installed one if that fails. Only a broken installed pair (a
        mistake in config.toml) raises."""
        pair, damaged = self._in_use(self._read()[0])
        if damaged:
            print(f"(couldn't load the wake models {damaged}, using the installed ones)")
        return pair

    def overview(self) -> tuple[Pair, list[dict], str | None]:
        """For the Models page, from one read of the history: the pair in use; every version, newest first and the
        installed pair last, as {version, ts, note, active}; and why the saved history is ignored, if it is."""
        history, problem = self._read()
        # The model itself is loaded here, as the assistant does, so a damaged one shows as not in use.
        pair, damaged = self._in_use(history, load_model=True)
        if damaged and not problem:
            problem = f"Couldn't load the wake models {damaged}, so TARS uses the installed ones."
        rows = [self.installed()] + [p for p in (self._try_load(e) for e in history["versions"]) if p]
        return (
            pair,
            [
                {"version": p.version, "ts": p.ts, "note": p.note, "active": p.version == pair.version}
                for p in reversed(rows)
            ],
            problem,
        )

    def install(self, folder: Path, based_on: str | None = None) -> str:
        """Add a trained pair (a folder with MODEL, CHECK and ABOUT) and put it in use. With `based_on`, only if
        that's still the version in use: the pair was tested against it. Returns the new version's name."""
        about = json.loads((folder / ABOUT).read_text())
        _check_pair(folder / MODEL, json.loads((folder / CHECK).read_text()), about)
        with self._changing():
            history = self._writable()
            active = self._in_use(history)[0].version
            if based_on is not None and active != based_on:
                raise ValueError(
                    f"it was tested against {based_on}, but {active} is in use now; train again, "
                    "or install it without the check"
                )
            numbers = [v["number"] for v in history["versions"]]
            numbers += [int(p.name[1:]) for p in self.folder.glob("v*") if p.name[1:].isdigit()]
            number = 1 + max(numbers, default=0)  # never reuses a folder, even one an older history pointed to
            name = f"v{number}"
            staging = self.folder / f".{name}.tmp"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            for f in (MODEL, CHECK):
                _copy(folder / f, staging / f)
            about = {**about, "ts": about.get("ts") or time.time(), "replaced": active}
            atomic_write(staging / ABOUT, json.dumps(about, indent=1))
            staging.rename(self.folder / name)
            fsync_folder(self.folder)
            history["versions"].append({"version": name, "number": number, "folder": name})
            history["active"] = name
            self._save(history)
            return name

    def use(self, version: str) -> None:
        """KeyError for a version that doesn't exist or can't be loaded."""
        with self._changing():
            history = self._writable()
            if version != INSTALLED:
                entry = self._find(history, version)
                if entry is None:
                    raise KeyError(version)
                try:
                    self._load(entry, load_model=True)
                except UNUSABLE as e:
                    raise KeyError(f"{version} can't be loaded: {e!r}") from None
            history["active"] = None if version == INSTALLED else version
            self._save(history)

    def installed(self) -> Pair:
        i = self._installed
        check = json.loads((self._root / i["check_model"]).read_text())
        path = self._root / i["wake_model"]
        return Pair(
            INSTALLED,
            i["wake_model"],
            i["check_model"],
            check,
            i["threshold"],
            i["check_window_s"],
            path,
            "What TARS was installed with.",
            ts=_mtime(path)[0] or 0.0,
        )

    @contextmanager
    def _changing(self) -> Iterator[None]:
        """One change to the history at a time, across processes: an install runs as its own command (often over
        ssh from the training Mac), while "Use this" runs in the web UI."""
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(self.folder / LOCK, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # released when the file closes
            yield

    def _in_use(self, history: dict, load_model: bool = False) -> tuple[Pair, str | None]:
        """(the pair in use, None), or (the installed pair, why the chosen one couldn't be loaded)."""
        entry = self._find(history, history["active"])
        if entry is not None:
            try:
                return self._load(entry, load_model), None
            except UNUSABLE as e:
                return self.installed(), f"{entry['version']}: {e!r}"
        return self.installed(), None

    def _load(self, entry: dict, load_model: bool = False) -> Pair:
        folder = self.folder / entry["folder"]
        about = json.loads((folder / ABOUT).read_text())
        check = json.loads((folder / CHECK).read_text())
        _check_pair(folder / MODEL, check, about, load_model)
        return Pair(
            entry["version"],
            self._name(folder / MODEL),
            self._name(folder / CHECK),
            check,
            float(about["threshold"]),
            float(about["check_window_s"]),
            folder / MODEL,
            about.get("note", ""),
            about.get("results"),
            float(about["ts"]),
            about.get("replaced"),
        )

    def _try_load(self, entry: dict) -> Pair | None:
        try:
            return self._load(entry)
        except UNUSABLE:
            return None

    def _name(self, path: Path) -> str:
        return str(path.relative_to(self._root)) if path.is_relative_to(self._root) else str(path)

    def _installed_hash(self) -> str:
        digest = hashlib.sha256(json.dumps(self._installed, sort_keys=True).encode())
        for key in ("wake_model", "check_model"):
            digest.update((self._root / self._installed[key]).read_bytes())
        return digest.hexdigest()

    def _read(self) -> tuple[dict, str | None]:
        """(the history, None), or (an empty one, why the saved one can't be used)."""
        empty = {"installed": None, "active": None, "versions": []}
        try:
            history = json.loads((self.folder / HISTORY).read_text())
            if not isinstance(history, dict) or not isinstance(history.get("versions"), list):
                raise TypeError("not a version history")
        except FileNotFoundError:
            return empty, None
        except (OSError, ValueError, TypeError) as e:
            return empty, (
                f"The version history couldn't be read ({e}), so TARS uses the installed models. The next "
                'install or "Use this" starts a new one; the old file is kept.'
            )
        if history.get("installed") != self._installed_hash():
            return empty, (
                "The installed models have changed since these versions were trained, so TARS uses them. "
                "The next install starts a new history."
            )
        return history, None

    def _writable(self) -> dict:
        """The history to change. One that can't be used is kept aside, and a new one started."""
        history, problem = self._read()
        path = self.folder / HISTORY
        if problem and path.exists():
            path.replace(path.with_name(f"history-{time.strftime('%Y%m%d-%H%M%S')}.old.json"))
        history["installed"] = self._installed_hash()
        return history

    def _save(self, history: dict) -> None:
        atomic_write(self.folder / HISTORY, json.dumps(history, indent=1))

    @staticmethod
    def _find(history: dict, name: str | None) -> dict | None:
        return next((v for v in history["versions"] if v["version"] == name), None)


def model_versions(cfg: Config, root: Path) -> ModelVersions | None:
    """The versions of the pair config.toml installs; None without a learned double-check to pair with."""
    w = cfg.wake
    if not (w.verify and w.check_model and w.model.endswith(".tflite")):
        return None
    return ModelVersions(root / cfg.learning.folder, root, w.model, w.check_model, w.threshold, w.check_window_s)


def pair_source(cfg: Config, root: Path) -> ModelVersions | FixedPair:
    """What TARS listens with: the versions of config.toml's pair, or that pair alone when it can't have versions."""
    if versions := model_versions(cfg, root):
        return versions
    w = cfg.wake
    check_model = w.check_model if w.verify else ""
    check = json.loads((root / check_model).read_text()) if check_model else None
    # A file, or the name of one of openWakeWord's pretrained models.
    model = root / w.model if w.model.endswith((".tflite", ".onnx")) else Path(w.model)
    return FixedPair(
        Pair(
            INSTALLED,
            w.model,
            check_model,
            check,
            w.threshold,
            w.check_window_s,
            model,
            "What TARS was installed with.",
        )
    )


def _check_pair(model: Path, check: dict, about: dict, load_model: bool = True) -> None:
    from .verify import TunedCheck

    TunedCheck(check)
    if not (0 < float(about["threshold"]) < 1 and 0 < float(about["check_window_s"]) <= 10):
        raise ValueError(f"threshold {about['threshold']} or check window {about['check_window_s']} is out of range")
    if load_model:
        from .wake import MicroWakeWordTrigger

        MicroWakeWordTrigger(str(model), float(about["threshold"]))
    elif not model.is_file():
        raise FileNotFoundError(model)


def _mtime(path: Path) -> tuple:
    try:
        st = path.stat()
        return st.st_mtime, st.st_mtime_ns, st.st_size
    except FileNotFoundError:
        return None, None, None


def _copy(source: Path, target: Path) -> None:
    shutil.copyfile(source, target)
    with open(target, "rb") as f:
        os.fsync(f.fileno())
