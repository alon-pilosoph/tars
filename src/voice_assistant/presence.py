"""Who's home: whose phone the home network's routers see now (routers.py), as [presence] says whose phone is whose
(docs/presence.md).

The assistant asks the routers every [presence] every_s seconds and keeps the answer in the event database (the
presence table), where the Telegram service and the web UI read it: only one process ever signs into a router. It's
used three ways:

- "Who's home?": a status-only device in the home tool, by voice and in a chat.
- A reminder due aloud for someone who's out goes to their Telegram chat instead (ChatRouting).
- A message waiting until someone's back is said when their phone comes home, not only when TARS next hears them.

The rules lean towards "still home", never towards a false "gone":

- Seen by any router: home, at once. A phone joins the Wi-Fi as someone reaches the door.
- Unseen for [presence] away_after_min, while every router answered the whole time: out. A phone asleep on a table
  drops off the Wi-Fi now and then for a few minutes, and a router that doesn't answer says nothing about anyone.
- Before the routers have answered (TARS just started), nobody is known: "not known yet", never "out".
"""

import json
import threading
import time
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import datetime

from .config import PresenceConfig
from .events import person_key
from .home import Device
from .reminders import MESSAGE, REMINDER, Reminders, name, twelve_hour
from .routers import Router, RouterError, Seen
from .store import Store

DEVICE = "who's home"  # its name in the home tool
ROUTERS_KEY = "presence_routers"  # the meta row with how each router last answered
# A phone joins the Wi-Fi a little before its person is through the door: a message waiting for them is said this
# long after their phone comes home, unless they speak to TARS first.
ARRIVAL_GRACE_S = 60.0
# The table says who's home only while the assistant keeps writing it: older than this, it's not known now.
STALE_AFTER_S = 5 * 60.0


class People:
    """Whose phone is whose: each person's hardware addresses and the names routers list their phone under."""

    def __init__(self, people: dict[str, list[str]]):
        self.names = {person_key(n): n.strip() for n in people}
        self._ids = {person_key(n): {i.strip().lower() for i in ids} for n, ids in people.items()}

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
    home: bool | None = None  # None: not known yet
    since: float | None = None  # when they came home or went out; None: before TARS was watching
    last_seen: float | None = None


class Tracker:
    """The rules, over each round's answer: who's home, and who just came or went."""

    def __init__(self, people: People, away_after_s: float, started: float):
        self.people = people
        self.away_after_s = away_after_s
        self.states = {p: State() for p in people.names}
        self._watching_since = started  # every router has answered, every round, since then

    def update(self, seen: Collection[Seen], complete: bool, now: float) -> list[tuple[str, bool]]:
        """One round: `seen`, the devices connected, from the routers that answered; `complete`, whether they all
        did. Returns who came home (person, True) or went out (person, False), as person_key."""
        if not complete:
            self._watching_since = now  # someone's phone may be on the router that didn't answer
        changes = []
        here = self.people.whose(seen)
        for person, s in self.states.items():
            if person in here:
                s.last_seen = now
                if s.home is not True:
                    if s.home is False:
                        changes.append((person, True))
                    s.home, s.since = True, (now if s.home is False else None)
                continue
            unseen_since = max(s.last_seen or 0.0, self._watching_since)
            if s.home is not False and now - unseen_since >= self.away_after_s:
                if s.home:
                    changes.append((person, False))
                s.home, s.since = False, (s.last_seen if s.home is not None else None)
        return changes


class Board:
    """The presence table and how each router last answered, in the event database: the assistant writes, every
    process reads."""

    def __init__(self, store: Store, people: People | None = None, clock: Callable[[], float] = time.time):
        self.store, self.people, self._clock = store, people, clock

    def write(self, states: dict[str, State], routers: dict[str, dict], now: float) -> None:
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
                (ROUTERS_KEY, json.dumps(routers)),
            )

    def states(self) -> dict[str, State]:
        """Everyone's, as last written; not known for anyone the assistant hasn't written about lately."""
        now = self._clock()
        rows = self.store.rows("SELECT * FROM presence")
        found = {
            r["person"]: State(None if r["home"] is None else bool(r["home"]), r["since"], r["last_seen"])
            if now - r["updated"] < STALE_AFTER_S
            else State()
            for r in rows
        }
        names = self.people.names if self.people else {r["person"]: r["person"] for r in rows}
        return {p: found.get(p, State()) for p in names}

    def routers(self) -> dict[str, dict]:
        row = self.store.row("SELECT value FROM meta WHERE key=?", (ROUTERS_KEY,))
        return json.loads(row["value"]) if row else {}

    def is_out(self, who: str | None) -> bool:
        """Whether `who` is known to be out now. Not known is not out."""
        return self.states().get(person_key(who) or "", State()).home is False

    def summary(self) -> str:
        """Everyone, as the home tool says it."""
        names = self.people.names if self.people else {}
        lines = []
        for person, s in self.states().items():
            who = names.get(person) or name(person)
            if s.home is None:
                lines.append(f"{who}: not known yet")
            elif s.home:
                lines.append(f"{who}: home{_since(s.since, 'since')}")
            else:
                lines.append(f"{who}: out{_since(s.since, 'since about')}")
        return "; ".join(lines) or "nobody is set up to be followed"


def _since(at: float | None, words: str) -> str:
    if at is None:
        return ""
    t, today = datetime.fromtimestamp(at).astimezone(), datetime.now().astimezone().date()
    day = "" if t.date() == today else (" yesterday" if (today - t.date()).days == 1 else f" {t:%A}")
    return f" ({words} {twelve_hour(t)}{day})"


