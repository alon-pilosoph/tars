"""The AC, through a Tuya IR blaster: codes made exactly as the AC's own remote makes them, from a profile learned
from that remote (ac_onboard.py), and TARS's own idea of whether the AC is on.

An AC remote sends its whole display with every press: mode, temperature, fan, swing. Some, like Electra's, also
send power as a toggle, a bit that means "switch over": the same press turns the AC on when it's off and off when it's
on, and a press without it changes the settings and leaves the power alone. IR goes one way, so TARS can't ask the AC
which it is: it keeps track of what it sent (voice_data/ac/state.json), and is told when that's wrong ("the AC is on").
With a sensor that sees the AC's own light ([[home.devices]] state), it reads that instead, toggles only when it has
to, and checks the light changed."""

import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .home import IS_OFF, IS_ON, OFF, ON, SET, HomeAssistant, HomeError

MODES = ("cool", "heat", "fan", "dry", "auto")
FANS = ("low", "medium", "high", "auto")
POWER, MODE, TEMPERATURE, FAN, SWING = "power", "mode", "temperature", "fan", "swing"
PRESS, NO_PRESS = "press", "none"  # the power field's values: the power button pressed, or not
MANCHESTER, DISTANCE = "manchester", "distance"
TUYA_KEY = "TUYA_LOCAL_KEY"  # the blaster's local key, in .env
# The AC's learned profile, and whether TARS thinks it's on: in this folder, under the project's root.
FOLDER, PROFILE, STATE = Path("voice_data/ac"), "profile.json", "state.json"
# How long the light sensor gets to show the AC came on (or went off) before the toggle is sent again, once.
SENSOR_WAIT_S = 6.0
SENSOR_POLL_S = 0.5
MAX_PULSE_US = 65535  # a Tuya code is 16-bit microseconds
# A blaster that doesn't answer is given up on in seconds, so a voice turn says so rather than going quiet: tinytuya's
# defaults (5 s, five tries, 5 s apart) took a minute.
BLASTER_TIMEOUT_S, BLASTER_TRIES, BLASTER_RETRY_S = 2.0, 2, 0.3
TUYA_PORT, REACH_TIMEOUT_S = 6668, 1.5


@dataclass
class Settings:
    mode: str = "cool"
    temperature: int = 24
    fan: str = "auto"
    swing: bool = False

    def said(self) -> str:
        swing = ", swing on" if self.swing else ""
        return f"{self.mode} {self.temperature}, fan {self.fan}{swing}"


class Profile:
    """How this AC's remote writes a code, as ac_onboard.py learned it: a frame of bits, where each setting is, the
    bits each value is written as, and the timing the bits are sent with."""

    def __init__(self, raw: dict):
        try:
            self.scheme: str = raw["scheme"]
            self.timing: dict = raw["timing"]
            self.repeats: int = int(raw.get("repeats", 1))
            self.template: str = raw["bits"]
            self.fields: dict[str, dict] = raw["fields"]
            self.checksum: dict | None = raw.get("checksum")
            self.checked: bool = bool(raw.get("checked", False))
            broken = [n for n, f in self.fields.items() if any(len(v) != len(f["at"]) for v in f["values"].values())]
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            raise ValueError(f"not an AC profile ({e!r}); run ac_onboard fit again") from None
        if self.scheme not in (MANCHESTER, DISTANCE) or set(self.template) - {"0", "1"}:
            raise ValueError("not an AC profile: unknown scheme or bits; run ac_onboard fit again")
        if broken:
            raise ValueError(f"the profile's {broken[0]} field is broken; run ac_onboard fit again")
        if POWER not in self.fields:
            raise ValueError("the profile has no power bit; the AC can be set, but not turned on or off")

    @classmethod
    def load(cls, path: Path) -> Profile:
        if not path.exists():
            raise ValueError(f"there's no AC profile at {path} yet: run python -m voice_assistant.ac_onboard")
        return cls(json.loads(path.read_text()))

    def values(self, field_name: str) -> list[str]:
        return list(self.fields.get(field_name, {}).get("values", {}))

    def temperatures(self) -> tuple[int, int] | None:
        temps = [int(t) for t in self.values(TEMPERATURE)]
        return (min(temps), max(temps)) if temps else None

    def bits(self, s: Settings, toggle: bool) -> str:
        """The frame for these settings: KeyError if the profile has no code for one of them."""
        bits = list(self.template)
        wanted = {
            POWER: PRESS if toggle else NO_PRESS,
            MODE: s.mode,
            TEMPERATURE: str(s.temperature),
            FAN: s.fan,
            SWING: "on" if s.swing else "off",
        }
        for name, f in self.fields.items():
            code = f["values"][wanted[name]]
            for at, bit in zip(f["at"], code, strict=True):
                bits[at] = bit
        frame = "".join(bits)
        return with_checksum(frame, self.checksum) if self.checksum else frame

    def pulses(self, s: Settings, toggle: bool) -> list[int]:
        return frame_pulses(self.bits(s, toggle), self.scheme, self.timing, self.repeats)


