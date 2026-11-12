"""Auto-lock that knows whether the door is shut (docs/home.md#auto-lock): a lock in [home] with a door sensor and
auto_lock is locked by TARS, never while the door is open.

It sends lock.lock only when it knows the door is shut right now: its sensor's last word was "off", and read again in
Home Assistant just before, it still is. When it can't know, it doesn't lock: it tells the maintainers, once, and the
lock's own auto-lock is the backstop. It only ever locks.

The rules, for each unlocking: the lock going from locked to unlocked or open, or found so as TARS starts. Only a
settled state counts (locked, unlocked, open): the lock out of reach and back (unavailable, a Home Assistant restart),
jamming, or unlatching (unlocked, open, unlocked) isn't a new unlocking, so it isn't tried again on its account.

1. The door opened and shut again: locked `after_shut_s` (10 s) after it shut, if it's still shut. Opened again
   meanwhile, the count starts over when it shuts.
2. Unlocked and never opened (an Auto Unlock as someone walks past, the app): locked `unopened_s` (60 s) after.
   Someone asking TARS to unlock it (the house's tools, either process: Asked) starts that count again, the lock
   already unlocked included, as Home Assistant then has nothing new to say.
3. Only when the door reads shut, just before: anything but "off" (open, unknown, unavailable, or Home Assistant not
   answering) and it isn't locked.
4. A try that never reached the lock (the door couldn't be read, or Home Assistant didn't take the call) is tried
   again, MAX_TRIES in all with RETRY_S between, reading the door again each time; then the maintainers are told
   what failed.
5. One that did, once each unlocking: if the lock doesn't say locked within LOCK_SETTLE_S of the call returning
   (jammed, the door not quite shut), the maintainers are told, and nothing is tried again until it's next unlocked.
6. The door left open for `open_alert_min` (2 minutes): said aloud at home, or posted to everyone's Telegram chat if
   nobody is home; once each time it's opened. Opened while auto-lock is off, it's said once it's back on.
7. The sensor not answering (unknown or unavailable in Home Assistant, or never heard): nothing is locked until it's
   back, whatever it said last. The maintainers are told once it's been out SENSOR_GRACE_S, and once it's back.
8. Turned off (the home tool's "<door> auto-lock", off for some minutes): nothing is locked, nor said about the door
   being open, until it's back on; it comes back by itself. Kept in a file beside the event log, so a chat's process
   can turn it off and the assistant, which locks, reads it.

The call to lock is made holding the house's own lock (HomeTools.changing), as the home tool's changes are, so it's
never made in the middle of an unlock in this process; once it has it, whether it's still due is asked again.

What it knows comes from following both in Home Assistant, and from reading both again (REST) each time that's
taken up, the first and after every reconnect, and every RESYNC_S besides: a change missed while nothing was
followed is caught, as if it had just happened.

TARS down means nothing locks the door: the lock's own auto-lock, with a long timer, is the backstop (docs). Nor can
any code here close the last window, a door opened between the read and the bolt moving (docs/home.md#auto-lock).
"""

import contextlib
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
ASKED_FILE = "auto_lock_asked.json"  # beside it: { lock entity: when TARS was last asked to unlock it, ... }
UNLOCKED = ("unlocked", "open")  # Home Assistant's lock states for not locked; "open" is unlatched
SETTLED = ("locked", *UNLOCKED)  # where a lock stays: not locking, unlocking, opening, jammed, nor out of reach
SHUT, OPEN = "off", "on"  # a door sensor's states in Home Assistant; anything else isn't known
LOCK_SETTLE_S = 20.0  # a Nuki turns its bolt in a few seconds, then reports it; from the call returning
MAX_TRIES, RETRY_S = 3, (5.0, 30.0)  # tries that never reached the lock, and the waits after the first two
SENSOR_GRACE_S = 60.0  # a sensor out this long is told: Home Assistant restarting is shorter
RESYNC_S = 30.0  # the lock and the door read again this often, beside following them: a change missed is caught
TICK_S = 1.0
DEFAULT_OFF_MIN, MOST_OFF_MIN = 60.0, 12 * 60.0


@dataclass(frozen=True)
class Settings:
    """[[home.devices]] auto_lock, for a lock: true, or a table of these."""

    after_shut_s: float = 10.0
    unopened_s: float = 60.0
    open_alert_min: float = 2.0  # 0: never said

    @classmethod
    def of(cls, value) -> Settings | None:
        if value is True:
            return cls()
        if isinstance(value, dict):
            return cls(**value)
        return None


# after_shut_s is MIN_AFTER_SHUT_S at least: a door pulled to can bounce off the frame, its contact saying shut
# for a moment as it swings open again, and the bolt thrown then would hit the frame.
MIN_AFTER_SHUT_S = 3
RANGES = {"after_shut_s": (MIN_AFTER_SHUT_S, 300), "unopened_s": (10, 600), "open_alert_min": (0, 60)}


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