class Watcher:
    """Asks the routers every `every_s`, on a thread of its own, keeps the rules' answer on the board, and tells
    `on_change(person, home)` (person as [presence] people names them) who came home or went out."""

    def __init__(
        self,
        routers: list[Router],
        tracker: Tracker,
        board: Board,
        every_s: float,
        on_change: Callable[[str, bool], None] | None = None,
        clock: Callable[[], float] = time.time,
        left_out: int = 0,
    ):
        """`left_out`: routers in [presence] that couldn't be set up (no password): like one that never answers,
        while there are any nobody is marked out, since their phone may be on it."""
        self.routers, self.tracker, self.board = routers, tracker, board
        self.every_s, self.on_change, self._clock = every_s, on_change, clock
        self.left_out = left_out
        self.answers: dict[str, dict] = {r.name: {"kind": r.kind, "address": r.address} for r in routers}

    def start(self) -> None:
        def loop():
            while True:
                self.round()
                time.sleep(self.every_s)

        threading.Thread(target=loop, daemon=True, name="presence").start()

    def round(self) -> list[tuple[str, bool]]:
        """Asks every router once. Never raises: a router that fails is noted, and the rest of TARS carries on."""
        now = self._clock()
        seen: list[Seen] = []
        complete = not self.left_out
        for router in self.routers:
            answer = self.answers[router.name]
            try:
                found = router.connected()
            except RouterError as e:
                complete = False
                if answer.get("error") != str(e):
                    print(f"(presence: the router at {router.address} can't say who's connected: {e})")
                answer.update(error=str(e), error_at=now)
                continue
            if answer.pop("error", None):
                print(f"(presence: the router at {router.address} answers again)")
            answer.update(ok_at=now, connected=len(found))
            seen += found
        try:
            changes = self.tracker.update(seen, complete, now)
        except Exception as e:  # noqa: BLE001 - a bug here must not stop the watching thread
            print(f"(presence: couldn't work out who's home: {e!r})")
            return []
        names = self.tracker.people.names
        for person, home in changes:
            print(f"(presence: {names[person]} {'came home' if home else 'went out'})")
        try:
            self.board.write(self.tracker.states, self.answers, now)
        except Exception as e:  # noqa: BLE001 - a locked or full database: written next round
            print(f"(presence: couldn't write who's home: {e!r})")
        for person, home in changes:
            if self.on_change:
                try:
                    self.on_change(names[person], home)
                except Exception as e:  # noqa: BLE001
                    print(
                        f"(presence: couldn't act on {names[person]} {'coming home' if home else 'going out'}: {e!r})"
                    )
        return changes


class WhoIsHome(Device):
    """The home tool's "who's home": status only, from the board."""

    def __init__(self, board: Board, away_after_min: float):
        super().__init__(DEVICE)
        self.board, self.away_after_min = board, away_after_min

    def what(self) -> str:
        return "who's home now, from whose phone is on the home Wi-Fi: status only"

    def status(self) -> str:
        return f"{DEVICE}: {self.board.summary()}"

    def rules(self) -> str:
        return (
            f"{DEVICE} goes by whose phone is on the home Wi-Fi: someone home without their phone shows as out, and "
            f"someone who just left shows as home for up to {self.away_after_min:.0f} minutes. When it matters, say "
            "it's going by their phone."
        )


class ChatRouting:
    """A reminder or message due aloud for someone who's out, and on Telegram, goes to their chat instead."""

    def __init__(self, board: Board, reminders: Reminders, chats: Collection[str]):
        self.board, self.reminders = board, reminders
        self.chats = {person_key(c) for c in chats}

    def __call__(self, r: dict) -> bool:
        """True if `r` was moved to its person's chat, and isn't to be said aloud."""
        who = r["for_name"]
        if r["kind"] not in (REMINDER, MESSAGE) or r["due"] is None or person_key(who) not in self.chats:
            return False
        try:
            if not (self.board.is_out(who) and self.reminders.to_chat(r["id"])):
                return False
        except Exception as e:  # noqa: BLE001 - said aloud, as it would have been
            print(f"(presence: couldn't send reminder {r['id']} to {who}'s chat: {e!r})")
            return False
        print(f"(presence: {name(who)} is out: reminder {r['id']} goes to their Telegram chat)")
        return True


def make_watcher(
    cfg: PresenceConfig,
    store: Store,
    secret: Callable[[str], str | None],
    on_change: Callable[[str, bool], None] | None = None,
) -> tuple[Watcher | None, list[str]]:
    """The watcher [presence] sets up, and a line for each router left out (no password in .env); None with no
    router left. With any left out, nobody is marked out: their phone may be on it."""
    from .routers import make

    routers, missing = [], []
    for spec in cfg.routers:
        password = secret(spec["secret"])
        if not password:
            missing.append(f"the router at {spec['address']}: no {spec['secret']} in .env")
            continue
        routers.append(make(spec, password))
    if not routers:
        return None, missing
    people = People(cfg.people)
    watcher = Watcher(
        routers,
        Tracker(people, cfg.away_after_min * 60, time.time()),
        Board(store, people),
        cfg.every_s,
        on_change,
        left_out=len(missing),
    )
    return watcher, missing


def arrivals(reminders: Reminders, grace_s: float = ARRIVAL_GRACE_S) -> Callable[[str, bool], None]:
    """What the watcher does when someone comes home: their messages waiting until they're back are said soon."""

    def on_change(person: str, home: bool) -> None:
        if home and (ids := reminders.arrived(person, time.time() + grace_s)):
            print(f"(presence: {person}'s waiting messages {ids} are said in {grace_s:.0f} s)")

    return on_change
