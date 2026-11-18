"""A Soehnle Airfresh Clean Connect 500 air purifier, over Bluetooth LE from this machine's own adapter: no Home
Assistant, no pairing. The protocol is from github.com/narolinus/soehnle-ac500 (MIT), which read it from the official
app's Bluetooth captures: writes to ef01, a live status about every second on ef02.

Out of this machine's range (a purifier in another room), it's reached through an M5Stack Atom beside it running our
firmware (firmware/atom-lite-bt), which keeps a Bluetooth connection to it ready and passes the bytes on: Atom, the
same requests over Wi-Fi.

Each request is one short connection, since a connection held open would keep the phone app out. Connections drop now
and then, so each is tried a few times. A change is "done" only when the purifier's own status shows it.

Left unplugged for days, it comes back sending a status of another kind (a0 20) and ignores what it's sent, until it's
turned on once at its panel. Nothing remote gets it out of that, so TARS says so instead of trying.

Its Bluetooth can be switched off at its own panel, and then nothing can reach it. So the voice assistant watches it
(Watch): every few minutes it listens for the purifier's broadcast for a few seconds, never connecting, and when it's
gone quiet for a quarter of an hour, TARS's maintainers are told once, in their chats, and again when it's back.
Being turned off doesn't do it: a night's watch on 2026-11-09, off for 14 hours, heard it in every scan. When it last
heard it, and whether it's told them, are kept in a file: TARS restarting changes neither, so it's never told again,
nor told "quiet for 16 minutes" when it's been unplugged since the morning."""

import asyncio
import hashlib
import hmac
import json
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, Self

from voice_assistant.files import atomic_write
from voice_assistant.home import OFF, ON, PRESS, SET, STATUS, Check, Device, HomeError, Request, button
from voice_assistant.reminders import twelve_hour

WRITE = "0000ef01-0000-1000-8000-00805f9b34fb"
LIVE = "0000ef02-0000-1000-8000-00805f9b34fb"
POWER, FAN, UV, TIMER, AUTO, NIGHT, BUZZER = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x08
HELLO, ASK = 0xAF, 0xA2  # the app's opening of a control session; a request for a status
FANS = ("low", "medium", "high", "turbo")
MODES = ("auto", "night", "normal")
TIMER_HOURS = (2, 4, 8)
ATTEMPTS = 3
CONNECT_S = 12.0
FIRST_STATUS_S = 4.0
SETTLE_S = 6.0
FILTER_HOURS = 4320  # a filter's life, as its hours-run counter counts it (the counter goes up; resetting zeroes it)
FLAG_POWER, FLAG_UV, FLAG_TIMER, FLAG_BUZZER, FLAG_AUTO, FLAG_NIGHT = 0x01, 0x02, 0x04, 0x08, 0x20, 0x40
HELD = (
    "it's ignoring remote commands, as it does after a long time unplugged; "
    "someone has to turn it on once at its panel, and then it listens again"
)
RULES = (
    "The air purifier's fan is low, medium, high or turbo; its mode is auto, night or normal; on takes minutes only "
    "for its timer of 2, 4 or 8 hours; press takes uv_on and uv_off, and buzzer_on and buzzer_off (its beeps). Its "
    "own PM2.5 reading is rough: for how the air is, a dedicated air monitor is better."
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
    filter_used: float  # percent of its filter's life used: hours run, of 4320 (180 days); a new one reads 0

    @property
    def on(self) -> bool:
        return bool(self.flags & FLAG_POWER)

    @property
    def uv(self) -> bool:
        return bool(self.flags & FLAG_UV)

    @property
    def buzzer(self) -> bool:
        return bool(self.flags & FLAG_BUZZER)

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
        filter_used=(data[12] | data[13] << 8) / FILTER_HOURS * 100,
    )


class Link(Protocol):
    def talk(self, commands: list[bytes], done: Callable[[Status], bool]) -> Status:
        """Connects, sends `commands` and returns the status once `done(status)`, or the last one when time runs out.
        Sends nothing to a purifier that's held. Raises OSError if it can't be reached."""
        ...


