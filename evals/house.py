"""The house a scenario plays in, and the tests' too: TARS's real home tool (home.py, hass.py, androidtv.py) over a
Home Assistant that's only pretend, and an Android TV box that does what it's told. What each turn changed is kept,
so a scenario can check it locked the door once, and never unlocked it for the wrong voice."""

import json
from dataclasses import dataclass, field

import httpx

from voice_assistant.androidtv import APPS, TvDevice
from voice_assistant.config import HomeConfig
from voice_assistant.hass import HomeAssistant, entity_device
from voice_assistant.home import STATUS, HomeTools, Request

URL, TOKEN = "http://pretend:8123", "eval"
# The pretend house: Home Assistant's entities, as [[home.devices]] lists them, and an Android TV box.
DEVICES = [
    {"name": "front door", "entity": "lock.front_door", "door": "binary_sensor.front_door_door"},
    {
        "name": "boiler",
        "entity": "switch.boiler",
        "details": ["sensor.boiler_time_left"],
        "timer": {"service": "switcher_kis.turn_on_with_timer", "field": "timer_minutes", "max": 150},
    },
    {"name": "AC", "entity": "climate.living_room"},
    {"name": "blinds", "entity": "cover.living_room"},
    {"name": "soundbar", "entity": "media_player.soundbar"},
    {"name": "living room", "entity": "sensor.living_room_temperature"},
    {"name": "tv", "kind": "androidtv", "address": "127.0.0.1"},
]
# What a house setting in a scenario may be, and the flags that break it: Home Assistant down, the lock jamming.
HOUSE_KEYS = {
    "lock": ("locked", "unlocked"),
    "door": ("closed", "open"),
    "boiler": ("on", "off"),
    "ac": ("off", "cool", "heat"),
    "blinds": ("open", "closed"),
    "tv": ("on", "off"),
}
HOUSE_FLAGS = ("jams", "down")
CHANGED = ("done", "sent", "not confirmed", "started")


def entities(house: dict) -> dict[str, tuple[str, dict]]:
    """The pretend Home Assistant's entities, as `house` sets them: {entity: (state, attributes)}. The AC is an IR
    one (assumed_state), as an AC with no Wi-Fi of its own is through Home Assistant."""
    boiler = house.get("boiler", "off")
    return {
        "lock.front_door": (house.get("lock", "unlocked"), {}),
        "binary_sensor.front_door_door": ("on" if house.get("door") == "open" else "off", {"device_class": "door"}),
        "switch.boiler": (boiler, {}),
        "sensor.boiler_time_left": (
            "35" if boiler == "on" else "0",
            {"friendly_name": "Boiler time left", "unit_of_measurement": "min"},
        ),
        "climate.living_room": (
            house.get("ac", "off"),
            {
                "hvac_modes": ["off", "cool", "heat", "dry", "fan_only", "auto"],
                "fan_modes": ["low", "medium", "high", "auto"],
                "min_temp": 16,
                "max_temp": 30,
                "temperature": 24,
                "fan_mode": "auto",
                "assumed_state": True,
            },
        ),
        "cover.living_room": (house.get("blinds", "open"), {}),
        "media_player.soundbar": ("on", {"volume_level": 0.3, "is_volume_muted": False}),
        "sensor.living_room_temperature": ("23.5", {"unit_of_measurement": "°C"}),
    }


