"""TARS keeping itself up to date: what's merged into its branch is installed on this machine by itself, and taken
back if it doesn't work (`voice-assistant --update`, every ten minutes from its timer; docs/deployment.md#updates).

A pass:

1. Fetches the branch ([updates] branch, main). Nothing new: done.
2. Decides whether it may install by itself. Not when the update changes the database (a new step in
   store.MIGRATIONS), which code alone can't take back, nor the system (packages in deploy/pi-packages.txt not
   installed, a device rule changed; done as root by deploy/tars-system, the one thing TARS may run as root): TARS's
   maintainers are asked in their chats, with an Install button, and it waits for a tap. Only a change to
   deploy/tars-system itself is never installed by an update (TARS mustn't rewrite its own root script): the
   maintainers are told to run deploy/install-pi.sh themselves.
3. Waits for a quiet moment: nobody's spoken to TARS for a few minutes and no errand is running.
4. Keeps what it'd need to go back: the database (through sqlite's backup API) and config.local.toml.
5. Moves the checkout forward and runs deploy/install-pi.sh, which installs the packages, upgrades the database and
   restarts the services; then `--check` must pass (nothing ✗) and each service must be running a minute later.
6. If any of it fails, goes back: the services stopped, the checkout back where it was, the database and config as
   they were kept, the installer run again. That version isn't tried again; a newer one is.
7. Tells the maintainers either way, in their chats, in plain words.

The maintainers are told straight through Telegram's API, not through the Telegram service, which may be the very
thing restarting.
"""

import ast
import json
import os
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import LOCAL_CONFIG, Config
from .store import DB_FILE, MIGRATIONS

STATE = "update.json"  # in the event log's folder: what's been asked, and what failed
KEPT = "updates"  # in the event log's folder: what a pass keeps to go back to
QUIET_S = 180.0  # nobody spoke to TARS for this long
SETTLE_S = 60.0  # after installing, how long every service must keep running
UNITS = ("voice-assistant", "voice-assistant-telegram", "voice-assistant-web")
INSTALL_TIMEOUT_S = 1800.0
TAP = "update:"  # what an Install button sends back, before the version
HELPER = Path("/usr/local/sbin/tars-system")  # deploy/tars-system, installed root-owned by install-pi.sh
UV = Path.home() / ".local" / "bin" / "uv"  # where install-pi.sh puts it; a service's PATH doesn't have it
SILENT = {
    "TARS_SILENT_CHECK": "1"
}  # install-pi.sh's check: the voice made, not said (it may be the middle of the night)


@dataclass
class Plan:
    old: str
    new: str
    titles: list[str]
    migrates: bool
    system: list[str] = field(default_factory=list)  # what it changes in the system, through deploy/tars-system
    needs_person: list[str] = field(default_factory=list)  # why it can't be installed by TARS at all


@dataclass
class State:
    asked: str = ""  # the version the maintainers were asked to approve
    approved: str = ""  # the version a maintainer tapped Install for
    told_sudo: str = ""  # the version they were told needs installing by hand
    failed: str = ""  # a version that was installed, failed and taken back: not tried again

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            return cls(**json.loads(path.read_text()))
        except OSError, ValueError, TypeError:
            return cls()

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self)))