def with_checksum(frame: str, checksum: dict) -> str:
    """The frame with its last byte replaced by the sum of the others, as some remotes check them (kind "sum8"),
    each byte read in `order` ("lsb": first bit sent is the lowest)."""
    body = frame[:-8]
    lsb = checksum.get("order") == "lsb"
    total = sum(_byte(body[i : i + 8], lsb) for i in range(0, len(body), 8)) & 0xFF
    last = format(total, "08b")
    return body + (last[::-1] if lsb else last)


def _byte(bits: str, lsb: bool) -> int:
    return int(bits[::-1] if lsb else bits, 2)


def frame_pulses(bits: str, scheme: str, timing: dict, repeats: int) -> list[int]:
    """The marks and spaces, in microseconds, that send `bits` `repeats` times, starting and ending with a mark."""
    if scheme == MANCHESTER:
        return _manchester(bits, timing, repeats)
    out: list[int] = []
    for i in range(repeats):
        out += [timing["header"][0], timing["header"][1]]
        for bit in bits:
            out += [timing["mark"], timing["one" if bit == "1" else "zero"]]
        out.append(timing["trailer"])
        if i < repeats - 1:
            out.append(timing["gap"])
    return _checked(out)


def _manchester(bits: str, timing: dict, repeats: int) -> list[int]:
    """Each bit is two half-bits (`timing["one"]` for a 1, e.g. "01": a space, then a mark), each `unit` long; a run
    of the same level is one mark or space. Frames start with a header (`header` units of mark, then of space); with
    no gap they follow each other directly and only the last ends with the trailer mark, else each ends with it and
    the gap (microseconds) comes before the next."""
    one, unit, trailer, gap = timing["one"], timing["unit"], timing["trailer"], timing.get("gap", 0)
    head_mark, head_space = timing["header"]
    frame = "1" * head_mark + "0" * head_space + "".join(one if b == "1" else one[::-1] for b in bits)
    if not gap:
        return _checked([round(n * unit) for _, n in _runs(frame * repeats + "1" * trailer)])
    out: list[int] = []
    for i in range(repeats):
        out += [round(n * unit) for _, n in _runs(frame + "1" * trailer)]
        if i < repeats - 1:
            out.append(int(gap))
    return _checked(out)


def _runs(levels: str) -> list[tuple[str, int]]:
    runs: list[tuple[str, int]] = []
    for level in levels:
        if runs and runs[-1][0] == level:
            runs[-1] = (level, runs[-1][1] + 1)
        else:
            runs.append((level, 1))
    return runs


def _checked(pulses: list[int]) -> list[int]:
    if any(not 0 < p <= MAX_PULSE_US for p in pulses):
        raise ValueError("a mark or space is out of what the blaster can send")
    return pulses


