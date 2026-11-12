"""Who's home: whose phone the home network's routers see now (routers.py), as [presence] says whose phone is whose
(docs/presence.md).

The assistant asks the routers every [presence] every_s seconds and keeps the answer in the event database (the
presence table), where the Telegram service and the web UI read it: only one process ever signs into a router. What
it's used for lives with what uses it: "who's home" in the house (WhoIsHome, a device in the home tool), and reminders
that follow people to their chat or wait for them to come home (reminders.ChatWhenOut, reminders.on_arrival).

The rules lean towards "still home", never towards a false "gone":

- Seen by any router: home, at once. A phone joins the Wi-Fi as someone reaches the door.
- Unseen for [presence] away_after_min, while every router answered the whole time: out. A phone asleep on a table
  drops off the Wi-Fi now and then for a few minutes, and a router that doesn't answer says nothing about anyone.
- When TARS starts, what was last known is where it starts from, if it's fresh; otherwise nobody is known, and
  "not known" is never "out".
- A door with a sensor opened from inside (doors.py), and a phone that was there as it opened dropping off just
  after: out in a minute and a half (Tracker._left_by_door). A phone already asleep, or seen well after the door, or
  someone the lock has just let in, waits away_after_min as ever.

The rules take who was detected, not how: a phone on the Wi-Fi, or someone opening the front door from outside
(doors.py), who is home at once, before their phone has found the Wi-Fi.
"""

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from .config import PresenceConfig
from .events import person_key
from .home import Device, HomeError
from .reminders import twelve_hour
from .routers import Router, RouterError, Seen, make
from .store import Store

DEVICE = "who's home"  # its name in the home tool
ROUTERS_KEY = "presence_routers"  # the meta row with how each router last answered
# The table says who's home only while the assistant keeps writing it: older than this, it's not known now. Longer
# than [presence] every_s can be, plus a round of routers that don't answer.
STALE_AFTER_S = 5 * 60.0
# A door sensor moving (doors.py): the routers are asked every HURRY_EVERY_S for HURRY_S, so whoever's phone comes or
# goes with it is seen within seconds, not at the next round.
HURRY_S, HURRY_EVERY_S = 3 * 60.0, 10.0
# Leaving by the door (Tracker._left_by_door): a phone last seen between a round before the door was opened from
# inside and LEFT_GONE_BY_S after, then by no router for LEFT_AFTER_S from whichever was later, all within
# LEFT_WINDOW_S of the door, has left by it: out now, not away_after_min later. A phone seen later than LEFT_GONE_BY_S
# after it is still in the house after the door; anyone the lock let in within LEFT_LET_IN_S before it (or since)
# has just come in, not gone out.
LEFT_GONE_BY_S, LEFT_AFTER_S, LEFT_WINDOW_S, LEFT_LET_IN_S = 60.0, 90.0, 5 * 60.0, 2 * 60.0
DOORS_KEPT = 16  # door uses from inside kept for the rule, the newest: a sensor saying "open" over and over is bounded
_MAC = re.compile(r"[0-9a-f]{2}([-:][0-9a-f]{2}){5}")


def identifier(i: str) -> str:
    """How a phone is known, as compared: lower case, and a hardware address written with dashes as with colons."""
    i = i.strip().lower()
    return i.replace("-", ":") if _MAC.fullmatch(i) else i


class People:
    """Whose phone is whose: each person's hardware addresses and the names routers list their phone under; and
    whose key a door lock knows (doors.py)."""

    def __init__(self, people: dict[str, list[str]]):
        self.names = {person_key(n): n.strip() for n in people}
        self._ids = {person_key(n): {identifier(i) for i in ids} for n, ids in people.items()}

    def whose_key(self, key: str) -> str | None:
        """The person (as person_key) a door's key is, as [presence] people names it."""
        key = identifier(key)
        return next((person for person, ids in self._ids.items() if key in ids), None)

    def whose(self, seen: Iterable[Seen]) -> set[str]:
        """The people (as person_key) one of whose phones is among `seen`."""
        found = set()
        for s in seen:
            for person, ids in self._ids.items():
                if s.mac in ids or (s.name and s.name.lower() in ids):
                    found.add(person)
        return found


@dataclass
class State:
    home: bool | None = None  # None: not known
    since: float | None = None  # when they came home or went out; None: before TARS was watching
    last_seen: float | None = None
    via: str | None = None  # home: the door they were let in by, as doors.py heard it; None: by their phone


