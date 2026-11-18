"""Rooms: one brain, several bodies, each in a room of its own (docs/bodies.md#rooms). Every room's assistant shares
one `Rooms` desk, which decides where each thing TARS says by itself is said, and makes sure it's said once.

- A reminder, or a skill's line, for a person goes to the room where that person was last heard, within RECENT_S;
  otherwise, and for anything for nobody in particular (an announcement), to the main room.
- A room whose body is gone isn't given anything; a muted one is passed over for one that isn't, but not left out:
  a muted TARS still says its reminders, as it always has.
- A room takes an item under the desk's lock, so two rooms waking at the same moment can't both say it; the item is
  the taker's until it's `done`, or given back (`give_back`, when its body goes away before it's said). A take that's
  never finished lapses after CLAIM_S, so nothing waits forever on a room that stopped.

With one room, every item is that room's: nothing about TARS changes. "Last heard" comes only from a room's assistant
knowing who spoke (`heard`): presence knows who's home, not which room they're in.

A web page (the Talk page) is a room only while it's open (`page`, then `remove`): it's never given anything, and
someone talking there isn't "heard" in it, so what's theirs still goes where their voice was last heard.
"""

import threading
import time
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field
from typing import TypeVar

from .events import person_key

MAIN = "home"  # the room of a home with one body, and of the Pi's own speakerphone unless [bodies] says otherwise
RECENT_S = 2 * 3600.0  # how long someone's last room counts: past it, what's theirs goes to the main room
CLAIM_S = 120.0  # a take not finished by then is free again (a room that stopped mid-reminder)

T = TypeVar("T")


@dataclass
class Room:
    name: str
    connected: bool = True
    muted: bool = False
    page: bool = False  # a web page: never given anything to say
    last_heard: dict[str, float] = field(default_factory=dict)  # person: when (the desk's clock)


class Rooms:
    """The desk. `main`: the main room's name. `clock`: seconds."""

    def __init__(self, main: str = MAIN, recent_s: float = RECENT_S, clock: Callable[[], float] = time.monotonic):
        self.main, self._recent_s, self._clock = main, recent_s, clock
        self._lock = threading.Lock()
        self._rooms: dict[str, Room] = {main: Room(main)}
        self._claims: dict[Hashable, tuple[str, float]] = {}  # item: (the room that took it, when)

    # What the rooms tell it

    def add(self, name: str, connected: bool = True) -> None:
        with self._lock:
            self._rooms.setdefault(name, Room(name)).connected = connected

    def connected(self, name: str, connected: bool) -> None:
        """A room's body came or went. Gone, what it had taken is free again."""
        self.add(name, connected)
        if not connected:
            self.give_back(name)

    def page(self, name: str) -> None:
        """A web page opened as a room of its own, for as long as it's open."""
        with self._lock:
            self._rooms[name] = Room(name, page=True)

    def remove(self, name: str) -> None:
        """A web page's room, closed. The main room stays."""
        if name != self.main:
            self.give_back(name)
            with self._lock:
                self._rooms.pop(name, None)

    def muted(self, name: str, muted: bool) -> None:
        with self._lock:
            self._rooms.setdefault(name, Room(name)).muted = muted

    def heard(self, name: str, person: str | None) -> None:
        """Someone was just recognized in a room: things for them go there now."""
        if key := person_key(person):
            now = self._clock()
            with self._lock:
                if (here := self._rooms.get(name)) and here.page:
                    return
                for room in self._rooms.values():
                    room.last_heard.pop(key, None)
                self._rooms.setdefault(name, Room(name)).last_heard[key] = now

    # Where things go

    def room_for(self, person: str | None) -> str | None:
        """Where something for `person` (None: for nobody in particular) is said; None if no room can say it."""
        with self._lock:
            return self._room_for(person_key(person), self._clock())

    def _room_for(self, person: str, now: float) -> str | None:
        here = [r for r in self._rooms.values() if r.connected and not r.page]
        if not here:
            return None
        if person:
            for room in here:
                at = room.last_heard.get(person)
                if at is not None and now - at <= self._recent_s and not room.muted:
                    return room.name
        main = self._rooms.get(self.main)
        if main and main.connected and not main.muted:
            return main.name
        unmuted = [r for r in here if not r.muted]
        return (unmuted or [main if main and main.connected else here[0]])[0].name

    def waiting(
        self, room: str, items: Iterable[T], key: Callable[[T], Hashable], person: Callable[[T], str | None]
    ) -> bool:
        """Whether any of `items` is this room's to say and free to take: the wake loop's question, asked often."""
        with self._lock:
            now = self._clock()
            return any(self._mine(room, key(i), person_key(person(i)), now) for i in items)

    def take(
        self, room: str, items: Iterable[T], key: Callable[[T], Hashable], person: Callable[[T], str | None]
    ) -> list[T]:
        """Those of `items` this room says, taken: no other room gets them until they're done or given back."""
        with self._lock:
            now = self._clock()
            mine = [i for i in items if self._mine(room, key(i), person_key(person(i)), now)]
            for i in mine:
                self._claims[key(i)] = (room, now)
            return mine

    def _mine(self, room: str, key: Hashable, person: str, now: float) -> bool:
        taken = self._claims.get(key)
        if taken is not None and taken[0] != room and now - taken[1] < CLAIM_S:
            return False
        return self._room_for(person, now) == room

    def done(self, room: str, keys: Iterable[Hashable]) -> None:
        """What `room` took is said (or given up): it's nobody's now, and comes back only if it's due again."""
        with self._lock:
            for key in keys:
                if self._claims.get(key, (None,))[0] == room:
                    del self._claims[key]

    def give_back(self, room: str) -> None:
        """Everything `room` took and didn't finish is free again (its body went away)."""
        with self._lock:
            self._claims = {k: v for k, v in self._claims.items() if v[0] != room}

    @property
    def single(self) -> bool:
        with self._lock:
            return len(self._rooms) == 1
