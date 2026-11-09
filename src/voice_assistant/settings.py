"""TARS's own settings, changed by asking it: "be a bit less jokey", "you cut me off: wait longer before answering",
"remember our chats for a day". The tars tool's settings and set actions (myself.py) come here.

Only what's listed in ADJUSTABLE can change, each within its bounds, so no request makes TARS deaf (wake threshold),
mute, or less safe: who may unlock, who may change Home Assistant's setup, the keys and the models aren't here. A change
is written into this machine's config.local.toml (its own line, the rest of the file as it was), checked by loading the
whole config before it's kept, logged with who asked and what it was before (so it can be put back), and takes effect
when TARS's services restart a few seconds later, once what it's saying is said.
"""

import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import LOCAL_CONFIG, ConfigError, load_config

RESTART_AFTER_S = 8  # long enough for the reply that says it's done to be said, or posted
UNITS = ("voice-assistant", "voice-assistant-telegram", "voice-assistant-web")
MAX_TEXT = 300


@dataclass(frozen=True)
class Adjustable:
    section: str
    key: str
    kind: type
    what: str  # what it does, for the model and the person
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return f"{self.section}.{self.key}"


ADJUSTABLE = {
    a.name: a
    for a in (
        Adjustable("llm", "humor", int, "how jokey TARS is, in percent", 0, 100),
        Adjustable("llm", "memory_minutes", float, "how long a spoken conversation is remembered once quiet", 1, 240),
        Adjustable("telegram", "memory_minutes", float, "how long a chat is one conversation once quiet", 5, 1440),
        Adjustable("recorder", "follow_up_s", float, "how long TARS listens for more without the wake word", 0, 10),
        Adjustable("recorder", "end_silence_s", float, "how long a pause ends someone's turn, in seconds", 0.4, 2.5),
        Adjustable("recorder", "max_pause_s", float, "the longest pause mid-sentence TARS waits through", 0.8, 3.0),
        Adjustable(
            "recorder", "greet", str, 'when TARS says "Yes?" after its name', choices=("pause", "always", "never")
        ),
        Adjustable("wake", "threshold", float, "how sure the wake word must be: lower wakes more easily", 0.2, 0.6),
        Adjustable("tts", "instructions", str, "how TARS's voice speaks (tone, pace, accent), in plain words"),
        Adjustable("llm", "home_city", str, "the household's city, for the weather and opening hours"),
    )
}


class SettingsError(ValueError):
    """Said back to the model: what's wrong with the change asked for."""


class Settings:
    """`store`: the event database's, to log each change (settings_log). `run`: how commands run (systemd-run)."""

    def __init__(
        self,
        root: Path,
        store=None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        clock: Callable[[], float] = time.time,
    ):
        self._root, self._store, self._run, self._clock = root, store, run, clock

    def listing(self) -> str:
        cfg = load_config(self._root / "config.toml")
        lines = []
        for a in ADJUSTABLE.values():
            now = getattr(getattr(cfg, a.section), a.key)
            bounds = f", one of {', '.join(a.choices)}" if a.choices else ""
            bounds += f", {a.low:g} to {a.high:g}" if a.low is not None else ""
            lines.append(f"{a.name} = {json.dumps(now)}: {a.what}{bounds}")
        if self._store is not None:
            recent = self._store.rows("SELECT * FROM settings_log ORDER BY id DESC LIMIT 5")
            if recent:
                lines.append("changed lately (to put one back, set it to what it was):")
                for r in recent:
                    at = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["at"]))
                    lines.append(f"  {at} {r['name']}: {r['old']} -> {r['new']}, for {r['person'] or 'someone'}")
        return "\n".join(lines)

    def set(self, name: str, raw: str, person: str | None) -> str:
        """Changes one setting, checked, logged and applied; what to tell them."""
        a = ADJUSTABLE.get(name.strip().lower())
        if a is None:
            raise SettingsError(f"only these can change: {', '.join(ADJUSTABLE)}")
        value = _value(a, raw)
        path = self._root / LOCAL_CONFIG
        before = path.read_text() if path.exists() else ""
        old = getattr(getattr(load_config(self._root / "config.toml"), a.section), a.key)
        if old == value:
            return f"{a.name} is already {json.dumps(value)}: nothing to change"
        path.write_text(set_line(before, a.section, a.key, value, person))
        try:
            load_config(self._root / "config.toml")
        except (ConfigError, ValueError) as e:
            path.write_text(before)
            raise SettingsError(f"the config wouldn't load with that, so nothing changed: {e}") from None
        if self._store is not None:
            self._store.write(
                "INSERT INTO settings_log (at, person, name, old, new) VALUES (?, ?, ?, ?, ?)",
                (self._clock(), person, a.name, json.dumps(old), json.dumps(value)),
            )
        return f"done: {a.name} is {json.dumps(value)} (it was {json.dumps(old)}); {self._apply()}"

    def _apply(self) -> str:
        if not shutil.which("systemd-run"):
            return "it takes effect when TARS next starts"
        command = ["systemd-run", "--user", f"--on-active={RESTART_AFTER_S}", "systemctl", "--user", "restart", *UNITS]
        done = self._run(command, capture_output=True, text=True, timeout=10, check=False)
        if done.returncode:
            return "it takes effect when TARS next starts (a restart couldn't be scheduled)"
        return f"TARS restarts in {RESTART_AFTER_S} seconds to use it: say so, and that it'll be back in a moment"


def _value(a: Adjustable, raw: str):
    text = str(raw).strip()
    if a.kind is str:
        if a.choices and text.lower() not in a.choices:
            raise SettingsError(f"{a.name} is one of {', '.join(a.choices)}")
        if len(text) > MAX_TEXT:
            raise SettingsError(f"{a.name} is at most {MAX_TEXT} characters")
        return text.lower() if a.choices else text
    try:
        value = a.kind(float(text))
    except ValueError:
        raise SettingsError(f"{a.name} is a number") from None
    if a.low is not None and not a.low <= value <= a.high:
        raise SettingsError(f"{a.name} goes from {a.low:g} to {a.high:g}")
    return value


def set_line(text: str, section: str, key: str, value, person: str | None) -> str:
    """config.local.toml with `key` under [section] set to `value`: its own line replaced (the line it replaces kept as
    a comment above it, with whatever note a person wrote on it), or added under the section (made at the end if
    there's none); every other line as it was."""
    line = f"{key} = {json.dumps(value)}  # set by TARS for {person or 'someone'}, {time.strftime('%Y-%m-%d')}"
    lines = text.splitlines()
    header = re.compile(rf"^\s*\[\s*{re.escape(section)}\s*\]\s*(#.*)?$")
    start = next((i for i, l in enumerate(lines) if header.match(l)), None)
    if start is None:
        return "\n".join([*lines, "", f"[{section}]", line]).lstrip("\n") + "\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    mine = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start + 1, end):
        if mine.match(lines[i]):
            kept = f"# before TARS changed it: {lines[i].strip()}"  # a person's own note on it isn't lost
            if lines[i - 1].startswith(f"# before TARS changed it: {key} ") or lines[i - 1].startswith(
                f"# before TARS changed it: {key}="
            ):
                kept = lines[i - 1]  # the first one, a person's, stays; TARS's own in between are in settings_log
                lines[i - 1 : i + 1] = [kept, line]
            else:
                lines[i : i + 1] = [kept, line]
            return "\n".join(lines) + "\n"
    lines.insert(start + 1, line)
    return "\n".join(lines) + "\n"