class Bluetooth:
    """The link: bleak, a connection a request. One thing at a time on the adapter: a request, or the watch's
    listening."""

    def __init__(self, address: str):
        self.address = address
        self._lock = threading.Lock()

    def talk(self, commands: list[bytes], done: Callable[[Status], bool]) -> Status:
        with self._lock:
            return asyncio.run(self._talk(commands, done))

    def heard(self, seconds: float) -> bool | None:
        """Whether its broadcast is heard within `seconds`: a listen, never a connection. None: this machine couldn't
        listen at all (its adapter), which says nothing about the purifier."""
        from bleak import BleakScanner
        from bleak.exc import BleakError

        with self._lock:
            try:
                return asyncio.run(BleakScanner.find_device_by_address(self.address, timeout=seconds)) is not None
            except (BleakError, OSError) as e:
                print(f"(purifier watch: couldn't listen: {e})")
                return None

    def restart(self) -> bool:
        """This machine's Bluetooth switched off and on again, as a Pi's sometimes needs to come unstuck (bluetoothctl,
        which its users in the bluetooth group may run). Whether it came back on."""
        with self._lock:
            power("off")
            time.sleep(2)
            return power("on")

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


ATOM_PORT = 8780  # the firmware's (firmware/atom-lite-bt)
ATOM_KEY = "PURIFIER_ATOM_KEY"  # the key it and TARS share, in .env
LOOPBACK = ("127.0.0.1", "localhost")  # an "Atom" there is the bridge on this machine (bridge.py)
LINK_S = 20.0  # how long the Atom gets to connect to the purifier; it tries several times itself
REPLY_S = 5.0  # for anything else it's asked


class Atom:
    """The link through an Atom beside the purifier (firmware/atom-lite-bt), at `host` on the home network, sharing
    `key`: for a purifier too far from this machine's own Bluetooth. A request is one connection to the Atom over
    Wi-Fi, in which it hands over the connection to the purifier it keeps ready, passes on the frames sent and the
    statuses it reports, and replaces it after: the purifier takes a request's commands only on a fresh connection.
    One thing at a time."""

    def __init__(self, address: str, host: str, key: str):
        self.address, self.host, self._key = address, host, key
        self._lock = threading.Lock()

    def talk(self, commands: list[bytes], done: Callable[[Status], bool]) -> Status:
        with self._lock:
            return asyncio.run(self._talk(commands, done))

    def heard(self, seconds: float) -> bool | None:
        """Whether the Atom heard its broadcast in the last minute. None: the Atom couldn't be reached, which says
        nothing about the purifier."""
        with self._lock:
            try:
                return asyncio.run(self._heard())
            except OSError as e:
                print(f"(purifier watch: {e})")
                return None

    async def _heard(self) -> bool:
        async with _AtomSession(self.host, self._key) as atom:
            ms = int((await atom.ask("heard")).removeprefix("heard "))
            return 0 <= ms < 60_000

    async def _talk(self, commands: list[bytes], done: Callable[[Status], bool]) -> Status:
        async with _AtomSession(self.host, self._key) as atom:
            said = await atom.ask("link", LINK_S)
            if said != "linked":
                raise OSError(f"the Atom couldn't connect to it ({said.removeprefix('failed ')})")
            try:
                status = await atom.first(FIRST_STATUS_S)  # the Atom's latest, sent with "linked"
                if status.held:
                    return status
                await atom.send(frame(HELLO, 0, 1))
                await asyncio.sleep(0.12)
                await atom.send(frame(HELLO, 0, 1))
                await asyncio.sleep(0.2)
                for command in commands:
                    await atom.send(command)
                    await asyncio.sleep(0.2)
                # It reports its status about every second on its own: asking as well isn't needed.
                deadline = time.monotonic() + SETTLE_S
                while not done(atom.latest) and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                return atom.latest
            finally:
                await atom.ask("unlink")


_found: dict[str, str] = {}  # an Atom's name on the home network, and the address it was last found at


async def _ipv4(host: str) -> str:
    """`host`'s IPv4 address. Looked up for IPv4 only: a .local name's IPv6 lookup goes unanswered by the Atom, and
    waiting it out costs five seconds a request. Kept until a connection to it fails (_AtomSession forgets it)."""
    if host not in _found:
        infos = await asyncio.get_running_loop().getaddrinfo(host, ATOM_PORT, family=socket.AF_INET)
        _found[host] = infos[0][4][0]
    return _found[host]


