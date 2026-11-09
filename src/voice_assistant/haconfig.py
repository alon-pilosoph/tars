"""Home Assistant's own setup, versioned: its config folder as a git repository on this machine, so a change to the
house's setup (an integration added or removed, a device renamed, an automation edited) can be seen and undone.

What's kept is what Home Assistant is set up with: its YAML, its custom integrations, and .storage, where it keeps its
integrations, devices, entities and areas. Its logs, its history database and the files it rewrites every few seconds
are left out (IGNORED). .storage holds the integrations' passwords and tokens, and the keys Home Assistant signs its
logins with (whoever has them has the house, its lock included), so the repository is never pushed anywhere. It stays
on this machine, and goes in the backup's snapshots (backup.py) only as a bundle sealed with HA_CONFIG_KEY from .env
(AES-GCM), which never leaves the machine: the bucket holds nothing it could be opened with. Without the key, nothing
goes to the bucket. Keep a copy of the key somewhere else (a password manager): a new machine needs it to get the
history back.

Home Assistant's container runs as root and writes some files only root may read, so every git command runs as root
in a throwaway container of Home Assistant's own image, which has git: it works on the files whether Home Assistant is
running or not, and needs nothing installed beyond Docker.

Each backup pass commits whatever changed since the last (made in Home Assistant's own app, say). Restoring an earlier
version stops Home Assistant, puts the files back, records that as a new commit, and starts it again: nothing in the
history is ever rewritten. What a change did outside Home Assistant (a device unpaired, a cloud login revoked) isn't in
these files, and a restore can't bring it back.
"""

import base64
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

INSIDE = "/config"  # where the folder is, in the container
AUTHOR = ("TARS", "tars@localhost")
# Kept out of the history: what Home Assistant rewrites all the time, or makes again by itself.
IGNORED = """\
# Kept out of the history by TARS (haconfig.py): what Home Assistant rewrites all the time, or makes again itself.
*.log
*.log.*
*.log.fault
*.db
*.db-shm
*.db-wal
.ha_run.lock
.cache/
deps/
tts/
__pycache__/
.storage/core.restore_state
.storage/trace.saved_traces
.storage/bluetooth.passive_update_processor
.tars-restoring.bundle
"""
BUNDLE = "ha-config.bundle.sealed"  # its name in the backup's snapshots, and in the event log's folder till then
KEY_NAME = "HA_CONFIG_KEY"  # in .env: what the bundle is sealed with
SEALED = b"TARS sealed Home Assistant setup v1\n"  # a sealed bundle starts with this; it's checked as it's opened
NONCE_BYTES, KEY_BYTES = 12, 32
RESTORING = ".tars-restoring.bundle"  # where a bundle waits in the folder while the history is made from it
CHANGED_IN_HA = "Changes made in Home Assistant"  # a pass's commit: whatever changed since the last one
TIMEOUT_S = 60.0
FIELD, RECORD = "\x1f", "\x1e"  # between a commit's fields in `git log`, and before each commit


class HAConfigError(Exception):
    pass


def new_key() -> str:
    """A key for HA_CONFIG_KEY: 32 random bytes, as text."""
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode()


def _cipher(key: str):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = base64.urlsafe_b64decode(key.strip().encode())
    except ValueError:
        raw = b""
    if len(raw) != KEY_BYTES:
        raise HAConfigError(f"{KEY_NAME} isn't a key voice-assistant --ha-config-key made")
    return AESGCM(raw)


def seal(data: bytes, key: str) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return SEALED + nonce + _cipher(key).encrypt(nonce, data, SEALED)


def unseal(sealed: bytes, key: str) -> bytes:
    """What `seal` sealed; HAConfigError if it isn't one, or the key isn't the one it was sealed with."""
    from cryptography.exceptions import InvalidTag

    if not sealed.startswith(SEALED):
        raise HAConfigError("that isn't a sealed history of Home Assistant's setup")
    nonce = sealed[len(SEALED) : len(SEALED) + NONCE_BYTES]
    try:
        return _cipher(key).decrypt(nonce, sealed[len(SEALED) + NONCE_BYTES :], SEALED)
    except InvalidTag:
        raise HAConfigError(f"it won't open with this {KEY_NAME}: another key sealed it, or it's damaged") from None


@dataclass(frozen=True)
class Change:
    commit: str
    when: float  # seconds since the epoch
    message: str
    files: tuple[str, ...]


Run = Callable[..., subprocess.CompletedProcess]


