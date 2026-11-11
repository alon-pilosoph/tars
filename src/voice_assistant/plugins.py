"""Plugins (docs/home.md#plugins): someone else's code adding to TARS without a fork, declaring what it needs and
getting only what it's granted.

A plugin is a Python module named in [home.plugins.<name>] module. It declares itself:

    PLUGIN = Manifest(version="1.2.0", needs=("TUYA_LOCAL_KEY", "home_assistant", "announce"))

and has setup(settings, context), which returns the kinds of device it makes ({kind: make(spec) -> Device}), or
Provides(kinds, tools) to add tools of its own as well.

`needs` is what it may use: a key in .env by its name, or one of NEEDS. [home.plugins.<name>] grant says what it's
given. A plugin that needs anything not granted doesn't load. Its context has only what was granted: other keys aren't
there, Home Assistant only if granted, and never Home Assistant's unlock, open or disarm unless "unlock" is granted by
name. Without grant, a plugin loads as it did before grants, with everything, and --check warns until grant is set.

Grants hold at the interface TARS hands a plugin; a plugin is still Python running in TARS's process, so it's code
someone reviewed, not a sandbox.
"""

import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass, field

# What a plugin can be granted, besides keys in .env (UPPER_CASE names).
NEEDS = {
    "home_assistant": "read Home Assistant and call its services (never unlock, open or disarm)",
    "unlock": "Home Assistant's unlock, open and disarm services",
    "announce": "say something aloud on its own",
    "tell": "message TARS's maintainers",
    "tv_agent": "the Android TV agent",
    "devices": "the house's other devices",
}
SECRET = re.compile(r"[A-Z][A-Z0-9_]*")
# Services that open the house: only with "unlock" granted.
OPENING = re.compile(r"^(lock\.(unlock|open)|alarm_control_panel\.alarm_disarm)$")
BUILT_IN_TOOLS = {"home", "home_assistant", "send", "remind", "cancel_reminder", "snooze_reminder", "browse",
                  "skills", "tars", "feeds", "web_search"}  # fmt: skip


@dataclass(frozen=True)
class Manifest:
    version: str
    needs: tuple[str, ...] = ()

    def problems(self) -> list[str]:
        bad = [n for n in self.needs if n not in NEEDS and not SECRET.fullmatch(n)]
        return [f"it needs {', '.join(bad)}, which isn't a key's name in .env nor one of {', '.join(NEEDS)}"] * bool(
            bad
        )


@dataclass
class PluginTool:
    """A tool a plugin adds for the brain: its name, what it does and its parameters (a JSON schema object), as the
    model sees them; call(args, request) -> what to tell the model. `changes`: it changes something (it waits for the
    turn to be over, and a reply that read the web can't call it)."""

    name: str
    description: str
    parameters: dict
    call: Callable[[dict, object], str]
    changes: bool = False
    rules: str = ""

    @property
    def spec(self) -> dict:
        return {"type": "function", "name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass
class Provides:
    kinds: dict[str, Callable[[dict], object]] = field(default_factory=dict)
    tools: list[PluginTool] = field(default_factory=list)


@dataclass
class Loaded:
    """A plugin as loaded, for --check: its version, what it was granted (None: everything, no grant set)."""

    name: str
    version: str
    needs: tuple[str, ...]
    granted: tuple[str, ...] | None
    error: str = ""


class Refused(PermissionError):
    """A plugin used something it wasn't granted."""


class GuardedHass:
    """Home Assistant as a plugin granted home_assistant sees it: its public methods, but no service that opens the
    house unless it was granted unlock."""

    def __init__(self, hass, unlock: bool):
        self._hass, self._unlock = hass, unlock

    def call(self, service: str, data: dict):
        return self._hass.call(self._may(service), data)

    def call_service(self, service: str, data: dict):
        return self._hass.call_service(self._may(service), data)

    def _may(self, service: str) -> str:
        if OPENING.match(service.strip().lower()) and not self._unlock:
            raise Refused(f"{service} needs the unlock grant")
        return service

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._hass, name)


def scoped(context, needs: set[str]):
    """The context a plugin granted `needs` gets: a copy of `context` with only those."""
    full_secret = context.secret

    def secret(name: str) -> str | None:
        if name not in needs:
            raise Refused(f"{name} isn't granted to this plugin: add it to its grant in [home.plugins]")
        return full_secret(name)

    hass = context.hass if "home_assistant" in needs else None
    return dataclasses.replace(
        context,
        secret=secret,
        hass=GuardedHass(hass, "unlock" in needs) if hass is not None else None,
        announce=context.announce if "announce" in needs else None,
        tell=context.tell if "tell" in needs else None,
        tv_agent=context.tv_agent if "tv_agent" in needs else None,
        devices=context.devices if "devices" in needs else {},
    )


def admit(name: str, module, grant: list[str] | None, context) -> tuple[object, Loaded]:
    """The context plugin `name` (its loaded `module`) gets, and how it loaded; Refused if it needs what it wasn't
    granted, or declares nothing with a grant set."""
    manifest = getattr(module, "PLUGIN", None)
    if manifest is None:
        if grant is not None:
            raise Refused("it declares nothing it needs (PLUGIN = Manifest(...)), so it can't be granted")
        return context, Loaded(name, "unversioned", (), None)
    if problems := manifest.problems():
        raise Refused(problems[0])
    if grant is None:
        return context, Loaded(name, manifest.version, manifest.needs, None)
    missing = [n for n in manifest.needs if n not in grant]
    if missing:
        raise Refused(f"it needs {', '.join(missing)}, which [home.plugins.{name}] grant doesn't give it")
    return scoped(context, set(grant)), Loaded(name, manifest.version, manifest.needs, tuple(grant))


def provided(made, name: str) -> Provides:
    """What a plugin's setup returned, as Provides; its tools checked."""
    got = made if isinstance(made, Provides) else Provides(kinds=dict(made))
    seen = set()
    for tool in got.tools:
        if tool.name in BUILT_IN_TOOLS or tool.name in seen or not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", tool.name):
            raise ValueError(f"the plugin {name}'s tool {tool.name!r} needs a name of its own (lowercase, _)")
        seen.add(tool.name)
    return got