class _AtomSession:
    """One connection to the Atom: proven with the key, then lines both ways. Statuses ("live <hex>") come whenever
    the purifier sends them; anything else answers what was asked. OSError for anything gone wrong."""

    def __init__(self, host: str, key: str):
        self.host, self._key = host, key
        self.latest: Status | None = None
        self._fresh = asyncio.Event()
        self._replies: asyncio.Queue[str] = asyncio.Queue()

    async def __aenter__(self) -> Self:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(await _ipv4(self.host), ATOM_PORT), CONNECT_S
            )
            hello = (await asyncio.wait_for(self._reader.readline(), REPLY_S)).decode().split()
            if hello[:2] != ["tars-bt", "1"] or len(hello) != 3:
                raise OSError("it isn't running TARS's firmware (firmware/atom-lite-bt)")
            proof = hmac.new(self._key.encode(), hello[2].encode(), hashlib.sha256).hexdigest()
            self._writer.write(f"auth {proof}\n".encode())
            if (await asyncio.wait_for(self._reader.readline(), REPLY_S)).strip() != b"ok":
                raise OSError(f"it refused the key: is {ATOM_KEY} the one in its secrets.h?")
        except (OSError, TimeoutError, UnicodeDecodeError) as e:
            _found.pop(self.host, None)  # it may have moved: looked up again next time
            await self._close()
            raise OSError(f"the Atom at {self.host} didn't answer: {type(e).__name__}: {e}") from e
        self._reading = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *_) -> None:
        self._reading.cancel()
        await self._close()

    async def _close(self) -> None:
        if writer := getattr(self, "_writer", None):
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _read(self) -> None:
        while line := await self._reader.readline():
            said = line.decode(errors="replace").strip()
            if said.startswith("live "):
                try:
                    found = parse(bytes.fromhex(said[5:]))
                except ValueError:
                    continue
                if found is not None:
                    self.latest = found
                    self._fresh.set()
            elif said == "unlinked" and self._asked != "unlink":
                continue  # the purifier let go unasked: the next command sent says so
            else:
                self._replies.put_nowait(said)
        self._replies.put_nowait("")  # it hung up

    _asked = ""

    async def ask(self, what: str, wait: float = REPLY_S) -> str:
        self._asked = what
        self._writer.write(f"{what}\n".encode())
        try:
            said = await asyncio.wait_for(self._replies.get(), wait)
        except TimeoutError:
            raise OSError(f"the Atom at {self.host} didn't answer {what!r}") from None
        if not said:
            raise OSError(f"the Atom at {self.host} hung up")
        return said

    async def send(self, command: bytes) -> None:
        said = await self.ask(f"send {command.hex()}")
        if said != "sent":
            raise OSError(f"the Atom couldn't send it a command ({said.removeprefix('failed ')})")

    async def first(self, wait: float) -> Status:
        """Its status: the first one, waited for."""
        try:
            await asyncio.wait_for(self._fresh.wait(), wait)
        except TimeoutError:
            raise OSError("it sent no status") from None
        assert self.latest is not None
        return self.latest


def power(state: str) -> bool:
    """This machine's Bluetooth `state`, "on" or "off": whether bluetoothctl did it."""
    try:
        done = subprocess.run(["bluetoothctl", "power", state], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"(purifier watch: couldn't switch Bluetooth {state}: {e})")
        return False
    return done.returncode == 0


