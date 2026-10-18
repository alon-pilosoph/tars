"""The house a scenario plays in: TARS's real home tool (home.py, ac.py) over a Home Assistant that's only pretend (a
front door lock with its door sensor, a boiler) and an AC whose codes go nowhere. What each turn changed is kept, so a
scenario can check it locked the door once, and never unlocked it for the wrong voice."""

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from voice_assistant.ac import AC, Profile, Settings, State
from voice_assistant.config import HomeConfig
from voice_assistant.firetv import APPS, TV, Found, Title
from voice_assistant.home import STATUS, HomeAssistant, HomeTools, Request
from voice_assistant.ir import CODE_SETS, Remote

DEVICES = [
    {"name": "front door", "kind": "lock", "entity": "lock.front_door", "door": "binary_sensor.front_door_door"},
    {"name": "boiler", "kind": "boiler", "entity": "switch.boiler", "remaining": "sensor.boiler_remaining_time"},
    {"name": "AC", "kind": "ac"},
    {"name": "TV", "kind": "ir", "codes": "samsung-tv"},
    {"name": "soundbar", "kind": "ir", "codes": "polk-re9641"},
    {"name": "fire tv", "kind": "firetv", "address": "127.0.0.1"},
]
BLASTER = {"id": "eval", "address": "127.0.0.1"}
# What a house setting in a scenario may be: the lock, the door, the boiler and the AC, or Home Assistant down, or the
# lock jamming when it's turned.
HOUSE_KEYS = {
    "lock": ("locked", "unlocked"),
    "door": ("closed", "open"),
    "boiler": ("on", "off"),
    "ac": ("on", "off"),
    "tv": ("on", "off"),
}
HOUSE_FLAGS = ("jams", "down")
CHANGED = ("done", "sent", "not confirmed", "noted")


def electra_profile() -> dict:
    """An Electra remote's code, as ac_onboard would learn it (github.com/barakwei/IRelectra's layout)."""
    temps = {str(t): format(t - 15, "04b") for t in range(16, 31)}
    modes = {"cool": "001", "heat": "010", "auto": "011", "dry": "100", "fan": "101"}
    fans = {"low": "00", "medium": "01", "high": "10", "auto": "11"}
    return {
        "scheme": "manchester",
        "timing": {"unit": 1000, "header": [3, 3], "trailer": 4, "one": "01", "gap": 0},
        "repeats": 3,
        "bits": format(1 << 1, "034b"),
        "fields": {
            "power": {"at": [0], "values": {"press": "1", "none": "0"}},
            "mode": {"at": [1, 2, 3], "values": modes},
            "fan": {"at": [4, 5], "values": fans},
            "swing": {"at": [8], "values": {"on": "1", "off": "0"}},
            "temperature": {"at": [11, 12, 13, 14], "values": temps},
        },
        "checksum": None,
        "checked": True,
    }


class PretendHA:
    """Home Assistant's REST API, as the home tool uses it: states, and services that change them at once."""

    def __init__(self, house: dict):
        self.down, self.jams = house.get("down", False), house.get("jams", False)
        self.states = {
            "lock.front_door": house.get("lock", "unlocked"),
            "binary_sensor.front_door_door": "on" if house.get("door") == "open" else "off",
            "switch.boiler": house.get("boiler", "off"),
            "sensor.boiler_remaining_time": "00:35:00" if house.get("boiler") == "on" else "00:00:00",
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no route to host")
        path = request.url.path
        if path.startswith("/api/states/"):
            entity = path.removeprefix("/api/states/")
            if entity not in self.states:
                return httpx.Response(404)
            return httpx.Response(200, json={"entity_id": entity, "state": self.states[entity], "attributes": {}})
        domain, service = path.removeprefix("/api/services/").split("/")
        data = json.loads(request.content)
        entity = data["entity_id"]
        if domain == "lock":
            self.states[entity] = "jammed" if self.jams else {"lock": "locked", "unlock": "unlocked"}[service]
        elif service in ("turn_on", "turn_on_with_timer"):
            self.states[entity] = "on"
            minutes = data.get("timer_minutes", 0)
            self.states["sensor.boiler_remaining_time"] = f"{minutes // 60:02d}:{minutes % 60:02d}:00"
        elif service == "turn_off":
            self.states[entity] = "off"
        return httpx.Response(200, json=[])


class PretendStick:
    """A Fire TV that does what it's told at once: awake or asleep, the app in front, nothing ever playing."""

    def __init__(self, on: bool):
        self.on, self.front = on, "com.amazon.tv.launcher" if on else ""
        self.linked = self.title = ""  # a title's page opened by link, and then what plays from it

    def awake(self) -> bool:
        return self.on

    def wake(self) -> None:
        self.on, self.front = True, self.front or "com.amazon.tv.launcher"

    def sleep(self) -> None:
        self.on = False

    def app(self) -> str:
        return self.front if self.on else ""

    def playing(self) -> str:
        return f"playing: {self.title} ({self.front})" if self.title else "nothing playing"

    def media(self) -> list[tuple[str, str, str]]:
        return [(self.front, "playing", self.title)] if self.title else []

    def press(self, key: str, times: int = 1) -> None:
        if key == "select" and self.linked:
            self.title, self.linked = self.linked, ""

    def open_link(self, uri: str, package: str, component: str = "", flags: str = "", extras: dict | None = None):
        self.front, self.linked = package, uri

    def find_app(self, name: str) -> str | None:
        return APPS.get(name.strip().lower())

    def open(self, package: str) -> None:
        self.front = package


class PretendCatalog:
    """Every title is found, as asked: no lookups in an eval."""

    def imdb(self, title: Title) -> Found:
        return Found("tt0114709", title.name.title(), title.year, bool(title.season))

    netflix = imdb


class PretendAgent:
    """The Fire TV's agent, as far as the brain can tell: it starts, and says nothing until it's done."""

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
    changes: list[dict] = field(default_factory=list)  # every call that changed something, or tried to

    def changed_since(self, n: int) -> list[dict]:
        return self.changes[n:]


def make_house(house: dict, folder: Path) -> House:
    ha = PretendHA(house)
    hass = HomeAssistant("http://pretend:8123", "eval", transport=httpx.MockTransport(ha.handle))
    state_path = folder / "ac-state.json"
    if house.get("ac") == "on":
        State(on=True, settings=Settings(), how="told").save(state_path)

    def nowhere(pulses: list[int]) -> None:
        pass

    local = {
        "ac": AC(Profile(electra_profile()), nowhere, state_path),
        "tv": Remote("tv", CODE_SETS["samsung-tv"], nowhere, sleep=lambda s: None),
        "soundbar": Remote("soundbar", CODE_SETS["polk-re9641"], nowhere, sleep=lambda s: None),
        "fire tv": TV(
            PretendStick(house.get("tv") == "on"), PretendAgent(), sleep=lambda s: None, catalog=PretendCatalog()
        ),
    }
    cfg = HomeConfig(url="http://pretend:8123", unlock_voices=["alon"], devices=DEVICES, blaster=BLASTER)
    tools = HomeTools(cfg, hass, local, sleep=lambda s: None)
    result = House(tools, ha)
    call = tools.call

    def kept(args: dict, request: Request) -> str:
        said = call(args, request)
        if isinstance(args, dict) and args.get("action") != STATUS and said.startswith(CHANGED):
            result.changes.append({**args, "device": str(args.get("device", "")).lower(), "result": said})
        return said

    tools.call = kept
    return result
