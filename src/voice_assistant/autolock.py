"""Auto-lock that knows whether the door is shut (docs/home.md#auto-lock): a lock in [home] with a door sensor and
auto_lock is locked by TARS, never while the door is open.

The rules, for each unlocking (the lock going from locked to unlocked or open):

1. The door opened and shut again: locked `after_shut_s` (10 s) after it shut, if it's still shut. Opened again
   meanwhile, the count starts over when it shuts.
2. Unlocked and never opened (an Auto Unlock as someone walks past, the app): locked `unopened_s` (60 s) after.
3. Never while the door sensor says it's open: the door is read again in Home Assistant just before locking.
4. Tried once each unlocking: if the lock doesn't say locked within LOCK_SETTLE_S (jammed, the door not quite shut),
   the maintainers are told, and nothing is tried again until it's next unlocked.
5. The door left open for `open_alert_min` (2 minutes): said aloud at home, or posted to everyone's Telegram chat if
   nobody is home; once each time it's opened.
6. The sensor not answering (unknown or unavailable in Home Assistant): its last word isn't trusted, the lock is
   locked by the clock, `no_sensor_s` after it was unlocked, as a lock's own timer would, and the maintainers are told
   once until it's back.
7. Turned off (the home tool's "<door> auto-lock", off for some minutes): nothing is locked, nor said about the door
   being open, until it's back on; it comes back by itself. Kept in a file beside the event log, so a chat's process
   can turn it off and the assistant, which locks, reads it.

TARS down means nothing locks the door: the lock's own auto-lock, with a long timer, is the backstop (docs).
"""

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .home import OFF, ON, STATUS, Device, HomeError, Request, set_off_by_door
from .reminders import twelve_hour

FILE = "auto_lock.json"  # in the event log's folder: { lock entity: off until (a time), ... }
UNLOCKED = ("unlocked", "open")  # Home Assistant's lock states for not locked; "open" is unlatched
LOCK_SETTLE_S = 20.0  # a Nuki turns its bolt in a few seconds, then reports it
TICK_S = 1.0
DEFAULT_OFF_MIN, MOST_OFF_MIN = 60.0, 12 * 60.0


@dataclass(frozen=True)
class Settings:
    """[[home.devices]] auto_lock, for a lock: true, or a table of these."""

    after_shut_s: float = 10.0
    unopened_s: float = 60.0
    open_alert_min: float = 2.0  # 0: never said
    no_sensor_s: float = 30.0

    @classmethod
    def of(cls, value) -> Settings | None:
        if value is True:
            return cls()
        if isinstance(value, dict):
            return cls(**value)
        return None


RANGES = {"after_shut_s": (0, 300), "unopened_s": (10, 600), "open_alert_min": (0, 60), "no_sensor_s": (10, 600)}


def problems(value, where: str) -> list[str]:
    """What's wrong with a lock's auto_lock."""
    if value is True or value is False:
        return []
    if not isinstance(value, dict):
        return [f"{where}: auto_lock must be true, or a table like {{ after_shut_s = 10 }}"]
    found = [f"{where}: auto_lock has no setting {k}" for k in value if k not in RANGES]
    for key, (low, high) in RANGES.items():
        v = value.get(key)
        if key in value and (isinstance(v, bool) or not isinstance(v, int | float) or not low <= v <= high):
            found.append(f"{where}: auto_lock {key} must be between {low} and {high}")
    return found


class Off:
    """Whether a lock's auto-lock is turned off, and until when: a file both processes read and write."""

    def __init__(self, folder: Path, entity: str, clock: Callable[[], float] = time.time):
        self.path, self.entity, self._clock = folder / FILE, entity, clock

    def until(self) -> float | None:
        """When it comes back on, while it's off; None while it's on."""
        until = self._read().get(self.entity)
        return until if isinstance(until, int | float) and until > self._clock() else None

    def set(self, until: float | None) -> None:
        found = self._read()
        if until is None:
            found.pop(self.entity, None)
        else:
            found[self.entity] = until
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_name(f".{FILE}.{os.getpid()}")
        part.write_text(json.dumps(found))
        part.replace(self.path)

    def _read(self) -> dict:
        try:
            found = json.loads(self.path.read_text())
        except OSError, ValueError:
            return {}
        return found if isinstance(found, dict) else {}