class Kept:
    """A time for each lock, in a file both processes read and write (beside the event log)."""

    name = FILE

    def __init__(self, folder: Path, entity: str, clock: Callable[[], float] = time.time):
        self.path, self.entity, self._clock = folder / self.name, entity, clock

    def _when(self) -> float | None:
        found = self._read().get(self.entity)
        return found if isinstance(found, int | float) and not isinstance(found, bool) else None

    def _keep(self, when: float | None) -> None:
        found = self._read()
        if when is None:
            found.pop(self.entity, None)
        else:
            found[self.entity] = when
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_name(f".{self.name}.{os.getpid()}")
        part.write_text(json.dumps(found))
        part.replace(self.path)

    def _read(self) -> dict:
        try:
            found = json.loads(self.path.read_text())
        except OSError, ValueError:
            return {}
        return found if isinstance(found, dict) else {}


class Off(Kept):
    """Whether a lock's auto-lock is turned off, and until when."""

    def until(self) -> float | None:
        """When it comes back on, while it's off; None while it's on."""
        until = self._when()
        return until if until is not None and until > self._clock() else None

    def set(self, until: float | None) -> None:
        self._keep(until)


class Asked(Kept):
    """When someone last asked TARS to unlock a lock, the house's tools in either process: auto-lock counts its
    "never opened" time again from then. Only ever a later lock: nothing here unlocks."""

    name = ASKED_FILE

    def at(self) -> float | None:
        return self._when()

    def mark(self) -> None:
        self._keep(self._clock())


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
    lock (HomeError if it didn't take the call; False if it took it but didn't answer in time). `say(text)` says it
    aloud at home; `post(text)` posts it to everyone's chat; `anyone_home()` is whether someone is (None: not known);
    `tell(text)` tells the maintainers."""

    def __init__(
        self,
        door: str,
        lock_entity: str,
        sensor: str,
        settings: Settings,
        off: Off,
        read: Callable[[str], str],
        lock: Callable[[], bool | None],
        say: Callable[[str], None],
        post: Callable[[str], None],
        tell: Callable[[str], None],
        anyone_home: Callable[[], bool | None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
        asked: Asked | None = None,
        changing: contextlib.AbstractContextManager | None = None,
    ):
        """`asked`: when TARS was last asked to unlock it. `changing`: held while the lock is called, the house's
        own lock (HomeTools.changing)."""
        self.door, self.lock_entity, self.sensor, self.settings, self.off = door, lock_entity, sensor, settings, off
        self._read, self._lock, self._say, self._post, self._tell = read, lock, say, post, tell
        self._anyone_home, self._clock = anyone_home, clock
        self._asked, self._changing = asked, changing or contextlib.nullcontext()
        self._asked_seen = asked.at() if asked else None  # one from before TARS started isn't news
        self._mutex = threading.Lock()
        self.lock_state = ""
        self.lock_settled = ""  # its last settled state (SETTLED); "" until one's heard
        self.door_state = ""  # the sensor's last word, as Home Assistant has it: only SHUT and OPEN are known
        self.door_known = ""  # its last known word (SHUT or OPEN), kept while it isn't answering
        self.down_since: float | None = clock()  # the sensor not answering since; nothing heard yet counts
        self.down_why = "nothing heard from it yet"
        self.down_told = False
        self.unlocked_at: float | None = None  # this unlocking; None while locked
        self.opened = False  # the door opened since it was unlocked (or may have, unseen)
        self.shut_at: float | None = None
        self.open_since: float | None = None
        self.alerted = False  # said it's open, this opening
        self.asked_at: float | None = None  # the call to lock returned, and it hasn't said locked yet
        self.answered = True  # Home Assistant answered that call in time
        self.tries = 0  # tries that never reached the lock, this unlocking
        self.retry_at: float | None = None  # not tried again before this
        self.gave_up = False  # the maintainers were told it couldn't be locked, this unlocking
        self._heard = {lock_entity: 0, sensor: 0}  # changes taken for each, so a read never undoes a newer one
        self._synced_at: float | None = None  # both last read again
        self._resync = threading.Event()  # set when following is taken up again: read both at the next step

    def changed(self, entity: str, state: str, heard: int | None = None) -> None:
        """An entity's new state, as Home Assistant reports it (hass.follow). `heard`: from a read started when
        `_heard[entity]` was this; taken only if no newer change has come since."""
        now, told = self._clock(), None
        with self._mutex:
            if heard is not None and self._heard.get(entity) != heard:
                return
            if entity in self._heard:
                self._heard[entity] += 1
            if entity == self.lock_entity:
                self._lock_changed(state, now)
            elif entity == self.sensor:
                told = self._door_changed(state, now)
        if told:
            self._tell(told)

    def subscribed(self) -> None:
        """Following is taken up (hass.follow's on_subscribed): both are read again at the next step."""
        self._resync.set()

    def resync(self) -> None:
        """Reads the door, then the lock, in Home Assistant now, and takes what they say as changes: as TARS starts,
        unlocked counts as just unlocked; later, a change missed counts from now. A read that fails changes nothing
        (the read just before locking still decides)."""
        self._resync.clear()
        for entity in (self.sensor, self.lock_entity):
            with self._mutex:
                heard = self._heard[entity]
            try:
                state = self._read(entity)
            except HomeError as e:
                if entity == self.sensor and self.door_state == "":
                    with self._mutex:
                        self.down_why = f"I couldn't read it: {e}"
                continue
            self.changed(entity, str(state), heard)
        self._synced_at = self._clock()

    def _lock_changed(self, state: str, now: float) -> None:
        was, self.lock_state = self.lock_settled, state
        if state in SETTLED:
            self.lock_settled = state
        if state in UNLOCKED and was in ("locked", ""):
            self.unlocked_at, self.asked_at, self.tries, self.retry_at, self.gave_up = now, None, 0, None, False
            self.opened = self.door_state != SHUT  # open, or not known: it counts from when it's next seen shut
            self.shut_at = now if self.door_state == SHUT else None
            print(f"(auto-lock: the {self.door} unlocked)")
        elif state == "locked":
            if self.asked_at is not None:
                print(f"(auto-lock: the {self.door} locked)")
            self.unlocked_at = self.asked_at = None

    def _door_changed(self, state: str, now: float) -> str | None:
        """What to tell the maintainers, if anything: the sensor answering again."""
        self.door_state = state
        if state not in (SHUT, OPEN):
            if self.down_since is None:
                self.down_since, self.down_why = now, state or "nothing heard"
            return None
        was, back = self.door_known, self.down_since is not None
        self.door_known, self.down_since = state, None
        if state == OPEN and was != OPEN:
            self.opened, self.open_since, self.shut_at, self.alerted = True, now, None, False
        elif state == SHUT and (was != SHUT or back):
            # Back shut after a time out: it may have opened and shut unseen, so it counts as shut just now.
            self.open_since, self.shut_at = None, now
            self.opened = self.opened or (back and was != "")
        if back and self.down_told:
            self.down_told = False
            return f"The {self.door}'s sensor is answering again: TARS locks the door once it's shut, as before."
        return None

    def step(self) -> None:
        """What's due now: reading both again, locking, checking it locked, saying the door's open. Called every
        TICK_S."""
        if self._resync.is_set() or self._synced_at is None or self._clock() - self._synced_at >= RESYNC_S:
            self.resync()
        self._take_asked()
        now = self._clock()
        off = self.off.until() is not None
        with self._mutex:
            alert = None if off else self._open_too_long(now)  # off, it's kept for when it's back on
            down = self._down_to_tell(now)
            lock = not off and self._lock_due(now)
            check = self._not_locked(now)
        if down:
            self._tell(down)
        if alert:
            self._say_open(alert)
        if lock:
            self._lock_now()
        if check:
            self._tell(check)

    def _open_too_long(self, now: float) -> float | None:
        """For how many minutes the door's been open, once it's time to say so; None otherwise."""
        if not self.settings.open_alert_min or self.alerted or self.open_since is None or self.door_state != OPEN:
            return None
        if now - self.open_since < self.settings.open_alert_min * 60:
            return None
        self.alerted = True
        return max(self.settings.open_alert_min, (now - self.open_since) // 60)

    def _down_to_tell(self, now: float) -> str | None:
        if self.down_since is None or self.down_told or now - self.down_since < SENSOR_GRACE_S:
            return None
        self.down_told = True
        unlocked = " It's unlocked now." if self.lock_state in UNLOCKED else ""
        return (
            f"The {self.door}'s sensor isn't answering ({self.down_why}), so I can't tell whether the door is shut, "
            f"and I won't lock it until it's back.{unlocked} The lock's own auto-lock is the backstop. Its battery, "
            "or Home Assistant's Bluetooth?"
        )

    def _not_locked(self, now: float) -> str | None:
        """What to tell the maintainers once the lock was asked and hasn't locked: jammed, or not locked in time."""
        if self.asked_at is None or self.unlocked_at is None:
            return None
        if self.lock_state != "jammed" and now - self.asked_at < LOCK_SETTLE_S:
            return None
        self.asked_at, self.gave_up = None, True  # told once; tried again only when it's next unlocked
        if self.lock_state == "jammed":
            said = "the lock says it jammed"
        else:
            said = f"{LOCK_SETTLE_S:.0f} s later it still says {self.lock_state or 'nothing'}"
            said += "" if self.answered else " (Home Assistant hadn't answered the call in time)"
        opened = " The door's open now." if self.door_state == OPEN else ""
        return (
            f"I asked the {self.door} to lock and {said}: is something in the way of the bolt, or the door not quite "
            f"shut?{opened} I won't try again until it's next unlocked."
        )

    def _lock_due(self, now: float) -> bool:
        """Whether locking is due now: only ever with the door's last word shut. Changes nothing."""
        if self.unlocked_at is None or self.asked_at is not None or self.gave_up:
            return False
        if self.door_state != SHUT or (self.retry_at is not None and now < self.retry_at):
            return False
        s = self.settings
        shut_at = now if self.shut_at is None else self.shut_at
        due = shut_at + s.after_shut_s if self.opened else self.unlocked_at + s.unopened_s
        return now >= due

    def _take_asked(self) -> None:
        """Someone asked TARS to unlock it since last looked: an unlocking under way counts from now, as if it had
        just been unlocked. While it's locked, Home Assistant's "unlocked" starts the count, as ever."""
        if self._asked is None or (at := self._asked.at()) is None:
            return
        if self._asked_seen is not None and at <= self._asked_seen:
            return
        self._asked_seen, now = at, self._clock()
        with self._mutex:
            if self.unlocked_at is None:
                return
            self.unlocked_at, self.opened = now, self.door_state != SHUT
            self.shut_at = now if self.door_state == SHUT else None
            self.asked_at, self.tries, self.retry_at, self.gave_up = None, 0, None, False
        print(f"(auto-lock: someone asked for the {self.door} unlocked: counting from now)")

    def _lock_now(self) -> None:
        """Holding the house's lock, asks again whether it's due, then reads the door again and locks only if it
        reads shut; otherwise it's tried again later, or never."""
        with self._changing:
            self._take_asked()
            off = self.off.until() is not None
            with self._mutex:
                due = not off and self._lock_due(self._clock())
            if due:
                self._read_and_lock()

    def _read_and_lock(self) -> None:
        with self._mutex:
            heard = self._heard[self.sensor]
        try:
            door = self._read(self.sensor)
        except HomeError as e:
            self._failed(f"I couldn't read its sensor to be sure it's shut ({e})")
            return
        if door != SHUT:
            # It opened as the time came (the next shutting counts again), or its sensor isn't answering (nothing is
            # locked until it's back).
            print(f"(auto-lock: not locking the {self.door}: its sensor says {door or 'nothing'})")
            self.changed(self.sensor, door, heard)
            return
        print(f"(auto-lock: locking the {self.door}; its sensor says {door})")
        try:
            answered = self._lock()
        except HomeError as e:
            self._failed(f"Home Assistant didn't take the call to lock it ({e})")
            return
        with self._mutex:  # the settling is counted from here: the call itself can take SERVICE_TIMEOUT_S
            self.asked_at, self.answered = self._clock(), answered is not False

    def _failed(self, why: str) -> None:
        """A try that never reached the lock: tried again after a wait, MAX_TRIES in all, then told why."""
        print(f"(auto-lock: not locked the {self.door}: {why})")
        with self._mutex:
            self.tries += 1
            if self.tries < MAX_TRIES:
                self.retry_at = self._clock() + RETRY_S[min(self.tries, len(RETRY_S)) - 1]
                return
            self.gave_up = True
        self._tell(
            f"I didn't lock the {self.door}: {why}, {MAX_TRIES} times in a row. It stays unlocked until someone locks "
            "it, or the lock's own auto-lock does; I'll try again when it's next unlocked."
        )

    def _say_open(self, minutes: float) -> None:
        text = f"The {self.door} has been open for {minutes:g} minute{'s' if minutes != 1 else ''}."
        print(f"(auto-lock: {text})")
        if self._anyone_home() is False:
            self._post(f"{text} Nobody's home, as far as I can tell.")
        else:
            self._say(text)

    def start(self, follow: Callable[[list[str], Callable[[str, str], None], Callable[[], None]], None]) -> AutoLock:
        """Follows the lock and its sensor (`follow(entities, on_state, on_subscribed)`, on a thread of its own) and
        steps every TICK_S on another, the first step reading both."""

        def ticking() -> None:
            while True:
                time.sleep(TICK_S)
                try:
                    self.step()
                except Exception as e:  # noqa: BLE001 - the next tick tries again
                    print(f"(auto-lock: a step failed: {e!r})")

        entities = [self.lock_entity, self.sensor]
        threading.Thread(
            target=follow, args=(entities, self.changed, self.subscribed), daemon=True, name="auto-lock-follow"
        ).start()
        threading.Thread(target=ticking, daemon=True, name="auto-lock").start()
        s = self.settings
        print(
            f"(auto-lock: the {self.door}: {s.after_shut_s:.0f} s after it shuts, {s.unopened_s:.0f} s if it's never "
            "opened, never while it's open)"
        )
        return self
