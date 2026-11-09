"""TARS updating itself, against a real git history: installed at a quiet moment and checked, a database change
waiting for a maintainer's tap, sudo left to a person, and anything that fails taken back, the database and config
with it, and not tried again."""

import dataclasses
import subprocess
import time
from pathlib import Path

import pytest

from voice_assistant.config import HouseholdConfig, UpdatesConfig, load_config
from voice_assistant.store import MIGRATIONS, Store
from voice_assistant.updater import TAP, State, Updater, tap_listener

GIT = ["-c", "user.name=T", "-c", "user.email=t@x"]


def store_py(steps: int) -> str:
    return "MIGRATIONS = [" + "[], " * steps + "]\n"


class Pi:
    """A checkout cloned from a bare origin, with TARS's database in it; the installer, the check and systemd are
    stood in for, and every command is kept."""

    def __init__(self, tmp: Path):
        self.origin, self.root, self.work = tmp / "origin.git", tmp / "pi", tmp / "dev"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.work)], check=True)
        self.commit("TARS (#1)", store_py(len(MIGRATIONS)))
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.root)], check=True)
        (self.root / "config.local.toml").write_text("# this house\n")
        shipped = load_config(Path(__file__).parents[1] / "config.toml", local=False)
        self.cfg = dataclasses.replace(
            shipped, updates=UpdatesConfig(enabled=True), household=HouseholdConfig(maintainers=["Alon"])
        )
        self.store = Store(self.root / self.cfg.learning.folder)
        self.told, self.commands, self.check_fails, self.now = [], [], False, time.time() + 3600
        self.units = "active"

    def commit(self, title: str, store: str | None = None, files: dict | None = None) -> None:
        if store is not None:
            path = self.work / "src" / "voice_assistant" / "store.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(store)
        for name, text in (files or {}).items():
            (self.work / name).parent.mkdir(parents=True, exist_ok=True)
            (self.work / name).write_text(text)
        subprocess.run(["git", "-C", str(self.work), *GIT, "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.work), *GIT, "commit", "-q", "--allow-empty", "-m", title], check=True)
        subprocess.run(["git", "-C", str(self.work), "push", "-q", "origin", "main"], check=True)

    def run(self, command, **kw):
        self.commands.append(command)
        if command[0] == "git":
            return subprocess.run(command, **kw)  # noqa: PLW1510 - the updater says check
        if command[0] == "deploy/install-pi.sh":
            return subprocess.CompletedProcess(command, 0, "installed", "")
        if "--check" in command:
            out = "✗ Home Assistant: didn't answer" if self.check_fails else "Everything works."
            return subprocess.CompletedProcess(command, int(self.check_fails), out, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def updater(self) -> Updater:
        return Updater(
            self.root, self.cfg, lambda text, buttons: self.told.append((text, buttons)), run=self.run,
            sleep=lambda s: None, clock=lambda: self.now, unit_state=lambda unit: self.units,
        )  # fmt: skip

    def head(self) -> str:
        done = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        return done.stdout


@pytest.fixture
def pi(tmp_path):
    return Pi(tmp_path)


def test_nothing_new_is_nothing_done(pi):
    assert pi.updater().run_once() == "up to date"
    assert pi.told == []


def test_whats_merged_is_installed_checked_silently_and_said(pi):
    pi.commit("Maintainers: wishes reach their chats (#102)")
    said = pi.updater().run_once()
    assert said.startswith("updated to ") and pi.head().startswith(said.split()[-1])
    assert ["deploy/install-pi.sh"] in pi.commands
    assert any("--check" in c and "--silent" in c for c in pi.commands)  # never says "TARS is ready" at night
    assert pi.told == [("I've updated myself: Maintainers: wishes reach their chats. Everything checks out.", [])]


def test_it_waits_while_someone_is_talking_to_tars(pi):
    pi.store.write("INSERT INTO conversations (started) VALUES (?)", (pi.now,))
    pi.store.write("INSERT INTO turns (conversation_id, ts, role, text) VALUES (1, ?, 'person', 'hi')", (pi.now - 30,))
    pi.commit("Something new (#103)")
    before = pi.head()
    assert pi.updater().run_once() == "waiting for a quiet moment" and pi.head() == before


def test_a_database_change_waits_for_a_maintainers_tap(pi):
    pi.commit("Wishes in the database (#104)", store_py(len(MIGRATIONS) + 1))
    updater = pi.updater()
    assert updater.run_once() == "waiting for a maintainer to tap Install"
    ((text, buttons),) = pi.told
    assert "changes my database" in text and buttons[0][0] == "Install" and buttons[0][1].startswith(TAP)
    assert updater.run_once() == "waiting for a maintainer to tap Install" and len(pi.told) == 1  # asked once
    assert not updater.approve("not-that-one")
    assert updater.approve(buttons[0][1].removeprefix(TAP))
    assert updater.run_once().startswith("updated to ")


def test_a_failed_update_goes_back_with_its_database_and_config_and_isnt_tried_again(pi):
    pi.store.write("INSERT INTO meta (key, value) VALUES ('before', 'the update')")
    pi.commit("A broken one (#105)", store_py(len(MIGRATIONS) + 1))
    updater = pi.updater()
    updater.run_once()
    updater.approve(pi.told[0][1][0][1].removeprefix(TAP))
    before = pi.head()
    # The update changes the database and the config, then fails its check.
    pi.check_fails = True
    real_install = pi.run

    def install_then_break(command, **kw):
        if command[0] == "deploy/install-pi.sh" and not pi.commands.count(["deploy/install-pi.sh"]):
            pi.store.write("INSERT INTO meta (key, value) VALUES ('after', 'the update')")
            (pi.root / "config.local.toml").write_text("# changed by the update\n")
        return real_install(command, **kw)

    pi.run = install_then_break
    said = updater.run_once()
    assert said.startswith("went back to ") and "its check failed (✗ Home Assistant: didn't answer)" in said
    assert pi.head() == before
    assert Store(pi.root / pi.cfg.learning.folder).rows("SELECT key FROM meta WHERE key IN ('before', 'after')") == [
        {"key": "before"}
    ]
    assert (pi.root / "config.local.toml").read_text() == "# this house\n"
    assert ["systemctl", "--user", "stop", "voice-assistant", "voice-assistant-telegram", "voice-assistant-web"] in (
        pi.commands
    )
    assert "went back to how I was" in pi.told[-1][0]
    assert updater.run_once().startswith("not updating: ") and "failed before" in updater.run_once()


def test_a_service_that_doesnt_keep_running_counts_as_failing(pi):
    pi.commit("Starts, then falls over (#106)")
    pi.units = "failed"
    assert "didn't keep running" in pi.updater().run_once()


def test_a_system_change_waits_for_a_tap_like_a_database_change(pi, monkeypatch):
    monkeypatch.setattr("voice_assistant.updater.shutil.which", lambda name: f"/usr/bin/{name}")
    real = pi.run

    def no_package(command, **kw):
        if command[:2] == ["dpkg", "-s"]:
            return subprocess.CompletedProcess(command, 1, "", "not installed")
        return real(command, **kw)

    pi.run = no_package
    pi.commit("A new system package (#107)", files={"deploy/pi-packages.txt": "# audio\nsox\n"})
    before = pi.head()
    updater = pi.updater()
    assert updater.run_once() == "waiting for a maintainer to tap Install"
    ((text, buttons),) = pi.told
    assert "It changes the system: new packages (sox), so I'm asking first." in text and buttons[0][0] == "Install"
    assert pi.head() == before
    updater.approve(buttons[0][1].removeprefix(TAP))
    assert updater.run_once().startswith("updated to ")  # deploy/tars-system installs it, with no password


def test_a_change_to_the_root_helper_itself_is_left_for_a_person(pi, tmp_path, monkeypatch):
    helper = tmp_path / "sbin" / "tars-system"
    helper.parent.mkdir()
    helper.write_text("#!/bin/bash\n# as installed\n")
    monkeypatch.setattr("voice_assistant.updater.HELPER", helper)
    pi.commit("The helper does more (#108)", files={"deploy/tars-system": "#!/bin/bash\n# changed\n"})
    before = pi.head()
    assert pi.updater().run_once() == "waiting: it needs a person"
    assert "a change to the system helper (deploy/tars-system)" in pi.told[0][0] and pi.told[0][1] == []
    assert "deploy/install-pi.sh" in pi.told[0][0] and pi.head() == before
    pi.updater().run_once()
    assert len(pi.told) == 1  # told once
    helper.write_text("#!/bin/bash\n# changed\n")  # a person ran install-pi.sh
    assert pi.updater().run_once().startswith("updated to ")


def test_only_a_maintainers_tap_installs_and_the_errands_leave_it_alone(pi):
    pi.commit("Wishes in the database (#104)", store_py(len(MIGRATIONS) + 1))
    updater = pi.updater()
    updater.run_once()
    version = pi.told[0][1][0][1]

    class Bot:
        def __init__(self):
            self.said = []

        def answer_tap(self, tap_id):
            pass

        def drop_buttons(self, chat, message):
            self.said.append(("dropped", chat))

        def send(self, chat, text):
            self.said.append((chat, text))

    bot, started = Bot(), []
    take = tap_listener(updater, [1], bot, lambda: started.append(True))
    assert not take({"message": {"text": "hi"}})  # not a tap: the conversation's
    assert take({"callback_query": {"id": "t", "data": version, "from": {"id": 2}, "message": {"message_id": 9}}})
    assert started == [] and State.load(updater._state_path).approved == ""  # not a maintainer
    assert take({"callback_query": {"id": "t", "data": version, "from": {"id": 1}, "message": {"message_id": 9}}})
    assert started == [True] and ("dropped", 1) in bot.said and State.load(updater._state_path).approved

    from voice_assistant.telegram_errands import TelegramErrands

    errands = TelegramErrands.__new__(TelegramErrands)
    assert TelegramErrands._take(errands, {"callback_query": {"data": version}}) is False


def test_the_scripts_parse_and_the_root_helper_refuses_rules_that_run_programs():
    deploy = Path(__file__).parents[1] / "deploy"
    for script in ("tars-system", "install-pi.sh"):
        subprocess.run(["bash", "-n", str(deploy / script)], check=True)
    helper = (deploy / "tars-system").read_text()
    assert "RUN|PROGRAM|IMPORT" in helper and "^[a-z0-9][a-z0-9.+-]+$" in helper
    for rules in ("99-tars-speakerphone.rules", "99-tars-ir.rules"):  # TARS's own rules pass its own check
        lines = [line for line in (deploy / rules).read_text().splitlines() if not line.lstrip().startswith("#")]
        assert not any(word in line for line in lines for word in ("RUN", "PROGRAM", "IMPORT"))