class AutoLockDevice(Device):
    """The home tool's "<door> auto-lock": on, or off for a while (it comes back on by itself)."""

    actions = (STATUS, ON, OFF)

    def __init__(self, door: str, settings: Settings, off: Off):
        super().__init__(f"{door} auto-lock")
        self.door, self.settings, self.off = door, settings, off

    def what(self) -> str:
        return (
            f"locks the {self.door} {self.settings.after_shut_s:.0f} s after it shuts, never while it's open: status; "
            f"off keeps it from locking for `minutes` (null: {DEFAULT_OFF_MIN:.0f}; at most {MOST_OFF_MIN:.0f}), then "
            "it's back on by itself; on"
        )

    def rules(self) -> str:
        return (
            "An auto-lock turned off only stops TARS locking: it unlocks nothing, and the lock's own timer, if it has "
            "one, may still lock it. Say until when it's off."
        )

    def status(self) -> str:
        return f"{self.name}: {self._said()}"

    def call(self, action: str, args: dict, request: Request) -> str:
        if refused := set_off_by_door(request):
            return refused
        if action == ON:
            self.off.set(None)
            return f"done: {self.name} is on"
        try:
            minutes = DEFAULT_OFF_MIN if args.get("minutes") is None else float(args["minutes"])
        except TypeError, ValueError:
            minutes = -1.0
        if not 0 < minutes <= MOST_OFF_MIN:
            return f"error: it can be off for up to {MOST_OFF_MIN:.0f} minutes"
        self.off.set(time.time() + minutes * 60)
        return f"done: {self.name} is {self._said()}"

    def _said(self) -> str:
        until = self.off.until()
        if until is None:
            return "on"
        return f"off until {twelve_hour(datetime.fromtimestamp(until).astimezone())}"


