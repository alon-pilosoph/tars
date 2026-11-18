"""The hub's side of the house (docs/house-hub.md): the devices on the home network, behind one boundary.

The house tools (home.HomeTools) don't hold the devices themselves: each is a `HubDevice`, which says what the hub
described of it (its name, label, actions, entity, kind) and asks the hub for everything else (its status, a call,
its history), as it will over the link once the hub is a service of its own. Today the hub is `LocalHub`, in the same
process, holding the real devices that home.build made: nothing about the house changes, only where it's reached
through. The rules (a turn's go-ahead, nothing after a web search, unlocking) stay with HomeTools, on the brain's
side of the boundary.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import ANDROID_TV, CAMERA, SPOTIFY, HomeConfig
from .home import TOKEN_KEY, Built, Check, Context, Device, HomeTools, Request

if TYPE_CHECKING:
    from .plugins import Loaded, PluginTool


@dataclass(frozen=True)
class Description:
    """What the brain is told of a device, once, to build the home tool from: what doesn't change while it runs."""

    name: str
    label: str | None
    actions: tuple[str, ...]
    entity: str  # the Home Assistant entity a lock's unlock names (auto-lock counts from it); "" for the rest
    kind: str  # what sort of device it is, as its class is named ("Spotify", "Lock"...)
    takes_temperature: bool

    @classmethod
    def of(cls, device: Device) -> Description:
        return cls(
            device.name,
            device.label,
            tuple(device.actions),
            str(getattr(device, "entity", "") or ""),
            type(device).__name__,
            bool(device.takes_temperature),
        )


class LocalHub:
    """The hub in this process: the devices themselves, by name in lower case, and Home Assistant (`hass`, for the
    home_assistant tool). Each of its methods is one message of the link's (docs/house-hub.md#the-link):
    `describe`, a `call`, a read (`ask`), or a request to Home Assistant (`home_assistant`)."""

    def __init__(self, devices: dict[str, Device], hass=None):
        self._devices = {name.strip().lower(): device for name, device in devices.items()}
        self._hass = hass

    def home_assistant(self, request: str, *args, **kwargs):
        """One of HA_REQUESTS, made to Home Assistant here, where its token is."""
        if request not in HA_REQUESTS or self._hass is None:
            raise ValueError(f"not a request the hub makes of Home Assistant: {request}")
        return getattr(self._hass, request)(*args, **kwargs)

    def describe(self) -> dict[str, Description]:
        return {name: Description.of(device) for name, device in self._devices.items()}

    def call(self, name: str, action: str, args: dict, request: Request) -> str:
        return self._devices[name].call(action, args, request)

    def ask(self, name: str, what: str, *args):
        """A read: `what` is one of status, what, history, past, rules, glance, slow, check, stop."""
        if what not in READS:
            raise ValueError(f"not a read: {what}")
        return getattr(self._devices[name], what)(*args)


READS = ("status", "what", "history", "past", "rules", "glance", "slow", "check", "stop")
# What the home_assistant tool asks of Home Assistant (ha_tool.HAAccess, its door check, ha_setup.HASetup): reading it,
# a service call, and its setup's own.
HA_REQUESTS = (
    "states", "state", "areas", "area_list", "history", "statistics", "logbook", "services", "call_service",
    "entity_entries", "entries", "devices", "config_of", "discovered", "start_setup", "answer_setup",
    "update_device", "remove_entry", "link",
)  # fmt: skip


class HubHomeAssistant:
    """Home Assistant as the brain has it: each request made by the hub (LocalHub.home_assistant)."""

    def __init__(self, hub: LocalHub):
        self._hub = hub

    def __getattr__(self, request: str):
        if request not in HA_REQUESTS:
            raise AttributeError(request)
        return lambda *args, **kwargs: self._hub.home_assistant(request, *args, **kwargs)


class HubDevice(Device):
    """A device as the brain has it: what the hub described, and the hub asked for the rest."""

    def __init__(self, hub: LocalHub, described: Description):
        super().__init__(described.name)
        self._hub, self._key = hub, described.name.strip().lower()
        self.label = described.label
        self.actions = described.actions
        self.entity = described.entity
        self.kind = described.kind
        self.takes_temperature = described.takes_temperature

    def what(self) -> str:
        return self._hub.ask(self._key, "what")

    def status(self) -> str:
        return self._hub.ask(self._key, "status")

    def call(self, action: str, args: dict, request: Request) -> str:
        return self._hub.call(self._key, action, args, request)

    def history(self, args: dict) -> str:
        return self._hub.ask(self._key, "history", args)

    def past(self, start: float, end: float) -> list[tuple[float, str]]:
        return self._hub.ask(self._key, "past", start, end)

    def rules(self) -> str:
        return self._hub.ask(self._key, "rules")

    def glance(self) -> str | None:
        return self._hub.ask(self._key, "glance")

    def slow(self, action: str, args: dict) -> bool:
        return self._hub.ask(self._key, "slow", action, args)

    def check(self) -> list[Check]:
        return self._hub.ask(self._key, "check")

    def stop(self) -> None:
        self._hub.ask(self._key, "stop")