class Blaster:
    """The Tuya IR blaster, on the home network (tinytuya: local, no cloud)."""

    def __init__(self, device_id: str, address: str, local_key: str, version: str = "3.3"):
        self._id, self._address, self._key, self._version = device_id, address, local_key, float(version)
        self._control_type = 0  # which of Tuya's two IR protocols it speaks, found on first use

    def _device(self):
        from tinytuya.Contrib.IRRemoteControlDevice import IRRemoteControlDevice

        # One quick look first: finding out which of Tuya's protocols it speaks takes three tries, each with its own
        # timeout, before a blaster that's off the Wi-Fi is given up on.
        try:
            socket.create_connection((self._address, TUYA_PORT), timeout=REACH_TIMEOUT_S).close()
        except OSError as e:
            raise HomeError(f"the IR blaster didn't answer at {self._address} ({type(e).__name__})") from e

        ir = IRRemoteControlDevice(
            self._id,
            self._address,
            self._key,
            version=self._version,
            control_type=self._control_type or None,
            connection_timeout=BLASTER_TIMEOUT_S,
            connection_retry_limit=BLASTER_TRIES,
            connection_retry_delay=BLASTER_RETRY_S,
        )
        if not ir.control_type:
            raise HomeError(f"the IR blaster didn't answer at {self._address}")
        self._control_type = ir.control_type
        return ir

    def connect(self) -> None:
        """Raises HomeError if it doesn't answer."""
        self._device()

    def send(self, pulses: list[int]) -> None:
        from tinytuya.Contrib.IRRemoteControlDevice import IRRemoteControlDevice

        try:
            result = self._device().send_button(IRRemoteControlDevice.pulses_to_base64(pulses))
        except OSError as e:
            raise HomeError(f"the IR blaster didn't answer ({type(e).__name__})") from e
        if isinstance(result, dict) and "Error" in result:
            raise HomeError(f"the IR blaster said: {result['Error']}")

    def learn(self, timeout_s: float = 20.0) -> list[int] | None:
        """The next code a remote sends it, as marks and spaces, or None if nothing came."""
        from tinytuya.Contrib.IRRemoteControlDevice import IRRemoteControlDevice

        code = self._device().receive_button(timeout=timeout_s)
        if not isinstance(code, str):
            return None
        return IRRemoteControlDevice.base64_to_pulses(code)


@dataclass
class State:
    on: bool = False
    settings: Settings | None = None
    how: str = "sent"  # sent (TARS's own record), told (someone said), sensor (its light)
    at: float = 0.0

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            raw = json.loads(path.read_text())
            settings = Settings(**raw["settings"]) if raw.get("settings") else None
            return cls(bool(raw["on"]), settings, raw.get("how", "sent"), float(raw.get("at", 0)))
        except FileNotFoundError, KeyError, TypeError, ValueError:
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**asdict(self), "settings": asdict(self.settings) if self.settings else None}))
        tmp.replace(path)