class AutoLock:
    """One lock's auto-lock, run in the assistant. `read(entity)` is Home Assistant's state now; `lock()` asks it to
    lock. `say(text)` says it aloud at home; `post(text)` posts it to everyone's chat; `anyone_home()` is whether
    someone is (None: not known); `tell(text)` tells the maintainers."""

    def __init__(
        self,
        door: str,
        lock_entity: str,
        sensor: str,
        settings: Settings,
        off: Off,
        read: Callable[[str], str],
        lock: Callable[[], None],
        say: Callable[[str], None],
        post: Callable[[str], None],
        tell: Callable[[str], None],
        anyone_home: Callable[[], bool | None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.door, self.lock_entity, self.sensor, self.settings, self.off = door, lock_entity, sensor, settings, off
        self._read, self._lock, self._say, self._post, self._tell = read, lock, say, post, tell
        self._anyone_home, self._clock = anyone_home, clock
        self._mutex = threading.Lock()
        self.lock_state = self.door_state = ""
        self.unlocked_at: float | None = None  # this unlocking; None while locked
        self.opened = False  # the door opened since it was unlocked
        self.shut_at: float | None = None
        self.open_since: float | None = None
        self.alerted = False  # said it's open, this opening
        self.tried_at: float | None = None  # locking was asked, this unlocking
        self.sensor_down_told = False

    def start_from(self, lock_state: str, door_state: str) -> None:
        """What Home Assistant says now, as TARS starts: unlocked now counts as just unlocked."""
        self.changed(self.sensor, door_state)
        self.changed(self.lock_entity, lock_state)

    def changed(self, entity: str, state: str) -> None:
        """An entity's new state, as Home Assistant reports it (hass.follow)."""
        now = self._clock()
        with self._mutex:
            if entity == self.lock_entity:
                self._lock_changed(state, now)
            elif entity == self.sensor:
                self._door_changed(state, now)

    def _lock_changed(self, state: str, now: float) -> None:
        was, self.lock_state = self.lock_state, state
        if state in UNLOCKED and was not in UNLOCKED:
            self.unlocked_at, self.opened, self.tried_at = now, self.door_state == "on", None
            self.shut_at = None if self.door_state == "on" else now
            print(f"(auto-lock: the {self.door} unlocked)")
        elif state == "locked":
            self.unlocked_at = self.tried_at = None

    def _door_changed(self, state: str, now: float) -> None:
        was, self.door_state = self.door_state, state
        if state == "on" and was != "on":
            self.opened, self.open_since, self.shut_at, self.alerted = True, now, None, False
        elif state == "off" and was != "off":
            self.open_since, self.shut_at = None, now
        if state in ("on", "off") and self.sensor_down_told:
            self.sensor_down_told = False
            self._tell(f"The {self.door}'s sensor is answering again: TARS locks it once it's shut, as before.")

    def step(self) -> None:
        """What's due now: locking, checking it locked, saying the door's open. Called every TICK_S."""
        now = self._clock()
        off = self.off.until() is not None
        with self._mutex:
            alert = self._open_too_long(now) and not off
            lock = not off and self._lock_due(now)
            check = self.tried_at is not None and now - self.tried_at >= LOCK_SETTLE_S and self.unlocked_at is not None
            if check:
                self.tried_at = float("inf")  # told once; tried again only when it's next unlocked
        if alert:
            self._say_open()
        if lock:
            self._lock_now()
        if check:
            self._tell(
                f"I tried to lock the {self.door} and it's still {self.lock_state or 'not locked'}: is something "
                "in the way of the bolt? I won't try again until it's next unlocked."
            )

    def _open_too_long(self, now: float) -> bool:
        if not self.settings.open_alert_min or self.alerted or self.open_since is None:
            return False
        if now - self.open_since < self.settings.open_alert_min * 60:
            return False
        self.alerted = True
        return True

    def _lock_due(self, now: float) -> bool:
        if self.unlocked_at is None or self.tried_at is not None:
            return False
        s = self.settings
        if self.door_state not in ("on", "off"):
            if not self.sensor_down_told:
                self.sensor_down_told = True
                self._tell(
                    f"The {self.door}'s sensor isn't answering ({self.door_state or 'nothing heard'}), so I can't tell "
                    f"if the door is shut: I lock it {s.no_sensor_s:.0f} s after it's unlocked until it's back. Its "
                    "battery, or Home Assistant's Bluetooth?"
                )
            due = self.unlocked_at + s.no_sensor_s
        elif self.door_state == "on":
            return False
        elif self.opened:
            due = (now if self.shut_at is None else self.shut_at) + s.after_shut_s
        else:
            due = self.unlocked_at + s.unopened_s
        if now < due:
            return False
        self.tried_at = now
        return True

    def _lock_now(self) -> None:
        try:
            door = self._read(self.sensor)
        except HomeError as e:
            door = f"not read ({e})"
        if door == "on":  # it opened as the time came: the next shutting counts again
            print(f"(auto-lock: not locking the {self.door}: it's open)")
            self.changed(self.sensor, "on")
            with self._mutex:
                self.tried_at = None
            return
        print(f"(auto-lock: locking the {self.door}; its sensor says {door})")
        try:
            self._lock()
        except HomeError as e:
            print(f"(auto-lock: couldn't ask for the {self.door} to lock: {e})")

    def _say_open(self) -> None:
        minutes = self.settings.open_alert_min
        text = f"The {self.door} has been open for {minutes:g} minute{'s' if minutes != 1 else ''}."
        print(f"(auto-lock: {text})")
        if self._anyone_home() is False:
            self._post(f"{text} Nobody's home, as far as I can tell.")
        else:
            self._say(text)

    def start(self, follow: Callable[[list[str], Callable[[str, str], None]], None]) -> AutoLock:
        """Follows the lock and its sensor (`follow(entities, on_state)`, on a thread of its own) and steps every
        TICK_S on another."""

        def read(entity: str) -> str:
            try:
                return self._read(entity)
            except HomeError:
                return ""

        self.start_from(read(self.lock_entity), read(self.sensor))

        def ticking() -> None:
            while True:
                time.sleep(TICK_S)
                try:
                    self.step()
                except Exception as e:  # noqa: BLE001 - the next tick tries again
                    print(f"(auto-lock: a step failed: {e!r})")

        entities = [self.lock_entity, self.sensor]
        threading.Thread(target=follow, args=(entities, self.changed), daemon=True, name="auto-lock-follow").start()
        threading.Thread(target=ticking, daemon=True, name="auto-lock").start()
        s = self.settings
        print(
            f"(auto-lock: the {self.door}: {s.after_shut_s:.0f} s after it shuts, {s.unopened_s:.0f} s if it's never "
            "opened, never while it's open)"
        )
        return self