class HAConfig:
    def __init__(self, folder: Path, container: str, run: Run = subprocess.run, git: Sequence[str] | None = None):
        """`folder`: Home Assistant's config folder on this machine. `container`: its Docker container, whose image
        runs git and which a restore stops and starts. `git`: the git command to use instead (tests)."""
        self.folder, self.container, self._run, self._git = folder, container, run, list(git) if git else None

    def _command(self) -> list[str]:
        if self._git:
            return self._git
        image = self._docker("inspect", "--format", "{{.Config.Image}}", self.container).stdout.strip()
        if not image:
            raise HAConfigError(f"Docker has no container called {self.container}")
        name, email = AUTHOR
        return [
            "docker", "run", "--rm", "--network", "none", "-v", f"{self.folder.resolve()}:{INSIDE}", "-w", INSIDE,
            "-e", f"GIT_AUTHOR_NAME={name}", "-e", f"GIT_AUTHOR_EMAIL={email}",
            "-e", f"GIT_COMMITTER_NAME={name}", "-e", f"GIT_COMMITTER_EMAIL={email}",
            "--entrypoint", "git", image, "-c", f"safe.directory={INSIDE}",
        ]  # fmt: skip

    def _docker(self, *args: str) -> subprocess.CompletedProcess:
        return self._call(["docker", *args])

    def _call(self, command: list[str], check: bool = True) -> subprocess.CompletedProcess:
        try:
            done = self._run(command, capture_output=True, timeout=TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HAConfigError(f"{command[0]} didn't run: {e}") from None
        if check and done.returncode:
            said = (done.stderr or b"").decode(errors="replace").strip().splitlines()
            raise HAConfigError(said[-1] if said else f"{' '.join(command[:2])} failed ({done.returncode})")
        return done

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._call([*self._command(), *args], check=check)

    def ready(self) -> bool:
        return (self.folder / ".git").is_dir()

    def start(self) -> bool:
        """Makes the folder a repository, with what's there now as its first commit, if it isn't one yet. Whether it
        made one."""
        if not self.folder.is_dir():
            raise HAConfigError(f"there's no folder {self.folder}")
        if self.ready():
            return False
        (self.folder / ".gitignore").write_text(IGNORED)
        self.git("init", "-q", "-b", "main")
        self.commit("Home Assistant's setup as it was when TARS started keeping its history")
        return True

    def commit(self, message: str) -> str | None:
        """Commits whatever changed, with `message`. The new commit's id, or None if nothing changed."""
        self.git("add", "-A")
        if not self.git("diff", "--cached", "--quiet", check=False).returncode:
            return None
        self.git("commit", "-q", "-m", message)
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.decode().strip()

    def history(self, count: int = 20) -> list[Change]:
        """The latest `count` changes, newest first, each with the files it touched."""
        out = self.git("log", f"-{count}", f"--format={RECORD}%H{FIELD}%ct{FIELD}%s", "--name-only").stdout.decode()
        changes = []
        for record in filter(None, out.split(RECORD)):
            head, *files = record.strip("\n").split("\n")
            commit, when, message = head.split(FIELD, 2)
            changes.append(Change(commit, float(when), message, tuple(f for f in files if f)))
        return changes

    def bundle(self) -> bytes:
        """The whole history, as one file `git clone` makes a repository from again."""
        return self.git("bundle", "create", "-", "--all").stdout

    def clone(self, bundle: bytes) -> None:
        """The history and the files of its latest change, from a bundle, into a folder that's empty or not there yet
        (a new machine, before Home Assistant first starts)."""
        if self.folder.exists() and any(self.folder.iterdir()):
            raise HAConfigError(f"{self.folder} isn't empty: move it away first")
        self.folder.mkdir(parents=True, exist_ok=True)
        waiting = self.folder / RESTORING
        waiting.write_bytes(bundle)
        try:
            self.git("init", "-q", "-b", "main")
            self.git("fetch", "-q", "--update-head-ok", RESTORING, "main:main")
            self.git("reset", "-q", "--hard")
        finally:
            waiting.unlink(missing_ok=True)

    def restore(self, commit: str) -> str:
        """Home Assistant's setup as it was at `commit`: whatever changed since the last commit is committed first,
        Home Assistant is stopped (it writes .storage as it stops, which would undo the restore), the files are put
        back, that's committed, and Home Assistant starts again, whatever happened. The new commit's id."""
        wanted = self.git("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", check=False)
        if wanted.returncode:
            raise HAConfigError(f"there's no change {commit!r} in Home Assistant's history")
        full = wanted.stdout.decode().strip()
        said = self.git("log", "-1", "--format=%s", full).stdout.decode().strip()
        self.commit(CHANGED_IN_HA)
        self._docker("stop", self.container)
        try:
            self.git("restore", f"--source={full}", "--staged", "--worktree", "--", ".")
            return self.commit(f"Restored to {full[:8]} ({said})") or full
        finally:
            self._docker("start", self.container)
