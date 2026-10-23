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

The rules take who was detected, not how: a phone on the Wi-Fi now, a door or a lock later.
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
_MAC = re.compile(r"[0-9a-f]{2}([-:][0-9a-f]{2}){5}")


def identifier(i: str) -> str:
    """How a phone is known, as compared: lower case, and a hardware address written with dashes as with colons."""
    i = i.strip().lower()
    return i.replace("-", ":") if _MAC.fullmatch(i) else i


class People:
    """Whose phone is whose: each person's hardware addresses and the names routers list their phone under."""

    def __init__(self, people: dict[str, list[str]]):
        self.names = {person_key(n): n.strip() for n in people}
        self._ids = {person_key(n): {identifier(i) for i in ids} for n, ids in people.items()}

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


class Tracker:
    """The rules, over who was detected each round. Durations go by a monotonic clock, since a Pi has no clock of
    its own while it's off and its wall clock jumps when it sets itself; `since` and `last_seen`, which people read,
    are wall-clock times."""

    def __init__(
        self, people: Iterable[str], away_after_s: float, started: float, last: dict[str, State] | None = None
    ):
        """`started`: the monotonic time watching began. `last`: what was known before (Board.states), to start from."""
        self.away_after_s = away_after_s
        self.states = {p: replace(last[p]) if last and p in last else State() for p in people}
        self._detected_at: dict[str, float] = {}  # monotonic
        self._watching_since = started  # every router has answered, every round, since then

    def update(self, here: set[str], complete: bool, now: float, wall: float) -> list[tuple[str, bool]]:
        """One round: `here`, the people detected; `complete`, whether every source answered. `now` is monotonic,
        `wall` the time of day. Returns who came home (person, True) or went out (person, False)."""
        if not complete:
            self._watching_since = now  # someone's phone may be on the router that didn't answer
        changes = []
        for person, s in self.states.items():
            if person in here:
                self._detected_at[person] = now
                if s.home is False:
                    changes.append((person, True))
                    s.since = wall
                s.home, s.last_seen = True, wall
                continue
            unseen_since = max(self._detected_at.get(person, 0.0), self._watching_since)
            if s.home is not False and now - unseen_since >= self.away_after_s:
                if s.home:
                    changes.append((person, False))
                s.home, s.since = False, s.last_seen
        return changes


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
                    "INSERT INTO presence (person, home, since, last_seen, updated) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(person) DO UPDATE SET home=excluded.home, since=excluded.since, "
                    "last_seen=excluded.last_seen, updated=excluded.updated",
                    (person, None if s.home is None else int(s.home), s.since, s.last_seen, now),
                )
            db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (ROUTERS_KEY, json.dumps([asdict(r) for r in routers])),
            )

    def states(self) -> dict[str, State]:
        """Everyone's, as last written; not known for anyone the assistant hasn't written about lately."""
        now = self._clock()
        fresh = {
            r["person"]: State(None if r["home"] is None else bool(r["home"]), r["since"], r["last_seen"])
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
                lines.append(f"{who}: home{_since(s.since, now, 'since')}")
            else:
                lines.append(f"{who}: out{_since(s.since, now, 'since about')}")
        return "; ".join(lines) or "nobody is set up to be followed"


def _since(at: float | None, now: float, words: str) -> str:
    if at is None:
        return ""
    t, today = datetime.fromtimestamp(at).astimezone(), datetime.fromtimestamp(now).astimezone().date()
    days = (today - t.date()).days
    day = "" if days == 0 else " yesterday" if days == 1 else f" {t:%A}"
    return f" ({words} {twelve_hour(t)}{day})"


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

    def start(self) -> None:
        def loop():
            while True:
                try:
                    self.round()
                except Exception as e:  # noqa: BLE001 - nothing stops the watching thread; the next round tries again
                    print(f"(presence: a round failed: {e!r})")
                time.sleep(self.every_s)

        threading.Thread(target=loop, daemon=True, name="presence").start()

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
        changes = self.tracker.update(self.people.whose(seen), complete, now, wall)
        for person, home in changes:
            print(f"(presence: {self.people.names[person]} {'came home' if home else 'went out'})")
        try:
            self.board.write(self.tracker.states, self.statuses.values(), wall)
        except Exception as e:  # noqa: BLE001 - a locked or full database: written next round
            print(f"(presence: couldn't write who's home: {e!r})")
        for person, home in changes:
            if self.on_change:
                try:
                    self.on_change(self.people.names[person], home)
                except Exception as e:  # noqa: BLE001 - what's done about one change never stops the watching
                    print(f"(presence: couldn't act on {self.people.names[person]} coming or going: {e!r})")
        return changes

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
        return "who's home now, from whose phone is on the home Wi-Fi: status only"

    def status(self) -> str:
        try:
            return f"{DEVICE}: {self.board.summary()}"
        except sqlite3.Error as e:
            raise HomeError(f"who's home can't be read just now ({e})") from None

    def rules(self) -> str:
        return (
            f"{DEVICE} goes by whose phone is on the home Wi-Fi: someone home without their phone shows as out, and "
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
        tracker = Tracker(self.people.names, self.away_after_min * 60, time.monotonic(), last)
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
