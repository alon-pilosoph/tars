"""Home Assistant: its REST API on the home network, and the devices TARS works through it, one class per kind of
entity (docs/home.md).

Every change is read back from what the entity reports afterwards, so the answer is "done" only when the device says
so. An entity whose state Home Assistant only assumes (an IR-controlled AC or TV, which can't report back) answers
"sent" instead: the request went out, and nothing can tell whether it worked."""

import time
from collections.abc import Callable

import httpx

from .home import (
    CLOSE,
    LOCK,
    OFF,
    ON,
    OPEN,
    PRESS,
    SET,
    STATUS,
    TOKEN_KEY,
    UNLOCK,
    Check,
    Device,
    HomeError,
    button,
    presses,
)

HTTP_TIMEOUT_S = 5.0
# Home Assistant answers a service call once the device has done it (a lock's bolt turning), so it gets longer.
SERVICE_TIMEOUT_S = 30.0
POLL_S = 0.5
UNKNOWN = ("unknown", "unavailable", "")


class NoAnswer(HomeError):
    """Home Assistant had the request, and didn't answer in time."""


class HomeAssistant:
    """Home Assistant's REST API. Reading a state is short: a device that doesn't answer is reported, never waited on.
    A service call waits as long as Home Assistant does for the device, up to SERVICE_TIMEOUT_S."""

    def __init__(self, url: str, token: str, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT_S,
            transport=transport,
        )

    def ping(self) -> str:
        return self._send("GET", "/api/").get("message", "")

    def close(self) -> None:
        self._http.close()

    def state(self, entity: str) -> dict:
        """{"state": ..., "attributes": {...}}; HomeError if there's no such entity."""
        return self._send("GET", f"/api/states/{entity}")

    def call(self, service: str, data: dict) -> bool:
        """Calls a service, "domain.service", with `data` (its entity_id and settings): whether Home Assistant
        answered. Not answering in time, once it has the call, isn't an error: it may well be doing it, so what the
        entity says next tells."""
        domain, _, name = service.partition(".")
        timeout = httpx.Timeout(HTTP_TIMEOUT_S, read=SERVICE_TIMEOUT_S)
        try:
            self._send("POST", f"/api/services/{domain}/{name}", data, timeout)
        except NoAnswer:
            print(f"(home: no answer to {service} after {SERVICE_TIMEOUT_S:.0f} s; reading back what it did)")
            return False
        return True

    def _send(self, method: str, path: str, data: dict | None = None, timeout: httpx.Timeout | None = None) -> dict:
        try:
            response = self._http.request(method, path, json=data, timeout=timeout or httpx.USE_CLIENT_DEFAULT)
        except httpx.ReadTimeout as e:
            raise NoAnswer(f"Home Assistant didn't answer in time ({type(e).__name__})") from e
        except httpx.HTTPError as e:
            raise HomeError(f"Home Assistant didn't answer ({type(e).__name__})") from e
        if response.status_code == 401:
            raise HomeError(f"Home Assistant refused the token ({TOKEN_KEY} in .env)")
        if response.status_code == 404:
            raise HomeError(f"Home Assistant has no {path.rsplit('/', 1)[-1]}")
        if response.status_code >= 400:
            raise HomeError(f"Home Assistant said HTTP {response.status_code}: {response.text[:200]}")
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {"result": body}