class Purifier(Device):
    """The home tool's air purifier. `answered()`: told each time it answers (its watch, if it has one)."""

    actions = (STATUS, ON, OFF, SET, PRESS)

    def __init__(
        self,
        name: str,
        link: Link,
        sleep: Callable[[float], None] = time.sleep,
        answered: Callable[[], None] | None = None,
    ):
        super().__init__(name)
        self._link, self._sleep, self._answered = link, sleep, answered

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
            if pressed in ("uv_on", "uv_off"):
                on = pressed == "uv_on"
                return self._do([frame(UV, 0, int(on))], lambda s: s.uv == on, f"UV {'on' if on else 'off'}")
            if pressed in ("buzzer_on", "buzzer_off"):
                on = pressed == "buzzer_on"
                return self._do(
                    [frame(BUZZER, 0, int(on))], lambda s: s.buzzer == on, f"buzzer {'on' if on else 'off'}"
                )
            return "error: its buttons are uv_on, uv_off, buzzer_on and buzzer_off"
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
                found = self._link.talk(commands, done)
            except OSError as e:
                why = str(e)
                continue
            if self._answered:
                self._answered()
            return found
        raise HomeError(f"the {self.name} didn't answer over Bluetooth ({why})")


def _said(s: Status) -> str:
    """Its status in words."""
    if s.held:
        return f"off, and {HELD}; {s.temperature:.0f} °C"
    modes = [m for m, on in (("auto", s.auto), ("night mode", s.night), ("UV on", s.uv)) if on]
    modes += [] if s.buzzer else ["buzzer off"]
    timer = [f"timer {s.timer_hours} hours"] if s.timer_hours else []
    parts = ["on" if s.on else "off", f"fan {s.fan}", *modes, *timer]
    left = max(0, 100 - s.filter_used)
    return ", ".join([*parts, f"PM2.5 {s.pm25:.0f}", f"{s.temperature:.0f} °C", f"filter {left:.0f}% left"])


WATCH_EVERY_S = 300.0  # how often the watch listens
LISTEN_S = 10.0  # for how long: it broadcasts several times a second when its Bluetooth is on
GONE_AFTER_S = 900.0  # unheard this long, and its maintainers are told
WATCH_FILE = "voice_data/purifier_watch.json"  # when each purifier was last heard, and whether it's been told gone
STUCK_AFTER = 2  # listens in a row this machine couldn't make before its Bluetooth is restarted