class Tracker:
    """The rules, over who was detected each round. Durations go by a monotonic clock, since a Pi has no clock of
    its own while it's off and its wall clock jumps when it sets itself; `since` and `last_seen`, which people read,
    are wall-clock times."""

    def __init__(
        self,
        people: Iterable[str],
        away_after_s: float,
        started: float,
        last: dict[str, State] | None = None,
        round_s: float = 30.0,
    ):
        """`started`: the monotonic time watching began. `last`: what was known before (Board.states), to start from.
        `round_s`: how far apart the rounds are ([presence] every_s), so a phone seen in the round before a door was
        opened was about then."""
        self.away_after_s, self.round_s = away_after_s, round_s
        self.states = {p: replace(last[p]) if last and p in last else State() for p in people}
        self._phone_at: dict[str, float] = {}  # monotonic: when a router last saw their phone
        self._let_in_at: dict[str, float] = {}  # monotonic: when a door's lock last let them in from outside
        self._watching_since = started  # every router has answered, every round, since then
        self._doors_used: list[float] = []  # monotonic: a door opened from inside, lately (door_used)

    def update(self, here: set[str], complete: bool, now: float, wall: float) -> list[tuple[str, bool]]:
        """One round: `here`, the people detected; `complete`, whether every source answered. `now` is monotonic,
        `wall` the time of day. Returns who came home (person, True) or went out (person, False)."""
        if not complete:
            self._watching_since = now  # someone's phone may be on the router that didn't answer
        changes = []
        for person, s in self.states.items():
            if person in here:
                self._phone_at[person] = now
                if s.home is False:
                    changes.append((person, True))
                    s.since, s.via = wall, None
                s.home, s.last_seen = True, wall
                continue
            detected = max(self._phone_at.get(person, 0.0), self._let_in_at.get(person, 0.0))
            unseen_since = max(detected, self._watching_since)
            if s.home is not False and (now - unseen_since >= self.away_after_s or self._left_by_door(person, now)):
                if s.home:
                    changes.append((person, False))
                s.home, s.since = False, s.last_seen
        return changes

    def door_used(self, at: float) -> None:
        """A door was opened from inside at `at` (monotonic), not by someone let in from outside (doors.py):
        whoever's phone drops off just after has left by it."""
        self._doors_used = [d for d in self._doors_used if at - d <= LEFT_WINDOW_S][-(DOORS_KEPT - 1) :] + [at]

    def _left_by_door(self, person: str, now: float) -> bool:
        """Whether `person` left by a door used lately: a router last saw their phone between a round before it was
        opened and LEFT_GONE_BY_S after, and every router has answered without it for LEFT_AFTER_S since then or the
        door, whichever was later, within LEFT_WINDOW_S of the door. Never someone known only by a lock letting them
        in, nor anyone it let in LEFT_LET_IN_S before the door or since: they've just come in."""
        phone, let_in = self._phone_at.get(person), self._let_in_at.get(person)
        if phone is None:
            return False
        for door in self._doors_used:
            if now - door > LEFT_WINDOW_S or not door - self.round_s <= phone <= door + LEFT_GONE_BY_S:
                continue
            if let_in is not None and let_in >= door - LEFT_LET_IN_S:
                continue
            if now - max(phone, door, self._watching_since) >= LEFT_AFTER_S:
                return True
        return False

    def arrived(self, person: str, door: str, now: float, wall: float) -> bool:
        """`person` opened `door` from outside: home now, and counted as seen (for away_after_min, not as their
        phone: _left_by_door). True if they'd been out."""
        s = self.states[person]
        self._let_in_at[person] = now
        came = s.home is False
        if came:
            s.since, s.via = wall, door
        s.home, s.last_seen = True, wall
        return came


@dataclass
class RouterStatus:
    """How a router last answered the assistant, as the Status page shows it."""

    address: str
    kind: str
    ok_at: float | None = None
    connected: int = 0
    error: str = ""
    error_at: float | None = None