class Entity(Device):
    """A Home Assistant entity: its status read from its state, and changes made through its domain's services, then
    read back until the entity reports them, for at most `settle_s`."""

    settle_s = 5.0
    # States that mean it's on its way to what was asked ("locking"): reported as such if time runs out.
    moving: tuple[str, ...] = ()

    def __init__(
        self,
        name: str,
        spec: dict,
        hass: HomeAssistant,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        super().__init__(name)
        self.entity = spec["entity"]
        self.domain = self.entity.partition(".")[0]
        self.details: list[str] = list(spec.get("details", []))
        self._hass, self._clock, self._sleep = hass, clock, sleep

    def status(self) -> str:
        found = self._hass.state(self.entity)
        guess = " (Home Assistant's guess: it can't report back)" if _assumed(found) else ""
        parts = [f"{self.name}: {self._said(found)}{guess}", *self._extra(found)]
        parts += [_reading(self._hass, e) for e in self.details]
        return ", ".join(p for p in parts if p)

    def check(self) -> list[Check]:
        said, missing = [], []
        for entity in [self.entity, *self._entities(), *self.details]:
            try:
                said.append(f"{entity} {self._hass.state(entity).get('state')}")
            except HomeError:
                missing.append(entity)
        if missing:
            fix = "check the entity ids in Home Assistant (Settings, Devices & services, Entities) and config.toml"
            return [Check("fail", f"Home Assistant has no {', '.join(missing)}", fix)]
        if said[0].endswith(" unavailable"):
            return [Check("warn", said[0], "Home Assistant knows it but can't reach it: is it online?")]
        return [Check("ok", ", ".join(said))]

    def _entities(self) -> list[str]:
        """Other entities this one reads (a lock's door sensor)."""
        return []

    def _said(self, found: dict) -> str:
        state = found.get("state", "")
        if state == "unavailable":
            return "not reachable (Home Assistant can't reach it)"
        return state or "unknown"

    def _extra(self, found: dict) -> list[str]:
        """More of its own status, after its state."""
        return []

    def _change(self, service: str, data: dict, done: Callable[[dict], bool], asked: str) -> str:
        """Calls `service` on the entity, then reads it back until `done(state)`: "done: ...", or "sent: ..." for an
        entity Home Assistant only assumes, or "not confirmed: ..." when time runs out."""
        before = self._hass.state(self.entity)
        self._hass.call(service, {"entity_id": self.entity, **data})
        if _assumed(before):
            return f"sent: asked the {self.name} to {asked}; it can't report back, so nothing can tell if it did"
        ended, found = self._settled(lambda f: done(f) or f.get("state") == "jammed", self.settle_s)
        state = found.get("state", "")
        if state == "jammed":
            return f"error: the {self.name} jammed; someone needs to check it"
        if ended and state in self.moving:
            return f"started: {self.name} reports {state}"
        if ended:
            return f"done: {self.name} reports {self._said(found)}"
        if state == "unavailable":
            return f"not confirmed: asked the {self.name} to {asked}, but Home Assistant can't reach it"
        if state in self.moving:
            return f"not confirmed: asked the {self.name} to {asked}, and it still says {state}"
        return f"not confirmed: asked the {self.name} to {asked}, but it still says {state or 'nothing'}"

    def _settled(self, done: Callable[[dict], bool], seconds: float) -> tuple[bool, dict]:
        """Reads the entity until `done(state)`, for at most `seconds`: (whether it came, what it said last)."""
        deadline = self._clock() + seconds
        while True:
            found = self._hass.state(self.entity)
            if done(found):
                return True, found
            if self._clock() >= deadline:
                return False, found
            self._sleep(POLL_S)


def _assumed(found: dict) -> bool:
    """Whether Home Assistant only assumes the entity's state (one worked by IR): it can't report back."""
    return bool((found.get("attributes") or {}).get("assumed_state"))


def _state_is(*states: str) -> Callable[[dict], bool]:
    return lambda found: found.get("state") in states


class Lock(Entity):
    actions = (STATUS, LOCK, UNLOCK)
    settle_s = 12.0  # a Nuki turns its bolt in a few seconds, then reports it
    moving = ("locking", "unlocking")

    def __init__(self, name: str, spec: dict, hass: HomeAssistant, **kw):
        super().__init__(name, spec, hass, **kw)
        self.door, self.battery = spec.get("door", ""), spec.get("battery", "")

    def what(self) -> str:
        return "a lock: status, lock, unlock"

    def call(self, action: str, args: dict, request) -> str:
        if action == LOCK:
            if self.door and self._door_open():
                return "error: the door sensor says the door is open; locking now would throw the bolt into the frame"
            return self._change("lock.lock", {}, _state_is("locked"), "lock")
        return self._change("lock.unlock", {}, _state_is("unlocked", "open"), "unlock")

    def slow(self, action: str, args: dict) -> bool:
        return action in (LOCK, UNLOCK)

    def _entities(self) -> list[str]:
        return [e for e in (self.door, self.battery) if e]

    def _extra(self, found: dict) -> list[str]:
        door = _reading(self._hass, self.door, lambda s: {"on": "door open", "off": "door closed"}.get(s))
        return [door, _reading(self._hass, self.battery, lambda s: f"battery {s}%")]

    def _door_open(self) -> bool:
        try:
            return self._hass.state(self.door).get("state") == "on"
        except HomeError:
            return False  # a door sensor that doesn't answer doesn't stop the lock


class Switch(Entity):
    """A switch, light, fan or input_boolean: on and off, and on for some minutes with its own timer if it has one
    (a Switcher water heater's switcher_kis.turn_on_with_timer), so it goes off even if TARS doesn't."""

    actions = (STATUS, ON, OFF)

    def __init__(self, name: str, spec: dict, hass: HomeAssistant, **kw):
        super().__init__(name, spec, hass, **kw)
        self.timer: dict | None = spec.get("timer")

    def what(self) -> str:
        timed = f", or on for some minutes (up to {self.timer['max']})" if self.timer else ""
        return f"{self.domain.replace('_', ' ')}: status, on, off{timed}"

    def call(self, action: str, args: dict, request) -> str:
        minutes = args.get("minutes")
        if action == ON and minutes is not None:
            if not self.timer:
                return f"error: the {self.name} has no timer; turn it on, and set a reminder to turn it off"
            try:
                minutes = round(float(minutes))
            except TypeError, ValueError:
                return "error: minutes must be a number"
            if not 1 <= minutes <= self.timer["max"]:
                return f"error: the {self.name}'s timer is 1 to {self.timer['max']} minutes"
            result = self._change(self.timer["service"], {self.timer["field"]: minutes}, _state_is("on"), "turn on")
            return f"{result}, for {minutes} minutes" if not result.startswith(("error", "not")) else result
        service = f"{'homeassistant' if self.domain == 'input_boolean' else self.domain}.turn_{action}"
        return self._change(service, {}, _state_is(action), f"turn {action}")


class Cover(Entity):
    """Blinds, shutters, a garage door: open and close. They take a while, so "opening" is enough to answer
    ("started"); a lock's "locking" isn't, so a lock waits for "locked"."""

    actions = (STATUS, OPEN, CLOSE)
    settle_s = 3.0
    moving = ("opening", "closing")

    def what(self) -> str:
        return "a cover (blinds, shutters or a garage door): status, open, close"

    def call(self, action: str, args: dict, request) -> str:
        want = ("open", "opening") if action == OPEN else ("closed", "closing")
        return self._change(f"cover.{action}_cover", {}, _state_is(*want), action)


class Climate(Entity):
    """A thermostat or an AC: on (in a mode), off, and its temperature, mode and fan speed, from those it lists."""

    actions = (STATUS, ON, OFF, SET)

    def what(self) -> str:
        found = self._attributes()
        modes = "/".join(m for m in found.get("hvac_modes", []) if m != "off") or "its modes"
        fans = "/".join(found.get("fan_modes", []))
        low, high = found.get("min_temp"), found.get("max_temp")
        degrees = f", {low:g} to {high:g} degrees" if low is not None and high is not None else ""
        return f"heating or cooling: status, on, off, set (mode {modes}{degrees}{f', fan {fans}' if fans else ''})"

    def _extra(self, found: dict) -> list[str]:
        a = found.get("attributes") or {}
        parts = []
        if a.get("temperature") is not None:
            parts.append(f"set to {a['temperature']:g} degrees")
        if a.get("fan_mode"):
            parts.append(f"fan {a['fan_mode']}")
        if a.get("current_temperature") is not None:
            parts.append(f"{a['current_temperature']:g} degrees in the room")
        return parts

    def call(self, action: str, args: dict, request) -> str:
        found = self._hass.state(self.entity)
        a = found.get("attributes") or {}
        if action == OFF:
            return self._change("climate.turn_off", {}, _state_is("off"), "turn off")
        mode, problem = _choose(args.get("mode"), a.get("hvac_modes", []), "modes", {"fan": "fan_only"})
        if not problem:
            fan, problem = _choose(args.get("fan"), a.get("fan_modes", []), "fan speeds", {})
        if problem:
            return f"error: the {self.name} has {problem}"
        temperature = args.get("temperature")
        if temperature is not None:
            try:
                temperature = float(temperature)
            except TypeError, ValueError:
                return "error: temperature must be a number of degrees"
            low, high = a.get("min_temp", 7), a.get("max_temp", 35)
            if not low <= temperature <= high:
                return f"error: the {self.name} goes from {low:g} to {high:g} degrees"
        # Asked to change the settings of one Home Assistant guesses is off: someone turned it on with its remote. It's
        # turned on first, since an IR one may send nothing while Home Assistant has it off (SmartIR's doesn't).
        turn_on = action == ON or (action == SET and found.get("state") == "off" and _assumed(found))
        if action == SET and found.get("state") == "off" and mode is None and not turn_on:
            return f"error: the {self.name} is off; to turn it on with these settings, use on"
        steps = []  # (service, data, what it should then say)
        if mode is not None or turn_on:
            if mode is None:
                steps.append(("climate.turn_on", {}, lambda f: f.get("state") not in ("off", *UNKNOWN)))
            else:
                steps.append(("climate.set_hvac_mode", {"hvac_mode": mode}, _state_is(mode)))
        if temperature is not None:
            near = max((a.get("target_temp_step") or 0) / 2, 0.5)
            steps.append(("climate.set_temperature", {"temperature": temperature}, _near(temperature, near)))
        if fan is not None:
            steps.append(("climate.set_fan_mode", {"fan_mode": fan}, _attribute_is("fan_mode", fan)))
        if not steps:
            return "error: set needs a mode, a temperature or a fan speed"
        results = [self._change(service, data, done, "change") for service, data, done in steps]
        bad = [r for r in results if not r.startswith(("done", "sent"))]
        if bad:
            return bad[0]
        if all(r.startswith("sent") for r in results):
            asked = _said_settings(action, mode, temperature, fan)
            return f"sent: asked the {self.name} to {asked}; it can't report back, so nothing can tell if it did"
        return f"done: {self.status()} (it says so itself)"

    def _attributes(self) -> dict:
        try:
            return self._hass.state(self.entity).get("attributes") or {}
        except HomeError:
            return {}


def _choose(asked, allowed: list[str], what: str, aliases: dict[str, str]) -> tuple[str | None, str]:
    """`asked`, as one of `allowed` (any case, or an alias); (None, "") when nothing was asked."""
    if asked is None:
        return None, ""
    wanted = str(asked).strip().lower()
    wanted = aliases.get(wanted, wanted)
    for value in allowed:
        if value.lower() == wanted:
            return value, ""
    return None, f"{what} {', '.join(allowed) or '(none listed)'}, not {asked!r}"


def _attribute_is(key: str, value) -> Callable[[dict], bool]:
    return lambda found: (found.get("attributes") or {}).get(key) == value


def _near(temperature: float, within: float) -> Callable[[dict], bool]:
    """Set to `temperature`, as near as the thermostat's own steps go: 21.3 asked of one in halves reads back 21.5."""

    def done(found: dict) -> bool:
        now = (found.get("attributes") or {}).get("temperature")
        return isinstance(now, int | float) and abs(now - temperature) <= within

    return done


def _said_settings(action: str, mode, temperature, fan) -> str:
    parts = [mode or "", f"{temperature:g} degrees" if temperature is not None else ""]
    parts += [f"fan {fan}" if fan else ""]
    settings = ", ".join(p for p in parts if p)
    return f"turn on{f' at {settings}' if settings else ''}" if action == ON else f"change to {settings}"


# A media player's buttons, as Home Assistant's services.
BUTTONS = {
    "volume_up": "media_player.volume_up",
    "volume_down": "media_player.volume_down",
    "mute": "media_player.volume_mute",
    "play_pause": "media_player.media_play_pause",
    "next": "media_player.media_next_track",
    "previous": "media_player.media_previous_track",
}
PRESS_SETTLE_S = 2.0  # how long what a press does gets to show in what the player reports


class MediaPlayer(Entity):
    """A TV, a soundbar or speakers: on and off, and its buttons (volume, mute, play and pause, next and previous)."""

    actions = (STATUS, ON, OFF, PRESS)

    def what(self) -> str:
        return f"a media player: status, on, off, press ({', '.join(BUTTONS)})"

    def rules(self) -> str:
        return "For a little louder or quieter, press volume_up or volume_down 3 times; for a lot, 8."

    def _extra(self, found: dict) -> list[str]:
        a = found.get("attributes") or {}
        parts = []
        if a.get("media_title"):
            parts.append(f"playing {a['media_title']}")
        if a.get("source"):
            parts.append(f"on {a['source']}")
        if a.get("is_volume_muted"):
            parts.append("muted")
        elif a.get("volume_level") is not None:
            parts.append(f"volume {round(a['volume_level'] * 100)}%")
        return parts

    def call(self, action: str, args: dict, request) -> str:
        if action == ON:
            return self._change(
                "media_player.turn_on", {}, lambda f: f.get("state") not in ("off", *UNKNOWN), "turn on"
            )
        if action == OFF:
            return self._change("media_player.turn_off", {}, _state_is("off", "standby"), "turn off")
        pressed = button(args)
        if pressed not in BUTTONS:
            return f"error: the {self.name}'s buttons are {', '.join(BUTTONS)}"
        times = 1 if pressed == "mute" else presses(args)  # mute is set, not toggled: twice is once
        found = self._hass.state(self.entity)
        data = {"is_volume_muted": not (found.get("attributes") or {}).get("is_volume_muted")}
        sent = 0
        while sent < times:
            sent += 1
            if not self._hass.call(BUTTONS[pressed], {"entity_id": self.entity, **(data if pressed == "mute" else {})}):
                break
        said = f"{pressed}{f' {sent} times' if sent > 1 else ''}"
        if sent < times:
            said += f" (of {times}: Home Assistant stopped answering)"
        if _assumed(found):
            return f"sent: {said} to the {self.name}; it can't report back, so nothing can tell if it took"
        shown = READ_BACK[pressed]
        changed, _ = self._settled(lambda f: shown(f) != shown(found), PRESS_SETTLE_S)
        if changed:
            return f"done: pressed {said}; now {self.status()}"
        return f"sent: {said} to the {self.name}, and what it reports hasn't changed; now {self.status()}"


def _sound(found: dict) -> tuple:
    a = found.get("attributes") or {}
    return a.get("volume_level"), a.get("is_volume_muted")


def _track(found: dict) -> tuple:
    return found.get("state"), (found.get("attributes") or {}).get("media_title")


# Where each button's press shows in what the player reports.
READ_BACK: dict[str, Callable[[dict], tuple]] = {
    "volume_up": _sound,
    "volume_down": _sound,
    "mute": _sound,
    "play_pause": _track,
    "next": _track,
    "previous": _track,
}


class Sensor(Entity):
    """Something only read: a temperature, a door or window, a power meter."""

    def what(self) -> str:
        return "a sensor: status only"

    def _said(self, found: dict) -> str:
        state = super()._said(found)
        unit = (found.get("attributes") or {}).get("unit_of_measurement")
        if self.domain == "binary_sensor":
            device_class = (found.get("attributes") or {}).get("device_class", "")
            return _binary(state, device_class)
        return f"{state} {unit}" if unit and state not in UNKNOWN else state

    def call(self, action: str, args: dict, request) -> str:
        return f"error: the {self.name} can only be read"


def _binary(state: str, device_class: str) -> str:
    """A binary sensor's on and off in words: "open", "motion", "wet"."""
    words = {
        "door": ("open", "closed"),
        "window": ("open", "closed"),
        "opening": ("open", "closed"),
        "garage_door": ("open", "closed"),
        "motion": ("motion", "no motion"),
        "occupancy": ("someone there", "nobody there"),
        "moisture": ("wet", "dry"),
        "smoke": ("smoke!", "no smoke"),
        "lock": ("unlocked", "locked"),
    }.get(device_class, ("on", "off"))
    return {"on": words[0], "off": words[1]}.get(state, state)


def _reading(hass: HomeAssistant, entity: str, say: Callable[[str], str | None] | None = None) -> str:
    """Another entity's reading, as a few words, or "" if there's nothing to say: `say(state)`, or by default its
    name, state and unit ("Boiler time left: 25 min")."""
    if not entity:
        return ""
    try:
        found = hass.state(entity)
    except HomeError:
        return ""
    state = found.get("state", "")
    if state in UNKNOWN:
        return ""
    if say is not None:
        return say(state) or ""
    a = found.get("attributes") or {}
    unit = f" {a['unit_of_measurement']}" if a.get("unit_of_measurement") else ""
    return f"{a.get('friendly_name') or entity}: {state}{unit}"


KINDS: dict[str, type[Entity]] = {
    "lock": Lock,
    "switch": Switch,
    "light": Switch,
    "fan": Switch,
    "input_boolean": Switch,
    "cover": Cover,
    "climate": Climate,
    "media_player": MediaPlayer,
    "sensor": Sensor,
    "binary_sensor": Sensor,
}


def entity_device(name: str, spec: dict, hass: HomeAssistant, **timing) -> Entity:
    """The device for a [[home.devices]] entry with an entity, by its domain (config.py checked it's one of these).
    `timing`: clock and sleep, for the tests and evals."""
    return KINDS[spec["entity"].partition(".")[0]](name, spec, hass, **timing)
