"""A Soehnle Airfresh Clean Connect 500 air purifier, over Bluetooth LE from this machine's own adapter: no Home
Assistant, no pairing. The protocol is from github.com/narolinus/soehnle-ac500 (MIT), which read it from the official
app's Bluetooth captures: writes to ef01, a live status about every second on ef02.

Each request is one short connection, since a connection held open would keep the phone app out. Connections drop now
and then, so each is tried a few times. A change is "done" only when the purifier's own status shows it.

Left unplugged for days, it comes back sending a status of another kind (a0 20) and ignores what it's sent, until it's
turned on once at its panel. Nothing remote gets it out of that, so TARS says so instead of trying."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from voice_assistant.home import OFF, ON, PRESS, SET, STATUS, Check, Device, HomeError, Request, button

WRITE = "0000ef01-0000-1000-8000-00805f9b34fb"
LIVE = "0000ef02-0000-1000-8000-00805f9b34fb"
POWER, FAN, UV, TIMER, AUTO, NIGHT = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06
HELLO, ASK = 0xAF, 0xA2  # the app's opening of a control session; a request for a status
FANS = ("low", "medium", "high", "turbo")
MODES = ("auto", "night", "normal")
TIMER_HOURS = (2, 4, 8)
ATTEMPTS = 3
CONNECT_S = 12.0
FIRST_STATUS_S = 4.0
SETTLE_S = 6.0
FLAG_POWER, FLAG_UV, FLAG_TIMER, FLAG_AUTO, FLAG_NIGHT = 0x01, 0x02, 0x04, 0x20, 0x40
HELD = (
    "it's ignoring remote commands, as it does after a long time unplugged; "
    "someone has to turn it on once at its panel, and then it listens again"
)
RULES = (
    "The air purifier's fan is low, medium, high or turbo; its mode is auto, night or normal; on takes minutes only "
    "for its timer of 2, 4 or 8 hours; press takes uv_on and uv_off. Its own PM2.5 reading is rough: for how the air "
    "is, a dedicated air monitor is better."
)


def frame(opcode: int, arg1: int = 0, arg2: int = 0) -> bytes:
    """A command: aa 03 <opcode> <arg1> <arg2> <sum> ee."""
    return bytes([0xAA, 0x03, opcode, arg1, arg2, (0x03 + opcode + arg1 + arg2) & 0xFF, 0xEE])


@dataclass(frozen=True)
class Status:
    """What a live status says."""

    held: bool  # the a0 20 kind: it won't take commands
    fan: str
    timer_hours: int
    flags: int
    pm25: float
    temperature: float
    filter_percent: float

    @property
    def on(self) -> bool:
        return bool(self.flags & FLAG_POWER)

    @property
    def uv(self) -> bool:
        return bool(self.flags & FLAG_UV)

    @property
    def auto(self) -> bool:
        return bool(self.flags & FLAG_AUTO)

    @property
    def night(self) -> bool:
        return bool(self.flags & FLAG_NIGHT)


def parse(data: bytes) -> Status | None:
    """A live status, or None for anything else. Its checksum covers everything from its length on."""
    if len(data) != 17 or data[0] != 0xAA or data[-1] != 0xEE or data[2] != 0xA0 or data[3] not in (0x20, 0x21, 0x22):
        return None
    if sum(data[1:-2]) & 0xFF != data[-2]:
        return None
    pm = data[7] << 8 | data[8] if data[3] == 0x22 else data[8] | data[9] << 8
    return Status(
        held=data[3] == 0x20,
        fan=FANS[data[4]] if data[4] < len(FANS) else "unknown",
        timer_hours=data[5],
        flags=data[6],
        pm25=pm / 10,
        temperature=data[10] / 10,
        filter_percent=(data[12] | data[13] << 8) / 4320 * 100,
    )


class Link(Protocol):
    def talk(self, commands: list[bytes], done: Callable[[Status], bool]) -> Status:
        """Connects, sends `commands` and returns the status once `done(status)`, or the last one when time runs out.
        Sends nothing to a purifier that's held. Raises OSError if it can't be reached."""
        ...


class Bluetooth:
    """The link: bleak, a connection a request."""

    def __init__(self, address: str):
        self.address = address

    def talk(self, commands: list[bytes], done: Callable[[Status], bool]) -> Status:
        return asyncio.run(self._talk(commands, done))

    async def _talk(self, commands: list[bytes], done: Callable[[Status], bool]) -> Status:
        from bleak import BleakClient
        from bleak.exc import BleakError

        latest: list[Status] = []
        heard = asyncio.Event()

        def live(_, data: bytearray) -> None:
            if status := parse(bytes(data)):
                latest[:] = [status]
                heard.set()

        try:
            async with BleakClient(self.address, timeout=CONNECT_S) as client:
                await client.start_notify(LIVE, live)
                try:
                    await asyncio.wait_for(heard.wait(), FIRST_STATUS_S)
                except TimeoutError:
                    raise OSError("it sent no status") from None
                if latest[0].held:
                    return latest[0]
                for command in [frame(HELLO, 0, 1), frame(HELLO, 0, 1), *commands]:
                    await client.write_gatt_char(WRITE, command, response=True)
                    await asyncio.sleep(0.2)
                deadline = time.monotonic() + SETTLE_S
                while not done(latest[0]) and time.monotonic() < deadline:
                    await client.write_gatt_char(WRITE, frame(ASK, 0, 3), response=True)
                    await asyncio.sleep(1.0)
                return latest[0]
        except (BleakError, EOFError, TimeoutError) as e:
            raise OSError(f"{type(e).__name__}: {e}") from e


class Purifier(Device):
    """The home tool's air purifier."""

    actions = (STATUS, ON, OFF, SET, PRESS)

    def __init__(self, name: str, link: Link, sleep: Callable[[float], None] = time.sleep):
        super().__init__(name)
        self._link, self._sleep = link, sleep

    def what(self) -> str:
        return f"an air purifier: status, on, off, set (fan: {', '.join(FANS)}; mode: {', '.join(MODES)}), press"

    def rules(self) -> str:
        return RULES

    def slow(self, action: str, args: dict) -> bool:
        return True

    def status(self) -> str:
        return f"{self.name}: {_said(self._talk([], lambda _: True))}"

    def check(self) -> list[Check]:
        try:
            found = self._talk([], lambda _: True)
        except HomeError as e:
            return [Check("warn", str(e), "is it plugged in, and in range of this machine's Bluetooth?")]
        return [
            Check("warn", _said(found), "turn it on once at its panel") if found.held else Check("ok", _said(found))
        ]

    def call(self, action: str, args: dict, request: Request) -> str:
        if action == OFF:
            return self._do([frame(POWER, 0, 0)], lambda s: not s.on, "off")
        if action == ON:
            return self._on(args.get("minutes"))
        if action == PRESS:
            pressed = button(args)
            if pressed not in ("uv_on", "uv_off"):
                return "error: its buttons are uv_on and uv_off"
            on = pressed == "uv_on"
            return self._do([frame(UV, 0, int(on))], lambda s: s.uv == on, f"UV {'on' if on else 'off'}")
        return self._set(args.get("fan"), args.get("mode"))

    def _on(self, minutes) -> str:
        commands, hours = [frame(POWER, 0, 1)], 0
        if minutes:
            hours = round(float(minutes) / 60)
            if hours not in TIMER_HOURS:
                return f"error: its timer is {', '.join(map(str, TIMER_HOURS))} hours; nothing changed"
            commands.append(frame(TIMER, 0, hours))
        asked = f"on for {hours} hours" if hours else "on"
        return self._do(commands, lambda s: s.on and (not hours or s.timer_hours == hours), asked)

    def _set(self, fan, mode) -> str:
        fan, mode = str(fan or "").strip().lower(), str(mode or "").strip().lower()
        if fan and fan not in FANS:
            return f"error: its fan is {', '.join(FANS)}; nothing changed"
        if mode and mode not in MODES:
            return f"error: its mode is {', '.join(MODES)}; nothing changed"
        if not (fan or mode):
            return "error: say a fan or a mode to set; nothing changed"
        commands = []
        if mode:
            commands += [frame(AUTO, 0, int(mode == "auto")), frame(NIGHT, 0, int(mode == "night"))]
        elif fan:
            commands.append(frame(AUTO, 0, 0))  # a fixed speed leaves auto, as the app does first
        if fan:
            commands.append(frame(FAN, 0, FANS.index(fan)))

        def done(s: Status) -> bool:
            kept = not mode or (s.auto == (mode == "auto") and s.night == (mode == "night"))
            return kept and (not fan or s.fan == fan)

        return self._do(commands, done, " and ".join(x for x in (mode and f"{mode} mode", fan and f"fan {fan}") if x))

    def _do(self, commands: list[bytes], done: Callable[[Status], bool], asked: str) -> str:
        found = self._talk(commands, done)
        if found.held:
            return f"not confirmed: the {self.name} isn't {asked}: {HELD}"
        if done(found):
            return f"done: {self.name} reports {asked}"
        return f"not confirmed: asked for {asked}, and it reports {_said(found)}"

    def _talk(self, commands: list[bytes], done: Callable[[Status], bool]) -> Status:
        """One request, tried again if the connection fails: what was sent is safe to send twice, each says a state."""
        why = ""
        for attempt in range(ATTEMPTS):
            if attempt:
                self._sleep(1.0)
            try:
                return self._link.talk(commands, done)
            except OSError as e:
                why = str(e)
        raise HomeError(f"the {self.name} didn't answer over Bluetooth ({why})")


def _said(s: Status) -> str:
    """Its status in words."""
    if s.held:
        return f"off, and {HELD}; {s.temperature:.0f} °C"
    modes = [m for m, on in (("auto", s.auto), ("night mode", s.night), ("UV on", s.uv)) if on]
    timer = [f"timer {s.timer_hours} hours"] if s.timer_hours else []
    parts = ["on" if s.on else "off", f"fan {s.fan}", *modes, *timer]
    return ", ".join([*parts, f"PM2.5 {s.pm25:.0f}", f"{s.temperature:.0f} °C", f"filter {s.filter_percent:.0f}%"])
