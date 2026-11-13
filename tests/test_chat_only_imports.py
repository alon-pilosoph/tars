"""A chat-only home ([audio] enabled = false) runs without the optional extra "voice": what Telegram and the web UI
load never imports a voice package. Checked in a fresh Python where those packages can't be imported at all, as on a
server without them (or without PortAudio), so a top-level import added anywhere on the chat side fails here first.
CI also runs this file in an environment synced with no extras."""

import subprocess
import sys
import textwrap
from pathlib import Path

from voice_assistant.wiring import VOICE_PACKAGES

REPO = Path(__file__).parents[1]

CHAT_SIDE = textwrap.dedent(
    """
    import importlib.abc, sys, tempfile
    from pathlib import Path

    VOICE = {voice!r}

    class NoVoice(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in VOICE:
                raise ImportError(f"{{name}} is a voice package: the chat side imported it")
            return None

    sys.meta_path.insert(0, NoVoice())
    root = Path(tempfile.mkdtemp())
    (root / "config.toml").write_text(Path({config!r}).read_text())
    (root / "config.local.toml").write_text("[audio]\\nenabled = false\\n")

    from voice_assistant.config import load_config
    from voice_assistant import entrypoints, telegram, webui, wiring
    from voice_assistant.conversations import ConversationLog

    cfg = load_config(root / "config.toml")
    household = wiring.Household(cfg, root)
    for part in ("events", "reminders", "skills", "feeds", "myself", "presence", "wishes", "speaker_id"):
        getattr(household, part)
    webui.create_app(household.events, reminders=household.reminders, aloud=False)
    telegram.TelegramTars(object(), {{}}, lambda: None, ConversationLog(household.events), aloud=False)
    from voice_assistant.check import service_checks
    service_checks(cfg, root)
    print("chat side loaded:", sorted(m for m in VOICE if m in sys.modules))
    """
)


def test_the_chat_side_never_imports_a_voice_package():
    script = CHAT_SIDE.format(voice=VOICE_PACKAGES + ("pymicro_features",), config=str(REPO / "config.toml"))
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().endswith("chat side loaded: []")


def test_without_the_voice_extra_a_voice_command_says_how_to_get_it(monkeypatch):
    import importlib.util

    import pytest

    from voice_assistant.config import ConfigError
    from voice_assistant.wiring import need_voice

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "sounddevice" else real(name, *a))
    with pytest.raises(ConfigError, match=r'extra "voice" \([^)]*sounddevice[^)]*missing\).*uv sync --all-extras'):
        need_voice()