class Board:
    """The presence table and how each router last answered, in the event database: the assistant writes, every
    process reads."""

    def __init__(self, store: Store, people: People, clock: Callable[[], float] = time.time):
        self.store, self.people, self._clock = store, people, clock

    def write(self, states: dict[str, State], routers: Iterable[RouterStatus], now: float) -> None:
        with self.store.transaction() as db:
            for person, s in states.items():
                db.execute(
                    "INSERT INTO presence (person, home, since, last_seen, via, updated) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(person) DO UPDATE SET home=excluded.home, since=excluded.since, "
                    "last_seen=excluded.last_seen, via=excluded.via, updated=excluded.updated",
                    (person, None if s.home is None else int(s.home), s.since, s.last_seen, s.via, now),
                )
            db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (ROUTERS_KEY, json.dumps([asdict(r) for r in routers])),
            )

    def states(self) -> dict[str, State]:
        """Everyone's, as last written; not known for anyone the assistant hasn't written about lately."""
        now = self._clock()
        fresh = {
            r["person"]: State(None if r["home"] is None else bool(r["home"]), r["since"], r["last_seen"], r["via"])
            for r in self.store.rows("SELECT * FROM presence")
            if now - r["updated"] < STALE_AFTER_S
        }
        return {p: fresh.get(p, State()) for p in self.people.names}

    def routers(self) -> dict[str, RouterStatus]:
        """By address."""
        row = self.store.row("SELECT value FROM meta WHERE key=?", (ROUTERS_KEY,))
        return {r["address"]: RouterStatus(**r) for r in json.loads(row["value"])} if row else {}

    def is_home(self, who: str | None) -> bool:
        return self.states().get(person_key(who) or "", State()).home is True

    def is_out(self, who: str | None) -> bool:
        """Whether `who` is known to be out now. Not known is not out."""
        return self.states().get(person_key(who) or "", State()).home is False

    def summary(self) -> str:
        """Everyone, as the home tool says it."""
        now, lines = self._clock(), []
        for person, s in self.states().items():
            who = self.people.names[person]
            if s.home is None:
                lines.append(f"{who}: not known yet")
            elif s.home:
                by = f", let in by the {s.via}" if s.via else ""
                lines.append(f"{who}: home{_since(s.since, now, 'since', by)}")
            else:
                lines.append(f"{who}: out{_since(s.since, now, 'since about')}")
        return "; ".join(lines) or "nobody is set up to be followed"


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _since(at: float | None, now: float, words: str, more: str = "") -> str:
    if at is None:
        return f" ({more[2:]})" if more else ""
    t, today = datetime.fromtimestamp(at).astimezone(), datetime.fromtimestamp(now).astimezone().date()
    days = (today - t.date()).days
    day = "" if days == 0 else " yesterday" if days == 1 else f" {t:%A}"
    return f" ({words} {twelve_hour(t)}{day}{more})"


class Watcher:
    """Asks the routers every `every_s`, on a thread of its own, keeps the rules' answer on the board, and tells
    `on_change(person, home)` (person as [presence] people names them) who came home or went out."""

    def __init__(
        self,
        routers: list[Router],
        people: People,
        tracker: Tracker,
        board: Board,
        every_s: float,
        on_change: Callable[[str, bool], None] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        left_out: int = 0,
    ):
        """`left_out`: routers in [presence] that couldn't be set up (no password): like one that never answers,
        while there are any nobody is marked out, since their phone may be on it."""
        self.routers, self.people, self.tracker, self.board = routers, people, tracker, board
        self.every_s, self.on_change, self.left_out = every_s, on_change, left_out
        self._clock, self._monotonic = clock, monotonic
        self.statuses = {r.address: RouterStatus(r.address, r.kind) for r in routers}
        self._lock = threading.Lock()  # the tracker and the board: rounds here, doors on the MQTT thread
        self._wake = threading.Event()  # set by hurry(): a round now
        self._hurry_until = 0.0  # monotonic: rounds every HURRY_EVERY_S until then

    def start(self) -> None:
        def loop():
            while True:
                try:
                    self.round()
                except Exception as e:  # noqa: BLE001 - nothing stops the watching thread; the next round tries again
                    print(f"(presence: a round failed: {e!r})")
                hurried = self._monotonic() < self._hurry_until
                self._wake.wait(min(self.every_s, HURRY_EVERY_S) if hurried else self.every_s)
                self._wake.clear()

        threading.Thread(target=loop, daemon=True, name="presence").start()

    def hurry(self, inside: bool = False) -> None:
        """A door moved (doors.py): a round now, and every HURRY_EVERY_S for HURRY_S. `inside`: it was opened from
        inside, so a phone that drops off now has left by it (Tracker.door_used)."""
        now = self._monotonic()
        with self._lock:
            if inside:
                self.tracker.door_used(now)
            self._hurry_until = now + HURRY_S
        self._wake.set()

    def round(self) -> list[tuple[str, bool]]:
        """Asks every router once, and says who came home or went out. A router that fails is noted, never raised."""
        wall, now = self._clock(), self._monotonic()
        seen: list[Seen] = []
        complete = not self.left_out
        for router in self.routers:
            try:
                found = router.connected()
            except Exception as e:  # noqa: BLE001 - RouterError, or a bug in one router's code: it's not answering
                complete = False
                self._failed(router, e if isinstance(e, RouterError) else f"{type(e).__name__}: {e}", wall)
                continue
            status = self.statuses[router.address]
            if status.error:
                print(f"(presence: the router at {router.address} answers again)")
            self.statuses[router.address] = replace(status, ok_at=wall, connected=len(found), error="", error_at=None)
            seen += found
        with self._lock:
            changes = self.tracker.update(self.people.whose(seen), complete, now, wall)
            for person, home in changes:
                print(f"(presence: {self.people.names[person]} {'came home' if home else 'went out'})")
            self._write(wall)
        self._tell(changes)
        return changes

    def at_door(self, person: str, door: str) -> bool:
        """`person` (as person_key) just opened `door` from outside (doors.py): home now. True if they'd been out."""
        wall, now = self._clock(), self._monotonic()
        with self._lock:
            came = self.tracker.arrived(person, door, now, wall)
            if came:
                print(f"(presence: {self.people.names[person]} came home, by the {door})")
                self._write(wall)
        self._tell([(person, True)] if came else [])
        return came

    def _write(self, wall: float) -> None:
        try:
            self.board.write(self.tracker.states, self.statuses.values(), wall)
        except Exception as e:  # noqa: BLE001 - a locked or full database: written next round
            print(f"(presence: couldn't write who's home: {e!r})")

    def _tell(self, changes: list[tuple[str, bool]]) -> None:
        for person, home in changes:
            if self.on_change:
                try:
                    self.on_change(self.people.names[person], home)
                except Exception as e:  # noqa: BLE001 - what's done about one change never stops the watching
                    print(f"(presence: couldn't act on {self.people.names[person]} coming or going: {e!r})")

    def _failed(self, router: Router, e: Exception | str, wall: float) -> None:
        status = self.statuses[router.address]
        if status.error != str(e):
            print(f"(presence: the router at {router.address} can't say who's connected: {e})")
        self.statuses[router.address] = replace(status, error=str(e), error_at=wall)


