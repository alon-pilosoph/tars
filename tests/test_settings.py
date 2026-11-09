"""TARS's own settings, changed by asking: only what's listed, within bounds, written into config.local.toml with
everything else left as it was, checked, logged and applied by a restart a moment later."""

import shutil
import subprocess
from pathlib import Path

import pytest

from voice_assistant.config import load_config
from voice_assistant.home import Request
from voice_assistant.settings import ADJUSTABLE, Settings, SettingsError, set_line
from voice_assistant.store import Store

LOCAL = """# This house's own settings.
[llm]
groq_model = ""   # Groq off on the Pi
humor = 75

[home]
url = "http://ha.local:8123"

[[home.devices]]
name = "boiler"
entity = "switch.boiler"
"""


@pytest.fixture
def root(tmp_path):
    shutil.copy(Path(__file__).parents[1] / "config.toml", tmp_path / "config.toml")
    (tmp_path / "config.local.toml").write_text(LOCAL)
    return tmp_path


def settings(root, run=None):
    calls = []

    def ran(command, **kw):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    s = Settings(root, Store(root / "events"), run=run or ran, clock=lambda: 1000.0)
    s.calls = calls
    return s


def test_a_line_is_set_where_it_is_added_under_its_section_or_its_section_is_made():
    changed = set_line(LOCAL, "llm", "humor", 40, "Stacey")
    assert "# before TARS changed it: humor = 75\nhumor = 40  # set by TARS for Stacey" in changed
    assert changed.count("\n") == LOCAL.count("\n") + 1 and 'groq_model = ""   # Groq off on the Pi' in changed
    again = set_line(changed, "llm", "humor", 20, "Alon")  # the first, a person's line, is the one kept
    assert again.count("# before TARS changed it") == 1 and "humor = 20  # set by TARS for Alon" in again
    assert "# before TARS changed it: humor = 75\n" in again
    added = set_line(LOCAL, "llm", "memory_minutes", 30.0, None)
    assert added.index("memory_minutes = 30.0") < added.index("[[home.devices]]")
    made = set_line(LOCAL, "recorder", "greet", "never", "Alon")
    assert made.startswith(LOCAL.rstrip("\n")) and made.endswith('[recorder]\ngreet = "never"  # set by TARS for Alon, '
                                                                    + made.rsplit(", ", 1)[1])  # fmt: skip


def test_a_change_is_kept_checked_logged_and_applied_by_a_restart(root, monkeypatch):
    monkeypatch.setattr("voice_assistant.settings.shutil.which", lambda name: f"/usr/bin/{name}")
    s = settings(root)
    said = s.set("llm.humor", "40", "Stacey")
    assert said.startswith("done: llm.humor is 40 (it was 75); TARS restarts in 8 seconds")
    assert load_config(root / "config.toml").llm.humor == 40
    assert s.calls == [["systemd-run", "--user", "--on-active=8", "systemctl", "--user", "restart",
                        "voice-assistant", "voice-assistant-telegram", "voice-assistant-web"]]  # fmt: skip
    assert "llm.humor: 75 -> 40, for Stacey" in s.listing()
    assert s.set("llm.humor", "40", "Stacey") == "llm.humor is already 40: nothing to change"


def test_only_whats_listed_changes_and_within_its_bounds(root):
    s = settings(root)
    for name, value, wrong in (
        ("home.unlock_chats", "Stacey", "only these can change"),
        ("wake.threshold", "0.05", "goes from 0.2 to 0.6"),  # it can't make itself deaf
        ("llm.humor", "lots", "is a number"),
        ("recorder.greet", "sometimes", "one of pause, always, never"),
        ("tts.instructions", "x" * 400, "at most 300 characters"),
    ):
        with pytest.raises(SettingsError, match=wrong):
            s.set(name, value, "Alon")
    assert (root / "config.local.toml").read_text() == LOCAL
    assert "home" not in {a.section for a in ADJUSTABLE.values()}  # who may unlock, or set up the house: never here


def test_a_change_the_config_wont_load_with_is_taken_back(root, monkeypatch):
    s = settings(root)
    monkeypatch.setattr("voice_assistant.settings.load_config", _fails_second_time())
    with pytest.raises(SettingsError, match="wouldn't load with that, so nothing changed"):
        s.set("llm.humor", "40", "Alon")
    assert (root / "config.local.toml").read_text() == LOCAL


def test_without_systemd_it_waits_for_the_next_start(root, monkeypatch):
    monkeypatch.setattr("voice_assistant.settings.shutil.which", lambda name: None)
    assert settings(root).set("llm.home_city", "Ra'anana", "Alon").endswith("it takes effect when TARS next starts")
    assert load_config(root / "config.toml").llm.home_city == "Ra'anana"


def test_the_tars_tool_changes_one_only_for_someone_known_or_at_home_and_never_after_a_search(root, monkeypatch):
    from voice_assistant.events import EventLog
    from voice_assistant.myself import Myself

    monkeypatch.setattr("voice_assistant.settings.shutil.which", lambda name: None)
    tars = Myself(load_config(root / "config.toml"), root, EventLog(root / "events"))

    def ask(request, **args):
        keys = ("query", "since", "until", "why", "value")
        return tars.call({"action": "set", **dict.fromkeys(keys), **args}, request)

    assert ask(Request(by_chat=True), query="llm.humor", value="10").startswith(
        "refused: TARS changes its settings only"
    )
    searched = Request(speaker="Alon", by_chat=True, read_the_web=True)
    assert ask(searched, query="llm.humor", value="10").startswith("refused: this reply searched the web")
    assert ask(Request(by_voice=True), query="llm.humor", value="10").startswith("done: llm.humor is 10")
    assert ask(Request(speaker="Alon"), query="home.unlock_voices", value="x").startswith("error: only these")
    assert "llm.humor = 10" in tars.call({"action": "settings"}, Request())


def _fails_second_time():
    real, calls = load_config, []

    def load(path, *a, **kw):
        calls.append(path)
        if len(calls) == 2:
            raise ValueError("broken")
        return real(path, *a, **kw)

    return load
