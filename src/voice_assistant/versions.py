"""The wake models TARS listens with: the pair config.toml installs, and every pair `training.household` made.

A pair is the wake model (stage 1), the double-check's learned layer (stage 2), and the threshold and check window
they were tuned for: they're trained, tested and switched together, since the window depends on when the wake model
fires. config.toml's [wake] section names the installed pair. Trained pairs are kept with the rest of what TARS
learned, in <learning folder>/wake_models/<setup>/v1/, v2/... (the setup is the installed check's folder, e.g.
"generic"), each with its test results, and history.json says which one is in use. `voice-assistant --install-models`
adds one; the web UI can switch back to any of them. The assistant notices a change within seconds and switches
without a restart.

Nothing here can keep TARS from listening: a history that can't be read, a pair that's missing or damaged, or pairs
made from a different installed pair (config.toml changed, or an update replaced the files) all fall back to the
installed pair. The next change then starts a new history, keeping the old file aside.
"""

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config

HISTORY = "history.json"
INSTALLED = "installed"  # the version name of config.toml's own pair
MODEL, CHECK, ABOUT = "hey_tars.tflite", "hey_tars_check.json", "version.json"  # the files of a trained pair
UNUSABLE = (OSError, ValueError, KeyError, TypeError)  # a file that's missing, cut short or not what it should be


@dataclass(frozen=True)
class Pair:
    version: str  # "installed", "v2"...
    wake_model: str  # the files, relative to the project where possible, as the event log records them
    check_model: str
    check: dict  # the learned layer itself
    threshold: float
    check_window_s: float
    note: str
    results: list | None  # how it tested against the pair it replaced
    ts: float
    model_path: Path


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
        self._lock = threading.Lock()  # the web UI and an install can change the history at the same time

    def stamp(self) -> tuple:
        """Changes whenever what in_use() returns could have: cheap enough to check every few seconds."""
        paths = [self.folder / HISTORY] + [self._root / self._installed[k] for k in ("wake_model", "check_model")]
        return tuple(_mtime(p) for p in paths)

    def in_use(self) -> Pair:
        """The pair in use, read and checked; the installed one if that fails. Only a broken installed pair (a
        mistake in config.toml) raises."""
        history, _ = self._read()
        entry = self._find(history, history["active"])
        if entry is not None:
            try:
                return self._load(entry)
            except UNUSABLE as e:
                print(f"(Couldn't load the wake models {entry['version']}, using the installed ones: {e!r})")
        return self._installed_pair()

    def problem(self) -> str | None:
        """Why the saved history is being ignored, if it is, for the Models page."""
        return self._read()[1]

    def versions(self) -> list[dict]:
        """Newest first, the installed pair last: {version, ts, note, active}."""
        history, _ = self._read()
        in_use = self.in_use().version
        rows = [self._installed_pair()] + [p for p in (self._try_load(e) for e in history["versions"]) if p]
        return [
            {"version": p.version, "ts": p.ts, "note": p.note, "active": p.version == in_use} for p in reversed(rows)
        ]

    def install(self, folder: Path, based_on: str | None = None) -> str:
        """Add a trained pair (a folder with MODEL, CHECK and ABOUT) and put it in use. With `based_on`, only if
        that's still the version in use: the pair was tested against it. Returns the new version's name."""
        about = json.loads((folder / ABOUT).read_text())
        _check_pair(folder / MODEL, json.loads((folder / CHECK).read_text()), about)
        with self._lock:
            history = self._writable()
            active = self._active_name(history)
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
            _write(staging / ABOUT, json.dumps({**about, "ts": about.get("ts") or time.time()}, indent=1))
            staging.rename(self.folder / name)  # all at once: a half-copied pair never looks like a version
            _fsync_folder(self.folder)
            history["versions"].append({"version": name, "number": number, "folder": name})
            history["active"] = name
            self._save(history)
            return name

    def use(self, version: str) -> None:
        """KeyError for a version that doesn't exist or can't be loaded."""
        with self._lock:
            history = self._writable()
            if version != INSTALLED:
                entry = self._find(history, version)
                if entry is None:
                    raise KeyError(version)
                try:
                    self._load(entry)
                except UNUSABLE as e:
                    raise KeyError(f"{version} can't be loaded: {e!r}") from None
            history["active"] = None if version == INSTALLED else version
            self._save(history)

    def _installed_pair(self) -> Pair:
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
            f"What TARS was installed with ({i['wake_model']}).",
            None,
            _mtime(path)[0] or 0.0,
            path,
        )

    def _load(self, entry: dict) -> Pair:
        folder = self.folder / entry["folder"]
        about = json.loads((folder / ABOUT).read_text())
        check = json.loads((folder / CHECK).read_text())
        _check_pair(folder / MODEL, check, about, load_model=False)
        return Pair(
            entry["version"],
            self._name(folder / MODEL),
            self._name(folder / CHECK),
            check,
            float(about["threshold"]),
            float(about["check_window_s"]),
            about.get("note", ""),
            about.get("results"),
            float(about["ts"]),
            folder / MODEL,
        )

    def _try_load(self, entry: dict) -> Pair | None:
        try:
            return self._load(entry)
        except UNUSABLE:
            return None

    def _active_name(self, history: dict) -> str:
        """The version in use, as in_use() would find it, from an already-read history."""
        entry = self._find(history, history["active"])
        return entry["version"] if entry is not None and self._try_load(entry) else INSTALLED

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
        _write(self.folder / HISTORY, json.dumps(history, indent=1))

    @staticmethod
    def _find(history: dict, name: str | None) -> dict | None:
        return next((v for v in history["versions"] if v["version"] == name), None)


def model_versions(cfg: Config, root: Path) -> ModelVersions | None:
    """The versions of the pair config.toml installs; None without a learned double-check to pair with."""
    w = cfg.wake
    if not (w.verify and w.check_model and w.model.endswith(".tflite")):
        return None
    return ModelVersions(root / cfg.learning.folder, root, w.model, w.check_model, w.threshold, w.check_window_s)


def listening_with(cfg: Config, root: Path) -> tuple[str, float, float, dict | None]:
    """(wake model, threshold, check window, learned layer or None): the pair in use, or config.toml's own."""
    if versions := model_versions(cfg, root):
        pair = versions.in_use()
        return str(pair.model_path), pair.threshold, pair.check_window_s, pair.check
    check = json.loads((root / cfg.wake.check_model).read_text()) if cfg.wake.check_model else None
    return cfg.wake.model, cfg.wake.threshold, cfg.wake.check_window_s, check


def _check_pair(model: Path, check: dict, about: dict, load_model: bool = True) -> None:
    """Raises if the assistant couldn't use this pair."""
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


def _write(path: Path, text: str) -> None:
    """Written to a temporary file, flushed to disk and swapped in, so a reader never sees half a file, even after
    a power cut."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_folder(path.parent)


def _fsync_folder(folder: Path) -> None:
    """A rename into a folder reaches the disk only when the folder itself is flushed."""
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