class WhoIsHome(Device):
    """The home tool's "who's home": status only, from the board."""

    def __init__(self, board: Board, away_after_min: float):
        super().__init__(DEVICE)
        self.board, self.away_after_min = board, away_after_min

    def what(self) -> str:
        return "who's home now, from whose phone is on the home Wi-Fi and who let themselves in: status only"

    def status(self) -> str:
        try:
            return f"{DEVICE}: {self.board.summary()}"
        except sqlite3.Error as e:
            raise HomeError(f"who's home can't be read just now ({e})") from None

    def glance(self) -> str:
        """ "Alon and Stacey", "Alon · Stacey out": by their phones, without the times."""
        try:
            states = self.board.states()
        except sqlite3.Error as e:
            raise HomeError(f"it can't be read just now ({e})") from None
        names = self.board.people.names
        home = [names[p] for p, s in states.items() if s.home]
        out = [names[p] for p, s in states.items() if s.home is False]
        unknown = [names[p] for p, s in states.items() if s.home is None]
        said = [_and(home) if home else "nobody", *([f"{_and(out)} out"] if out else [])]
        said += [f"{_and(unknown)} not known yet"] if unknown else []
        return " · ".join(said) if states else "nobody is followed"

    def rules(self) -> str:
        return (
            f"{DEVICE} goes by whose phone is on the home Wi-Fi, and whoever opens a door's lock from outside is "
            "home at once: someone home without their phone shows as out, and "
            f"someone who just left shows as home for up to {self.away_after_min:.0f} minutes. When it matters, say "
            "it's going by their phone."
        )


@dataclass
class Presence:
    """[presence], set up in one process: whose phone is whose, the board, and the routers there are passwords for
    (`left_out` says why any aren't). Only the assistant watches; every process reads the board."""

    people: People
    board: Board
    routers: list[Router]
    left_out: list[str]
    away_after_min: float
    every_s: float

    @property
    def phones(self) -> list[str]:
        """Who can be waited for by their phone: everyone in [presence] people, while there's a router to ask."""
        return list(self.people.names.values()) if self.routers else []

    def watch(self, on_change: Callable[[str, bool], None] | None = None) -> Watcher | None:
        """Starts asking the routers, from what was last known; None if there's no router to ask."""
        if not self.routers:
            return None
        last = self.board.states()
        tracker = Tracker(self.people.names, self.away_after_min * 60, time.monotonic(), last, round_s=self.every_s)
        watcher = Watcher(
            self.routers, self.people, tracker, self.board, self.every_s, on_change, left_out=len(self.left_out)
        )
        watcher.start()
        return watcher


def make_presence(cfg: PresenceConfig, store: Store, secret: Callable[[str], str | None]) -> Presence:
    """[presence]'s setup, with each router's password from `secret` (.env); a router without one is left out."""
    people = People(cfg.people)
    routers, left_out = [], []
    for spec in cfg.routers:
        password = secret(spec["secret"])
        if password:
            routers.append(make(spec, password))
        else:
            left_out.append(f"the router at {spec['address']}: no {spec['secret']} in .env")
    return Presence(people, Board(store, people), routers, left_out, cfg.away_after_min, cfg.every_s)