class PretendHA:
    """Home Assistant's REST API, as TARS uses it: states, and services that change them. A lock or a cover moves for
    `steps` reads before it gets there; `jams` jams the lock; `down` answers nothing."""

    def __init__(self, states: dict[str, tuple[str, dict]], steps: int = 1, jams: bool = False, down: bool = False):
        self.states = {e: [s, dict(a)] for e, (s, a) in states.items()}
        self.steps, self.jams, self.down = steps, jams, down
        self.calls: list[tuple[str, dict]] = []  # ("domain.service", data), in order
        self._moving: dict[str, tuple[int, str]] = {}

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def state(self, entity: str) -> str:
        return self.states[entity][0]

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no route to host")
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Unauthorized"})
        path = request.url.path
        if path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if path.startswith("/api/states/"):
            entity = path.removeprefix("/api/states/")
            if entity not in self.states:
                return httpx.Response(404, json={"message": "Entity not found."})
            self._step(entity)
            state, attributes = self.states[entity]
            return httpx.Response(200, json={"entity_id": entity, "state": state, "attributes": attributes})
        domain, _, service = path.removeprefix("/api/services/").partition("/")
        data = json.loads(request.content)
        self.calls.append((f"{domain}.{service}", data))
        entity = data.get("entity_id", "")
        if entity not in self.states:
            return httpx.Response(400, text=f"Entity {entity} not found")
        self._serve(domain, service, entity, data)
        return httpx.Response(200, json=[])

    def _step(self, entity: str) -> None:
        if entity in self._moving:
            left, final = self._moving[entity]
            if left <= 0:
                self.states[entity][0] = final
                del self._moving[entity]
            else:
                self._moving[entity] = (left - 1, final)

    def _move(self, entity: str, moving: str, final: str) -> None:
        self.states[entity][0] = moving
        self._moving[entity] = (self.steps, final)

    def _serve(self, domain: str, service: str, entity: str, data: dict) -> None:
        state = self.states[entity]
        if domain == "lock":
            final = "jammed" if self.jams else {"lock": "locked", "unlock": "unlocked"}[service]
            self._move(entity, f"{service}ing", final)
        elif domain == "cover":
            self._move(entity, *{"open_cover": ("opening", "open"), "close_cover": ("closing", "closed")}[service])
        elif domain == "switcher_kis":
            state[0] = "on"
            self.states["sensor.boiler_time_left"][0] = str(data.get("timer_minutes", 0))
        elif domain == "climate":
            if service == "turn_off":
                state[0] = "off"
            elif service == "turn_on":
                state[0] = "cool" if state[0] == "off" else state[0]
            elif service == "set_hvac_mode":
                state[0] = data["hvac_mode"]
            elif service == "set_temperature":
                state[1]["temperature"] = data["temperature"]
            elif service == "set_fan_mode":
                state[1]["fan_mode"] = data["fan_mode"]
        elif domain == "media_player":
            a = state[1]
            if service in ("turn_on", "turn_off"):
                state[0] = "on" if service == "turn_on" else "off"
            elif service in ("volume_up", "volume_down"):
                step = 0.05 if service == "volume_up" else -0.05
                a["volume_level"] = round(min(1.0, max(0.0, a.get("volume_level", 0.5) + step)), 2)
            elif service == "volume_mute":
                a["is_volume_muted"] = data["is_volume_muted"]
        elif service in ("turn_on", "turn_off"):
            state[0] = service.removeprefix("turn_")


class PretendBox:
    """An Android TV box that does what it's told at once: awake or asleep, the app in front, nothing playing."""

    def __init__(self, on: bool):
        self.on, self.front = on, "com.google.android.tvlauncher" if on else ""
        self.pressed: list[tuple[str, int]] = []

    def awake(self) -> bool:
        return self.on

    def wake(self) -> None:
        self.on, self.front = True, self.front or "com.google.android.tvlauncher"

    def sleep(self) -> None:
        self.on = False

    def app(self) -> str:
        return self.front if self.on else ""

    def playing(self) -> str:
        return "nothing playing"

    def press(self, key: str, times: int = 1) -> None:
        self.pressed.append((key, times))

    def find_app(self, name: str) -> str | None:
        return APPS.get(name.strip().lower())

    def open(self, package: str) -> None:
        self.front = package

    def force_stop(self, package: str) -> None:
        self.front = ""

    def close(self) -> None:
        pass


class PretendAgent:
    """The TV's agent, as far as the brain can tell: it starts, and says nothing until it's done."""

    model = "pretend"

    def __init__(self):
        self.goals: list[str] = []

    def start(self, goal: str) -> None:
        self.goals.append(goal)

    def stop(self) -> None:
        pass


@dataclass
class House:
    tools: HomeTools
    ha: PretendHA
    box: PretendBox
    agent: PretendAgent
    changes: list[dict] = field(default_factory=list)  # every call that changed something, or tried to

    def changed_since(self, n: int) -> list[dict]:
        return self.changes[n:]


def make_house(house: dict, unlock_voices=("alon",)) -> House:
    """The pretend house, as a scenario's `house` table sets it, with its home tool."""
    ha = PretendHA(entities(house), jams=house.get("jams", False), down=house.get("down", False))
    hass = HomeAssistant(URL, TOKEN, transport=ha.transport)
    now = [0.0]  # pretend time, which passes only by waiting: the house moves on reads, never on the clock

    def sleep(seconds: float) -> None:
        now[0] += seconds

    timing = {"clock": lambda: now[0], "sleep": sleep}
    devices = {}
    for spec in DEVICES:
        if "entity" in spec:
            devices[spec["name"]] = entity_device(spec["name"], spec, hass, **timing)
    box, agent = PretendBox(house.get("tv") == "on"), PretendAgent()
    devices["tv"] = TvDevice("tv", box, agent, **timing)
    cfg = HomeConfig(url=URL, unlock_voices=list(unlock_voices), devices=DEVICES)
    tools = HomeTools(cfg, devices)
    result = House(tools, ha, box, agent)
    call = tools.call

    def kept(args: dict, request: Request) -> str:
        said = call(args, request)
        if isinstance(args, dict) and args.get("action") != STATUS and said.startswith(CHANGED):
            result.changes.append({**args, "device": str(args.get("device", "")).lower(), "result": said})
        return said

    tools.call = kept
    return result
