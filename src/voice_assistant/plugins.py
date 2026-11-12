"""Plugins (docs/home.md#plugins): someone else's code adding to TARS without a fork, declaring what it needs and
getting only what it's granted.

A plugin is a Python module named in [home.plugins.<name>] module. It declares itself:

    PLUGIN = Manifest(version="1.2.0", needs=("TUYA_LOCAL_KEY", "home_assistant", "announce"))

and has setup(settings, context), which returns the kinds of device it makes ({kind: make(spec) -> Device}), or
Provides(kinds, tools) to add tools of its own as well.

`needs` is what it may use: a key in .env by its name, or one of NEEDS. [home.plugins.<name>] grant says what it's
given. A plugin that needs anything not granted doesn't load. Its context has only what was granted: other keys aren't
there, Home Assistant only if granted, nothing that could open the house (in Home Assistant, or another device's
unlock) unless "unlock" is granted by name, and nothing that administers Home Assistant unless "ha_admin" is. Each
grant is checked on its own, so neither brings the other. Without grant, a plugin loads as it did before grants, with
everything, and --check warns until grant is set.

Grants hold at the interface TARS hands a plugin; a plugin is still Python running in TARS's process (and keeps root,
so it can read the project's files), so it's code someone reviewed, not a sandbox.
"""

import dataclasses
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