def behind_hub(tools: HomeTools, hass=None) -> LocalHub:
    """The house tools' devices and Home Assistant (`hass`) moved behind a hub: the tools then reach each device
    through its HubDevice, and Home Assistant through HubHomeAssistant."""
    hub = LocalHub(tools.devices, hass)
    tools.devices = {name: HubDevice(hub, described) for name, described in hub.describe().items()}
    if tools.ha is not None and hass is not None:
        tools.ha.through(HubHomeAssistant(hub))
    return hub


def build(cfg: HomeConfig, context: Context, extra: dict[str, Device] | None = None) -> Built:
    """The devices [home] lists, made: Home Assistant's, then Android TVs, then each plugin's, then `extra`, made
    already (TARS's own, like who's home). Like every service, optional: a device that can't be made (no token, a
    missing extra, a plugin's own problem) is left out with why, and TARS starts without it."""
    from . import androidtv, hass
    from .home import load_plugin
    from .plugins import Loaded, admit, provided

    unavailable: dict[str, str] = {}
    # As people call them ("AC", "front door"), and without label, which is how each is written, the same for all.
    specs = [(d["name"].strip(), {k: v for k, v in d.items() if k != "label"}) for d in cfg.devices]
    labels = {d["name"].strip(): d["label"].strip() for d in cfg.devices if d.get("label")}
    if context.hass is None and cfg.url and context.secret(TOKEN_KEY):
        context.hass = hass.HomeAssistant(cfg.url, context.secret(TOKEN_KEY))
    makers: dict[str, Callable[[dict], Device]] = {}
    plugin_tools: list[PluginTool] = []
    loaded: dict[str, Loaded] = {}
    for plugin, settings in cfg.plugins.items():
        try:
            module = load_plugin(settings["module"], context.root)
            given, loaded[plugin] = admit(plugin, module, settings.get("grant"), context)
            made = provided(
                module.setup({k: v for k, v in settings.items() if k not in ("module", "grant")}, given),
                plugin,
                {t.name for t in plugin_tools},
            )
            makers |= {f"{plugin}.{kind}": make for kind, make in made.kinds.items()}
            plugin_tools += made.tools
        except Exception as e:  # noqa: BLE001 - a plugin that won't load costs its devices, not TARS
            had = loaded.get(plugin)
            loaded[plugin] = Loaded(plugin, had.version if had else "", had.needs if had else (), None, str(e))
            for name, d in specs:
                if str(d.get("kind", "")).startswith(f"{plugin}."):
                    unavailable[name] = f"the plugin {plugin} didn't load: {e}"
    # Home Assistant's and the Android TVs first: a plugin's device may lean on them (a TV remote asking the TV's box
    # whether the TV is on).
    for name, d in sorted(specs, key=lambda s: s[1].get("kind") not in (None, ANDROID_TV)):
        if name in unavailable:
            continue
        kind = d.get("kind")
        try:
            if kind is None:
                if context.hass is None:
                    why = f"no {TOKEN_KEY} in .env" if cfg.url else "[home] url isn't set"
                    raise ValueError(f"it goes through Home Assistant, and {why}")
                device = hass.entity_device(name, d, context.hass)
            elif kind == ANDROID_TV:
                device = androidtv.make(name, d, context)
            elif kind == CAMERA:
                if context.hass is None:
                    why = f"no {TOKEN_KEY} in .env" if cfg.url else "[home] url isn't set"
                    raise ValueError(f"it goes through Home Assistant, and {why}")
                device = hass.camera_device(name, d, context.hass)
            elif kind == SPOTIFY:
                from . import spotify

                device = spotify.make(name, d, context)
            elif kind in makers:
                device = makers[kind](d | {"name": name})
                context.plugin_of[name.lower()] = kind.partition(".")[0]
            else:
                raise ValueError(f"its plugin has no kind {kind.partition('.')[2]!r}")
        except Exception as e:  # noqa: BLE001 - one device that can't be made never stops the others
            unavailable[name] = str(e) or type(e).__name__
            continue
        device.label = labels.get(name) or device.label
        context.devices[name.lower()] = device
    for name, device in (extra or {}).items():
        context.devices[name.lower()] = device
    made = context.devices or context.hass is not None or plugin_tools
    history = None
    if cfg.config_dir and context.hass is not None:
        from .haconfig import HAConfig

        history = HAConfig(context.root / cfg.config_dir, cfg.container)
    tools = None
    if made:
        tools = HomeTools(
            cfg,
            dict(context.devices),
            hass=context.hass,
            ha_config=history,
            maintainers=context.maintainers,
            plugin_tools=plugin_tools,
        )
        context.may_unlock = tools._may_unlock
    return Built(tools, unavailable, loaded)