class Watch:
    """Whether the purifier's Bluetooth is still on, for TARS's maintainers. `heard(seconds)`: whether its broadcast
    is heard, None if this machine couldn't listen (Bluetooth.heard). `tell(text)`: to the maintainers' chats.
    `restart()`: this machine's Bluetooth off and on (Bluetooth.restart). Told once when it's gone quiet for
    GONE_AFTER_S, and once when it's back; anything that reaches it (a request it answered) counts as heard. When
    it's this machine that can't listen, STUCK_AFTER times running, its Bluetooth is restarted first; told only if
    that doesn't fix it, and never as the purifier being gone."""

    def __init__(
        self,
        name: str,
        heard: Callable[[float], bool | None],
        tell: Callable[[str], None],
        clock: Callable[[], float] = time.time,
        restart: Callable[[], bool] | None = None,
        memory: Path | None = None,
        via: str = "",
    ):
        """`clock`: wall-clock seconds, since what's kept in `memory` outlives this process. `memory`: the file
        where when it was last heard and whether it's been told gone are kept, one entry per purifier (None: kept
        only while TARS runs). `via`: the Atom it's heard through (its address), if not this machine's Bluetooth."""
        self.name, self._heard, self._tell, self._clock, self._restart = name, heard, tell, clock, restart
        self._memory, self._via = memory, via
        kept = self._recall()
        # Never heard before (a first start): unheard only since the watch began, which is all it can say.
        self._last = float(kept.get("last_heard") or clock())
        self._known = "last_heard" in kept
        self.gone = bool(kept.get("gone"))  # told it's gone, and not yet that it's back
        self.stuck = False  # told this machine's Bluetooth won't listen, and not yet that it does
        self._failed = 0  # listens in a row this machine couldn't make

    def _recall(self) -> dict:
        if self._memory is None:
            return {}
        try:
            entry = json.loads(self._memory.read_text()).get(self.name)
        except OSError, ValueError, AttributeError:
            return {}
        return entry if isinstance(entry, dict) else {}

    def _keep(self) -> None:
        if self._memory is None:
            return
        try:
            try:
                kept = json.loads(self._memory.read_text())
            except OSError, ValueError:
                kept = {}
            if not isinstance(kept, dict):
                kept = {}
            kept[self.name] = {"gone": self.gone, **({"last_heard": self._last} if self._known else {})}
            atomic_write(self._memory, json.dumps(kept, indent=1))
        except OSError as e:  # the watch goes on; only a restart would forget
            print(f"(purifier watch: couldn't keep what it knows in {self._memory}: {e})")

    def answered(self) -> None:
        """It answered a request: plainly on."""
        self._back()

    def look(self) -> None:
        """One listen; tells the maintainers when that changes what they know."""
        heard = self._listen()
        if heard is None:
            return  # this machine's trouble: nothing known about the purifier
        if heard:
            self._back()
        elif not self.gone and self._clock() - self._last >= GONE_AFTER_S:
            self.gone = True
            self._keep()
            self._tell(
                f"The {self.name} has gone quiet on Bluetooth: {self._unheard()}, so I can't work it. It's unplugged, "
                "or its Bluetooth was switched off at its panel: plug it in, or press its Bluetooth button."
            )

    def _unheard(self) -> str:
        """For how long it's been unheard, as far as this watch knows."""
        now, last = self._clock(), self._last
        when = datetime.fromtimestamp(last).astimezone()
        at = twelve_hour(when)
        if when.date() != datetime.fromtimestamp(now).astimezone().date():
            at = f"{when:%A} at {at}"
        if not self._known:
            return f"I haven't heard it since I started listening for it, at {at}"
        return f"I last heard it at {at}, {_duration(now - last)} ago"

    def _listen(self) -> bool | None:
        """A listen, with this machine's Bluetooth restarted first when it's failed STUCK_AFTER times running."""
        heard = self._heard(LISTEN_S)
        if heard is not None:
            self._adapter_works()
            return heard
        self._failed += 1
        if self._failed < STUCK_AFTER:
            return None
        if self._restart is not None:
            print("(purifier watch: this machine's Bluetooth can't listen; restarting it)")
            self._restart()
            heard = self._heard(LISTEN_S)
            if heard is not None:
                self._adapter_works()
                return heard
        if not self.stuck:
            self.stuck = True
            if self._via in LOOPBACK:
                self._tell(
                    f"The purifier bridge on this machine isn't answering, so I can't work the {self.name}. "
                    "Restarting it usually fixes it: systemctl --user restart tars-purifier-bridge."
                )
            elif self._via:
                self._tell(
                    f"The Atom I reach the {self.name} through ({self._via}) isn't answering on Wi-Fi, so I can't "
                    "work it. Is it plugged in? Unplugging it and plugging it back in restarts it."
                )
            elif self._restart is not None:
                self._tell(
                    f"My Bluetooth has stopped working, so I can't reach the {self.name}. I restarted it and it "
                    "still can't listen; restarting me (the Pi) usually fixes it."
                )
            else:
                self._tell(f"My Bluetooth has stopped working, so I can't reach the {self.name}.")
        return None

    def _adapter_works(self) -> None:
        self._failed = 0
        if self.stuck:
            self.stuck = False
            if self._via in LOOPBACK:
                self._tell("The purifier bridge answers again.")
            else:
                self._tell(f"The Atom for the {self.name} answers again." if self._via else "My Bluetooth works again.")

    def _back(self) -> None:
        # Kept every few minutes at most (a listen, or a request it answered), so the file is no burden.
        self._last, self._known = self._clock(), True
        was_gone, self.gone = self.gone, False
        self._keep()
        if was_gone:
            self._tell(f"The {self.name} is back on Bluetooth: I can work it again.")

    def start(self, every_s: float = WATCH_EVERY_S) -> Watch:
        def keep() -> None:
            while True:
                time.sleep(every_s)
                try:
                    self.look()
                except Exception as e:  # noqa: BLE001 - a watch that fails once tries again next time
                    print(f"(purifier watch: {e!r})")

        threading.Thread(target=keep, daemon=True, name=f"watch {self.name}").start()
        return self


def _duration(seconds: float) -> str:
    """ "20 minutes", "about 3 hours", "about 2 days": how long, as said."""
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = round(seconds / 3600)
    if hours < 36:
        return f"about {hours} hours"
    return f"about {round(seconds / 86400)} days"