class Updater:
    """`tell(text, buttons)`: posts to each maintainer's chat, with buttons [(label, what a tap sends)] if any.
    `run`: how commands run. `sleep`, `clock`: time, replaced in the tests."""

    def __init__(
        self,
        root: Path,
        cfg: Config,
        tell: Callable[[str, list[tuple[str, str]]], None],
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        unit_state: Callable[[str], str | None] | None = None,
    ):
        from .check import systemd_state

        self._root, self._cfg, self._tell, self._run = root, cfg, tell, run
        self._sleep, self._clock = sleep, clock
        self._unit_state = unit_state or systemd_state
        self._folder = root / cfg.learning.folder
        self._state_path = self._folder / STATE

    # Deciding.

    def plan(self) -> Plan | None:
        """What's new on the branch, or None if nothing is."""
        branch = self._cfg.updates.branch
        self._git("fetch", "-q", "origin", branch)
        old, new = self._git("rev-parse", "HEAD"), self._git("rev-parse", f"origin/{branch}")
        if old == new:
            return None
        if self._git("merge-base", "--is-ancestor", "HEAD", new, check=False) is None:
            raise UpdateError(f"this checkout has changes of its own that origin/{branch} doesn't: update it by hand")
        titles = self._git("log", "--first-parent", "--format=%s", f"{old}..{new}").splitlines()
        return Plan(old, new, titles, self._migrates(new), self._system(new), self._needs_person(new))

    def _migrates(self, new: str) -> bool:
        """Whether `new` adds a step to the database's upgrades."""
        source = self._git("show", f"{new}:src/voice_assistant/store.py")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "MIGRATIONS" for t in node.targets):
                return isinstance(node.value, ast.List) and len(node.value.elts) > len(MIGRATIONS)
        return False

    def _needs_person(self, new: str) -> list[str]:
        """What in `new` only a person may install: deploy/tars-system itself changed, or never installed."""
        wanted = self._git("show", f"{new}:deploy/tars-system", check=False)
        if wanted is None or not HELPER.parent.exists():
            return []  # not a Pi set up by install-pi.sh (a dev machine, the tests)
        if not HELPER.exists():
            return ["the system helper (deploy/tars-system), not installed yet"]
        if HELPER.read_text().strip() != wanted.strip():
            return ["a change to the system helper (deploy/tars-system)"]
        return []

    def _system(self, new: str) -> list[str]:
        """What in `new` changes the system, through deploy/tars-system: packages not installed, device rules
        changed."""
        why = []
        packages = self._git("show", f"{new}:deploy/pi-packages.txt", check=False) or ""
        wanted = [p.strip() for p in packages.splitlines() if p.strip() and not p.lstrip().startswith("#")]
        if wanted and shutil.which("dpkg"):
            missing = [p for p in wanted if self._call(["dpkg", "-s", p]).returncode != 0]
            if missing:
                why.append(f"the system: new packages ({', '.join(missing)})")
        for rules in ("99-tars-speakerphone.rules", "99-tars-ir.rules"):
            text = self._git("show", f"{new}:deploy/{rules}", check=False)
            installed = Path("/etc/udev/rules.d") / rules
            if (
                text is not None
                and installed.parent.exists()
                and (not installed.exists() or installed.read_text().strip() != text.strip())
            ):
                why.append(f"the system: a device rule ({rules})")
        return why

    # A pass.

    def run_once(self) -> str:
        """One pass, as the timer runs it: what happened, for the log."""
        state = State.load(self._state_path)
        try:
            plan = self.plan()
        except UpdateError as e:
            return f"not updating: {e}"
        if plan is None:
            return "up to date"
        what = _list(plan.titles)
        if plan.new == state.failed:
            return f"not updating: {plan.new[:8]} failed before, and nothing newer has come"
        if plan.needs_person:
            if state.told_sudo != plan.new:
                self._tell(
                    f"There's an update for me ({what}) with {' and '.join(plan.needs_person)}, which I don't install "
                    "myself, since it runs as root. To install it: cd ~/voice-assistant && git pull && "
                    "deploy/install-pi.sh",
                    [],
                )
                state.told_sudo = plan.new
                state.save(self._state_path)
            return "waiting: it needs a person"
        if (plan.migrates or plan.system) and state.approved != plan.new:
            if state.asked != plan.new:
                changes = ["my database, which I can't take back by myself if it goes wrong (I'll keep a copy)"]
                changes = changes * plan.migrates + plan.system
                self._tell(
                    f"There's an update for me ({what}). It changes {' and '.join(changes)}, so I'm asking first.",
                    [("Install", f"{TAP}{plan.new}")],
                )
                state.asked = plan.new
                state.save(self._state_path)
            return "waiting for a maintainer to tap Install"
        if not self._quiet():
            return "waiting for a quiet moment"
        return self._install(plan, state)

    def approve(self, version: str) -> bool:
        """A maintainer tapped Install for `version`: it's installed on the next pass. Whether it's the one asked."""
        state = State.load(self._state_path)
        if not version or version != state.asked:
            return False
        state.approved = version
        state.save(self._state_path)
        return True

    def _quiet(self) -> bool:
        events = self._folder / DB_FILE
        if not events.exists():
            return True
        with closing(sqlite3.connect(f"{events.resolve().as_uri()}?mode=ro", uri=True)) as db:
            last = db.execute("SELECT MAX(ts) FROM turns").fetchone()[0] or 0.0
            busy = db.execute("SELECT COUNT(*) FROM errands WHERE status='running'").fetchone()[0]
        return self._clock() - last >= QUIET_S and not busy

    def _install(self, plan: Plan, state: State) -> str:
        kept = self._keep(plan)
        self._git("merge", "-q", "--ff-only", plan.new)
        ok, why = self._installed()
        if ok:
            state.failed = ""
            state.save(self._state_path)
            self._tell(f"I've updated myself: {_list(plan.titles)}. Everything checks out.", [])
            return f"updated to {plan.new[:8]}"
        self._go_back(plan, kept)
        state.failed = plan.new
        state.save(self._state_path)
        self._tell(
            f"I tried to update myself ({_list(plan.titles)}), but {why}, so I went back to how I was. I won't try "
            "that version again; a newer one, I will.",
            [],
        )
        return f"went back to {plan.old[:8]}: {why}"

    def _installed(self) -> tuple[bool, str]:
        done = self._call(["deploy/install-pi.sh"], timeout=INSTALL_TIMEOUT_S, env=SILENT)
        if done.returncode:
            return False, f"the installer stopped ({_last_line(done)})"
        uv = str(UV) if UV.exists() else "uv"
        checked = self._call([uv, "run", "--frozen", "voice-assistant", "--check", "--silent"], timeout=600)
        if checked.returncode:
            failed = [line.strip() for line in (checked.stdout or "").splitlines() if line.lstrip().startswith("✗")]
            return False, f"its check failed ({'; '.join(failed[:3]) or _last_line(checked)})"
        self._sleep(SETTLE_S)
        stopped = [u for u in UNITS if self._unit_state(u) not in (None, "active")]
        if stopped:
            return False, f"{', '.join(stopped)} didn't keep running"
        return True, ""

    def _keep(self, plan: Plan) -> Path:
        """The database and config.local.toml as they are, before the update."""
        kept = self._folder / KEPT / plan.old[:12]
        kept.mkdir(parents=True, exist_ok=True)
        if (self._folder / DB_FILE).exists():
            with (
                closing(sqlite3.connect(f"{(self._folder / DB_FILE).resolve().as_uri()}?mode=ro", uri=True)) as src,
                closing(sqlite3.connect(kept / DB_FILE)) as copy,
            ):
                src.backup(copy)
        if (self._root / LOCAL_CONFIG).exists():
            shutil.copy2(self._root / LOCAL_CONFIG, kept / LOCAL_CONFIG)
        return kept

    def _go_back(self, plan: Plan, kept: Path) -> None:
        self._call(["systemctl", "--user", "stop", *UNITS])
        self._git("reset", "-q", "--hard", plan.old)
        if plan.migrates and (kept / DB_FILE).exists():
            for leftover in ("-wal", "-shm"):
                (self._folder / f"{DB_FILE}{leftover}").unlink(missing_ok=True)
            shutil.copy2(kept / DB_FILE, self._folder / DB_FILE)
        if (kept / LOCAL_CONFIG).exists():
            shutil.copy2(kept / LOCAL_CONFIG, self._root / LOCAL_CONFIG)
        self._call(["deploy/install-pi.sh"], timeout=INSTALL_TIMEOUT_S, env=SILENT)

    # Commands.

    def _call(
        self, command: list[str], timeout: float = 60.0, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        try:
            return self._run(
                command, cwd=self._root, capture_output=True, text=True, timeout=timeout, check=False,
                stdin=subprocess.DEVNULL, env={**os.environ, **env} if env else None,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired) as e:
            return subprocess.CompletedProcess(command, 1, "", str(e))

    def _git(self, *args: str, check: bool = True) -> str | None:
        done = self._call(["git", *args])
        if done.returncode:
            if check:
                raise UpdateError(f"git {args[0]} failed: {_last_line(done)}")
            return None
        return done.stdout.strip()


class UpdateError(Exception):
    pass


def _list(titles: list[str]) -> str:
    """A PR's title each, without its number: "Maintainers: ...; Wake: ..."."""
    shown = [t.rsplit(" (#", 1)[0] for t in titles[:3]]
    more = f", and {len(titles) - 3} more" if len(titles) > 3 else ""
    return "; ".join(shown) + more


def _last_line(done: subprocess.CompletedProcess) -> str:
    lines = [line for line in f"{done.stdout or ''}\n{done.stderr or ''}".splitlines() if line.strip()]
    return lines[-1].strip() if lines else f"exit {done.returncode}"


def tap_listener(updater: Updater, maintainers: list[int], bot, start: Callable[[], None]):
    """For the Telegram service (TelegramTars.listen_first): a maintainer's tap on an update's Install button approves
    that version and starts installing it now (`start`), and the buttons go. Another's tap, or a stale one, is only
    acknowledged. `maintainers`: their chat ids."""

    def take(update: dict) -> bool:
        tap = update.get("callback_query") or {}
        data = str(tap.get("data", ""))
        if not data.startswith(TAP):
            return False
        try:
            bot.answer_tap(tap["id"])
        except Exception as e:  # noqa: BLE001 - only the button's spinner
            print(f"(updates: couldn't answer the tap: {e})")
        chat = (tap.get("from") or {}).get("id")
        message = tap.get("message") or {}
        if chat not in maintainers or not updater.approve(data.removeprefix(TAP)):
            return True
        try:
            bot.drop_buttons(chat, message.get("message_id"))
            bot.send(chat, "Installing it now. I'll tell you how it went.")
        except Exception as e:  # noqa: BLE001 - the update goes ahead whether or not this is said
            print(f"(updates: couldn't say it's installing: {e})")
        start()
        return True

    return take