class AC:
    """The home tool's AC: on, off and set send codes; is_on and is_off correct what TARS thinks, sending nothing."""

    def __init__(
        self,
        profile: Profile,
        send: Callable[[list[int]], None],
        state_path: Path,
        hass: HomeAssistant | None = None,
        sensor: str = "",
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.profile, self._send, self._path = profile, send, state_path
        self._hass, self._sensor = hass, sensor if hass else ""
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()

    def status(self) -> str:
        state, seen = State.load(self._path), self._seen()
        settings = (state.settings or self._default()).said()
        if seen is not None:
            return f"AC: {'on' if seen else 'off'} (its light says so), last set to {settings}"
        how = "someone said so" if state.how == "told" else "as far as TARS knows: it can't see the AC"
        return f"AC: {'on' if state.on else 'off'} ({how}), last set to {settings}"

    def what(self) -> str:
        temps = self.profile.temperatures()
        degrees = f", {temps[0]} to {temps[1]} degrees" if temps else ""
        modes = "/".join(self.profile.values(MODE))
        return f"the air conditioner: status, on, off, set (mode {modes}{degrees}, fan), is_on, is_off"

    def call(self, action: str, args: dict, request=None) -> str:
        with self._lock:
            state = State.load(self._path)
            if action in (IS_ON, IS_OFF):
                state.on, state.how, state.at = action == IS_ON, "told", self._clock()
                state.save(self._path)
                return f"noted: the AC is {'on' if state.on else 'off'}; nothing was sent"
            settings, problem = self._settings(state, args)
            if problem:
                return f"error: {problem}"
            seen = self._seen()
            on_now = state.on if seen is None else seen
            if action == SET and not on_now:
                return "error: the AC is off as far as TARS knows; to turn it on with these settings, use on"
            toggle = (action == ON) != on_now if action in (ON, OFF) else False
            if action == OFF and not toggle:
                return "already off as far as TARS knows, so nothing was sent; if it's on, say so and ask again"
            try:
                pulses = self.profile.pulses(settings, toggle)
            except KeyError as e:
                return f"error: the remote was never recorded sending {e.args[0]!r}, so there's no code for it"
            self._send(pulses)
            state.settings, state.at = settings, self._clock()
            if toggle:
                state.on, state.how = action == ON, "sent"
            state.save(self._path)
            if not toggle:
                seen = "its light says" if self._sensor else "TARS thinks"
                return f"sent: {settings.said()}; {seen} the AC was already on, so its power wasn't touched"
            if self._sensor:
                return self._confirm(state, settings, action == ON)
            verb = "turn on" if action == ON else "turn off"
            set_to = f", set to {settings.said()}" if action == ON else ""
            return f"sent: {verb}{set_to}; TARS can't see the AC, so if it didn't, someone should say so"

    def _confirm(self, state: State, settings: Settings, want_on: bool) -> str:
        """With a light sensor: wait for the light to change, and send the toggle again once if it still shows the
        old state. A sensor that doesn't answer gets no second toggle: it could turn the AC straight back."""
        seen = None
        for attempt in (1, 2):
            deadline = self._clock() + SENSOR_WAIT_S
            while self._clock() < deadline:
                seen = self._seen()
                if seen is want_on:
                    state.on, state.how = want_on, "sensor"
                    state.save(self._path)
                    tried = " (on the second try)" if attempt == 2 else ""
                    return f"done: the AC is {'on' if want_on else 'off'}{tried}, its light says so"
                self._sleep(SENSOR_POLL_S)
            if seen is None:
                return "not confirmed: sent it, but the AC's light sensor didn't answer"
            if attempt == 1:
                self._send(self.profile.pulses(settings, True))
        state.on, state.how = seen, "sensor"
        state.save(self._path)
        return f"not confirmed: sent it twice, and the AC's light still says {'on' if seen else 'off'}"

    def _seen(self) -> bool | None:
        """What the AC's light says (on or off), or None without a sensor, or one that doesn't answer."""
        if not self._sensor:
            return None
        try:
            state = self._hass.state(self._sensor).get("state")
        except HomeError:
            return None
        return {"on": True, "off": False}.get(state)

    def _default(self) -> Settings:
        p = self.profile
        mode = "cool" if "cool" in p.values(MODE) else (p.values(MODE) or ["cool"])[0]
        fan = "auto" if "auto" in p.values(FAN) else (p.values(FAN) or ["auto"])[0]
        temps = p.temperatures()
        temperature = 24 if not temps or temps[0] <= 24 <= temps[1] else temps[0]
        return Settings(mode, temperature, fan)

    def _settings(self, state: State, args: dict) -> tuple[Settings, str]:
        """The settings to send: the last ones, with what was asked changed; or (_, what's wrong)."""
        s = Settings(**asdict(state.settings or self._default()))
        p = self.profile
        mode, fan, temperature = args.get("mode"), args.get("fan"), args.get("temperature")
        if mode is not None:
            mode = str(mode).strip().lower()
            if mode not in p.values(MODE):
                return s, f"this AC's codes have modes {', '.join(p.values(MODE))}, not {mode!r}"
            s.mode = mode
        if fan is not None:
            fan = {"med": "medium", "mid": "medium"}.get(str(fan).strip().lower(), str(fan).strip().lower())
            if fan not in p.values(FAN):
                return s, f"this AC's codes have fan speeds {', '.join(p.values(FAN))}, not {fan!r}"
            s.fan = fan
        if temperature is not None:
            temps = p.temperatures()
            try:
                t = round(float(temperature))
            except TypeError, ValueError:
                return s, "temperature must be a number of degrees"
            if not temps or not temps[0] <= t <= temps[1]:
                low, high = temps or (0, 0)
                return s, f"this AC goes from {low} to {high} degrees"
            s.temperature = t
        if SWING not in p.fields:
            s.swing = False
        return s, ""
