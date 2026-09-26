"""The double-check's learned layer: the one TARS was installed with, and every version "Retrain now" made of it.

config.toml names the installed layer ([wake] check_model). Retrained versions are kept with the rest of what TARS
learned, in <learning folder>/checks/<setup>/ (the setup is the installed layer's folder, e.g. "generic"), with a
history.json saying which one is in use. The web UI writes it; the assistant reads it on every wake, so a new
version, or a rollback, applies from the next wake without a restart.

Nothing here can keep TARS from listening: a history that can't be read, a version file that's missing or damaged,
or versions made from a different installed layer (check_model changed, or an update replaced the file) all fall
back to the installed layer. The next change from the web UI then starts a new history, keeping the old file aside.
"""

import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config

HISTORY = "history.json"
INSTALLED = "installed"  # the version name of config.toml's own layer
UNUSABLE = (OSError, ValueError, KeyError, TypeError)  # a file that's missing, cut short or not a layer


@dataclass(frozen=True)
class InUse:
    version: str  # "installed", "check v2"...
    name: str  # what the event log records: the file, relative to the project
    spec: dict
    stamp: tuple  # changes whenever the file does, so a rewritten file counts as a new version


class CheckVersions:
    def __init__(self, learning_folder: Path, root: Path, installed: str):
        self.installed = installed  # relative to root, as config.toml has it
        self._root = root
        self.folder = learning_folder / "checks" / Path(installed).parent.name
        self._lock = threading.Lock()  # the web UI can retrain and roll back at the same time

    def in_use(self) -> InUse:
        """The version in use, read and parsed; the installed one if that fails. Only a broken installed layer
        (a mistake in config.toml) raises."""
        history, _ = self._read()
        version = self._find(history, history["active"])
        if version is not None:
            try:
                return self._load(version["version"], self.folder / version["file"])
            except UNUSABLE as e:
                print(f"(Couldn't load the wake check {version['version']}, using the installed one: {e!r})")
        return self._load(INSTALLED, self._root / self.installed)

    def problem(self) -> str | None:
        """Why the saved history is being ignored, if it is, for the Models page."""
        return self._read()[1]

    def versions(self) -> list[dict]:
        """Newest first, the installed one last: {version, ts, note, active}."""
        history, _ = self._read()
        installed = self._root / self.installed
        rows = [
            {
                "version": INSTALLED,
                "ts": installed.stat().st_mtime if installed.exists() else 0,
                "note": f"What TARS was installed with ({self.installed}).",
            }
        ]
        rows += [{k: v[k] for k in ("version", "ts", "note")} for v in history["versions"]]
        in_use = self.in_use().version
        return [{**r, "active": r["version"] == in_use} for r in reversed(rows)]

    def last_retrain(self) -> dict | None:
        return self._read()[0]["last_retrain"]

    def add(self, spec: dict, note: str, result: dict, based_on: str) -> str | None:
        """Save a new version and put it in use, unless the version in use is no longer `based_on` (someone
        switched while it was being trained and tested): then nothing changes, and it returns None."""
        with self._lock:
            history = self._writable()
            if self._active_name(history) != based_on:
                return None
            numbers = [v["number"] for v in history["versions"]]
            numbers += [int(p.stem[1:]) for p in self.folder.glob("v*.json") if p.stem[1:].isdigit()]
            number = 1 + max(numbers, default=0)  # never reuses a file, even one an older history pointed to
            name, file = f"check v{number}", f"v{number}.json"
            _write(self.folder / file, json.dumps({**spec, "about": note}, indent=1))
            history["versions"].append(
                {"version": name, "number": number, "file": file, "ts": time.time(), "note": note}
            )
            history["active"] = name
            history["last_retrain"] = {**result, "version": name}
            self._save(history)
            return name

    def record(self, result: dict) -> None:
        """A retrain that didn't make a new version, so the Models page can still show how it went."""
        with self._lock:
            history = self._writable()
            history["last_retrain"] = result
            self._save(history)

    def use(self, version: str) -> None:
        """KeyError for a version that doesn't exist or can't be loaded."""
        with self._lock:
            history = self._writable()
            if version != INSTALLED:
                found = self._find(history, version)
                if found is None:
                    raise KeyError(version)
                try:
                    self._load(version, self.folder / found["file"])
                except UNUSABLE as e:
                    raise KeyError(f"{version} can't be loaded: {e!r}") from None
            history["active"] = None if version == INSTALLED else version
            self._save(history)

    def _active_name(self, history: dict) -> str:
        """The version in use, as in_use() would find it, from an already-read history."""
        version = self._find(history, history["active"])
        if version is None:
            return INSTALLED
        try:
            self._load(version["version"], self.folder / version["file"])
        except UNUSABLE:
            return INSTALLED
        return version["version"]

    def _load(self, version: str, path: Path) -> InUse:
        from .verify import TunedCheck

        spec = json.loads(path.read_text())
        TunedCheck(spec)  # it must be a layer the assistant can use
        st = path.stat()
        name = str(path.relative_to(self._root)) if path.is_relative_to(self._root) else str(path)
        return InUse(version, name, spec, (str(path), st.st_mtime_ns, st.st_size))

    def _installed_hash(self) -> str:
        return hashlib.sha256((self._root / self.installed).read_bytes()).hexdigest()

    def _read(self) -> tuple[dict, str | None]:
        """(the history, None), or (an empty one, why the saved one can't be used)."""
        empty = {"installed": None, "active": None, "versions": [], "last_retrain": None}
        try:
            history = json.loads((self.folder / HISTORY).read_text())
            if not isinstance(history, dict) or not isinstance(history.get("versions"), list):
                raise TypeError("not a version history")
        except FileNotFoundError:
            return empty, None
        except (OSError, ValueError, TypeError) as e:
            return empty, (
                f"The version history couldn't be read ({e}), so TARS uses the installed check. The next "
                'retrain or "Use this" starts a new one; the old file is kept.'
            )
        if history.get("installed") != self._installed_hash():
            return empty, (
                f"The installed check ({self.installed}) has changed since these versions were trained, "
                "so TARS uses it. The next retrain starts a new history."
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


def check_versions(cfg: Config, root: Path) -> CheckVersions | None:
    """The versions of the learned layer config.toml installs; None for a plain phrase match."""
    return CheckVersions(root / cfg.learning.folder, root, cfg.wake.check_model) if cfg.wake.check_model else None


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
    folder = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)