# What a plugin can be granted, besides keys in .env (UPPER_CASE names). Each is checked on its own: one never
# brings another with it.
NEEDS = {
    "home_assistant": "read Home Assistant and call its services (never what could open the house, or administers it)",
    "unlock": "whatever in Home Assistant could open the house (and what can't be told), and a lock's unlock",
    "ha_admin": "administer Home Assistant: restart, update, back up, users, recorder (running code needs unlock too)",
    "announce": "say something aloud on its own",
    "tell": "message TARS's maintainers",
    "tv_agent": "the Android TV agent",
    "devices": "the house's other devices",
}
SECRET = re.compile(r"[A-Z][A-Z0-9_]*")
# TARS's own keys in .env: granting one gives a plugin TARS's own accounts, so --check warns.
TARS_KEYS = frozenset(
    {"OPENAI_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY",
     "TELEGRAM_BOT_TOKEN", "HOME_ASSISTANT_TOKEN", "HA_CONFIG_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
)  # fmt: skip
SERVICE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
# Besides what ha_tool.Doors finds: what else opens the house, only with "unlock" granted.
DISARM = "alarm_control_panel.alarm_disarm"
OPEN_COVERS = ("garage", "door", "gate")  # a cover with one of these device classes lets someone in
# Home Assistant's methods a plugin may use as they are: reading it. call and call_service are guarded.
HASS_READS = ("ping", "state", "states", "areas", "history", "logbook", "services", "entries", "discovered",
              "devices", "entity_entries", "area_list", "config_of", "snapshot", "statistics", "link")  # fmt: skip
# Every tool the brain has of its own: a plugin's tool can't take one's name (test_plugins checks it's every one).
BUILT_IN_TOOLS = {"home", "home_assistant", "send", "remind", "cancel_reminder", "snooze_reminder", "browse",
                  "skills", "tars", "feeds", "web_search"}  # fmt: skip


@dataclass(frozen=True)
class Manifest:
    version: str
    needs: tuple[str, ...] = ()

    def problems(self) -> list[str]:
        if not (isinstance(self.version, str) and self.version.strip()):
            return ['its version is a string, like "1.0.0"']
        if not (isinstance(self.needs, list | tuple) and all(isinstance(n, str) for n in self.needs)):
            return ['its needs are a tuple of names, like ("home_assistant",)']
        if bad := [n for n in self.needs if n not in NEEDS and not SECRET.fullmatch(n)]:
            return [f"it needs {', '.join(bad)}, which isn't a key's name in .env nor one of {', '.join(NEEDS)}"]
        return []


@dataclass
class PluginTool:
    """A tool a plugin adds for the brain: its name, what it does and its parameters (a JSON schema object), as the
    model sees them; call(args, request) -> what to tell the model. `changes`: it changes something (it waits for the
    turn to be over, and neither a reply that read the web nor a turn a door's lock set off can call it)."""

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
    """Home Assistant as a plugin granted home_assistant sees it: reading it (HASS_READS), and calling its services.
    Each grant is checked on its own: a service that administers Home Assistant (ha_tool.ADMIN) needs ha_admin, and
    one that could open the house needs unlock, whatever else was granted. The client itself isn't handed over: each
    method here only relays to it."""

    def __init__(self, hass, unlock: bool, admin: bool = False):
        from .ha_tool import ADMIN, Doors

        doors = Doors(hass, covers=OPEN_COVERS)

        def may(service: str, data: dict) -> str:
            checked = service.strip().lower() if isinstance(service, str) else ""
            if not SERVICE.fullmatch(checked):
                raise Refused(f"{service!r} isn't a service's name, as light.turn_on")
            if not isinstance(data, dict):
                raise Refused(f"{checked}: its data is a dict, as {{'entity_id': 'light.kitchen'}}")
            domain, _, name = checked.partition(".")
            if domain in ADMIN and not admin:
                raise Refused(f"{checked} administers Home Assistant: it needs the ha_admin grant")
            if unlock:
                return checked
            if checked == DISARM:
                raise Refused(f"{checked} needs the unlock grant")
            if why := doors.opening(domain, name, data):
                raise Refused(f"{checked} needs the unlock grant ({why})")
            return checked

        def guarded(method):
            return lambda service, data: method(may(service, data), data)

        self.call, self.call_service = guarded(hass.call), guarded(hass.call_service)
        for name in HASS_READS:
            if callable(method := getattr(hass, name, None)):
                setattr(self, name, _relay(method))


def _relay(method):
    return lambda *args, **kwargs: method(*args, **kwargs)


class DeviceView:
    """Another device, as a plugin granted devices sees it: what it is and how it is, and its actions, but never
    unlock or open without the unlock grant, and with it only as the home tool would (HomeTools.may_unlock), once
    the house is built. The device itself isn't handed over."""

    def __init__(self, device, unlock: bool, may_unlock: Callable[[], Callable | None]):
        from .home import OPEN, READS, UNLOCK, Request

        self.name, self.label, self.display = device.name, device.label, device.display
        self.actions = actions = tuple(device.actions)
        for name in ("what", "status", "glance", "history", "rules", "slow", "check"):
            if callable(method := getattr(device, name, None)):
                setattr(self, name, _relay(method))

        def call(action: str, args: dict, request) -> str:
            if action not in actions or action in READS:
                raise Refused(f"the {device.name} can do: {', '.join(a for a in actions if a not in READS)}")
            if action in (UNLOCK, OPEN):
                if not unlock:
                    raise Refused(f"{action} needs the unlock grant")
                if (policy := may_unlock()) is not None:
                    if not isinstance(request, Request):
                        raise Refused(f"{action} needs the request it answers")
                    if refused := policy(request):
                        return refused
            return device.call(action, args, request)

        self.call = call


class DevicesView(Mapping):
    """The house's devices as a plugin granted devices reads them, by name, as they're made: its own as they are,
    the others as DeviceView."""

    def __init__(self, context, plugin: str, unlock: bool):
        devices, owners, policy = context.devices, context.plugin_of, lambda: context.may_unlock
        self._get = lambda name: (
            devices[name] if owners.get(name) == plugin else DeviceView(devices[name], unlock, policy)
        )
        self._names = lambda: list(devices)

    def __getitem__(self, name: str):
        return self._get(name)

    def __iter__(self):
        return iter(self._names())

    def __len__(self) -> int:
        return len(self._names())


def scoped(context, needs: set[str], plugin: str = ""):
    """The context plugin `plugin`, granted `needs`, gets: a copy of `context` with only those."""
    full_secret = context.secret

    def secret(name: str) -> str | None:
        if name not in needs:
            raise Refused(f"{name} isn't granted to this plugin: add it to its grant in [home.plugins]")
        return full_secret(name)

    hass = context.hass if "home_assistant" in needs else None
    unlock = "unlock" in needs
    return dataclasses.replace(
        context,
        secret=secret,
        hass=GuardedHass(hass, unlock, "ha_admin" in needs) if hass is not None else None,
        announce=context.announce if "announce" in needs else None,
        tell=context.tell if "tell" in needs else None,
        tv_agent=context.tv_agent if "tv_agent" in needs else None,
        devices=DevicesView(context, plugin, unlock) if "devices" in needs else MappingProxyType({}),
        maintainers=tuple(context.maintainers),
        plugin_of={},
        may_unlock=None,
    )


def admit(name: str, module, grant: list[str] | None, context) -> tuple[object, Loaded]:
    """The context plugin `name` (its loaded `module`) gets, and how it loaded; Refused if it needs what it wasn't
    granted, or declares nothing with a grant set."""
    manifest = getattr(module, "PLUGIN", None)
    if manifest is None:
        if grant is not None:
            raise Refused("it declares nothing it needs (PLUGIN = Manifest(...)), so it can't be granted")
        return context, Loaded(name, "unversioned", (), None)
    if not isinstance(manifest, Manifest):
        raise Refused("its PLUGIN isn't a Manifest: PLUGIN = Manifest(version=..., needs=(...))")
    if problems := manifest.problems():
        raise Refused(problems[0])
    if grant is None:
        return context, Loaded(name, manifest.version, manifest.needs, None)
    missing = [n for n in manifest.needs if n not in grant]
    if missing:
        raise Refused(f"it needs {', '.join(missing)}, which [home.plugins.{name}] grant doesn't give it")
    return scoped(context, set(grant), name), Loaded(name, manifest.version, manifest.needs, tuple(grant))


def provided(made, name: str, taken: set[str] | frozenset[str] = frozenset()) -> Provides:
    """What a plugin's setup returned, as Provides; its tools checked, none named as a built-in tool, one of its own,
    or one in `taken` (another plugin's)."""
    got = made if isinstance(made, Provides) else Provides(kinds=dict(made))
    seen = set(taken)
    for tool in got.tools:
        if tool.name in BUILT_IN_TOOLS or tool.name in seen or not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", tool.name):
            raise ValueError(f"the plugin {name}'s tool {tool.name!r} needs a name of its own (lowercase, _)")
        seen.add(tool.name)
    return got
