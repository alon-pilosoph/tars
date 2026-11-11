"""Doors that say who opened them: a Nuki smart lock's own events, over MQTT (docs/presence.md#the-front-door).

A Nuki with MQTT turned on publishes every lock action to `<topic>/lockActionEvent` as "action,trigger,auth id,code
id,auto unlock": which app (the auth id) or keypad code (the code id) did what, and whether the phone's Auto Unlock
did it. Each one is kept in the door_events table. Whose key is whose comes from [presence] people ("key:<auth id>",
"code:<code id>") or is learned: a key nobody's named that opens the door while exactly one person's phone comes home,
twice and never while someone else's does, is theirs from then on, and the maintainers are told.

Someone opening the door from outside (Auto Unlock, or the keypad) is home at once, before their phone has found the
Wi-Fi: presence.Watcher.at_door. Opening it from the app alone doesn't count: that can be done from anywhere.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .presence import People, Watcher
from .store import Store

# Nuki's lock actions and triggers (its MQTT API, lockActionEvent).
UNLOCK, LOCK, UNLATCH, LOCK_N_GO, LOCK_N_GO_UNLATCH, FULL_LOCK = 1, 2, 3, 4, 5, 6
OPENS = (UNLOCK, UNLATCH)
APP = 0  # an app or a bridge, over Bluetooth, Wi-Fi or Matter; Auto Unlock is the app's
ACTIONS = {
    UNLOCK: "unlocked",
    LOCK: "locked",
    UNLATCH: "unlatched",
    LOCK_N_GO: "Lock 'n' Go",
    LOCK_N_GO_UNLATCH: "Lock 'n' Go with unlatch",
    FULL_LOCK: "full lock",
}
# A key whose door opening came this near a phone coming home is guessed to be that phone's person's.
LEARN_WINDOW_S = 10 * 60.0
LEARN_AFTER = 2  # guesses, all the same person, before a key is taken to be theirs
SETTLE_EVERY_S = 30.0


@dataclass(frozen=True)
class DoorEvent:
    door: str
    action: int
    trigger: int
    auth_id: int
    code_id: int
    auto_unlock: bool
    at: float

    @property
    def key(self) -> str:
        """The key it was done with, as [presence] people names it: a keypad code, or an app's authorization."""
        return f"code:{self.code_id}" if self.code_id else f"key:{self.auth_id}"

    @property
    def opens(self) -> bool:
        return self.action in OPENS

    @property
    def from_outside(self) -> bool:
        """Opened by someone at the door: their phone's Auto Unlock, or the keypad. An app can open it from
        anywhere, and the lock's own button and knob are inside."""
        return self.opens and ((self.trigger == APP and self.auto_unlock) or bool(self.code_id))

    def words(self) -> str:
        auto = self.trigger == APP and self.auto_unlock
        how = "by Auto Unlock" if auto else "with the keypad" if self.code_id else ""
        return " ".join(w for w in (ACTIONS.get(self.action, f"action {self.action}"), how) if w)


def parse(door: str, payload: str, at: float) -> DoorEvent | None:
    """A lockActionEvent, or None if it isn't one."""
    fields = payload.strip().split(",")
    try:
        action, trigger, auth_id, code_id, *rest = (int(f) for f in fields)
    except ValueError:
        return None
    # The fifth field is Auto Unlock (0 or 1) for an app's action; for a button or a fob, how many presses.
    return DoorEvent(door, action, trigger, auth_id, code_id, trigger == APP and rest[:1] == [1], at)


class DoorLog:
    """The door_events table, and the keys learned (door_keys)."""

    def __init__(self, store: Store, people: People):
        self.store, self.people = store, people

    def who(self, key: str) -> str | None:
        """The person (as person_key) a key is, from [presence] people, or learned."""
        if found := self.people.whose_key(key):
            return found
        row = self.store.row("SELECT person FROM door_keys WHERE key=?", (key,))
        return row["person"] if row and row["person"] in self.people.names else None

    def record(self, e: DoorEvent, person: str | None) -> int:
        return self.store.write(
            "INSERT INTO door_events (at, door, action, trigger, key, auto_unlock, person) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (e.at, e.door, e.action, e.trigger, e.key, int(e.auto_unlock), person),
        )

    def unsettled(self, before: float) -> list[dict]:
        """Openings by a key nobody's named, from before `before`, not yet compared with who came home."""
        return self.store.rows(
            "SELECT * FROM door_events WHERE person IS NULL AND settled = 0 AND at < ? AND action IN (?, ?)",
            (before, *OPENS),
        )

    def settle(self, event_id: int, guess: str | None) -> str | None:
        """Keeps the guess (or that there was none) for one opening; returns the person if its key is now learned."""
        with self.store.transaction() as db:
            db.execute("UPDATE door_events SET settled = 1, guess = ? WHERE id = ?", (guess, event_id))
            key = db.execute("SELECT key FROM door_events WHERE id = ?", (event_id,)).fetchone()["key"]
            if guess is None or db.execute("SELECT 1 FROM door_keys WHERE key = ?", (key,)).fetchone():
                return None
            guesses = [
                r["guess"]
                for r in db.execute("SELECT guess FROM door_events WHERE key = ? AND guess IS NOT NULL", (key,))
            ]
            if len(guesses) < LEARN_AFTER or set(guesses) != {guess}:
                return None
            db.execute("INSERT INTO door_keys (key, person, learned) VALUES (?, ?, ?)", (key, guess, time.time()))
            return guess

    def last(self, door: str) -> dict | None:
        return self.store.row("SELECT * FROM door_events WHERE door = ? ORDER BY at DESC LIMIT 1", (door,))


class Doors:
    """Listens to each door's lock over MQTT, keeps what it says, puts whoever opens it from outside at home
    (watcher.at_door), and learns whose key is whose from phones coming home."""

    def __init__(
        self,
        log: DoorLog,
        watcher: Watcher | None,
        tell: Callable[[str], None] = lambda text: None,
        clock: Callable[[], float] = time.time,
    ):
        self.log, self.watcher, self._tell, self._clock = log, watcher, tell, clock
        self._arrivals: list[tuple[str, float]] = []  # (person, when) their phone came home, while TARS runs
        self._lock = threading.Lock()

    def heard(self, e: DoorEvent) -> None:
        """One event from a lock."""
        person = self.log.who(e.key)
        self.log.record(e, person)
        names = self.log.people.names
        print(f"(doors: the {e.door} {e.words()}, {f'by {names[person]}' if person else f'with {e.key}'})")
        if person and e.from_outside and self.watcher:
            self.watcher.at_door(person, e.door)

    def phone_came_home(self, name: str, home: bool) -> None:
        """presence.Watcher's on_change, for learning keys: who came home by their phone, and when."""
        if home:
            with self._lock:
                now = self._clock()
                self._arrivals = [(p, t) for p, t in self._arrivals if now - t < 3 * LEARN_WINDOW_S]
                self._arrivals.append((name, now))

    def settle(self) -> None:
        """Compares each opening by an unnamed key, once it's old enough, with whose phones came home around it."""
        now = self._clock()
        with self._lock:
            arrivals = list(self._arrivals)
        for row in self.log.unsettled(now - LEARN_WINDOW_S):
            near = {p for p, t in arrivals if abs(t - row["at"]) <= LEARN_WINDOW_S}
            guess = self.log.people.key_of(near.pop()) if len(near) == 1 else None
            if learned := self.log.settle(row["id"], guess):
                name = self.log.people.names[learned]
                print(f"(doors: {row['key']} on the {row['door']} is {name}'s)")
                self._tell(
                    f"I can tell when {name} opens the {row['door']} now: the lock's record of it matched "
                    f"{name}'s phone coming home, twice."
                )

    def start(self) -> Doors:
        def loop():
            while True:
                time.sleep(SETTLE_EVERY_S)
                try:
                    self.settle()
                except Exception as e:  # noqa: BLE001 - the next round tries again
                    print(f"(doors: couldn't compare the door with who came home: {e!r})")

        threading.Thread(target=loop, daemon=True, name="doors").start()
        return self


def listen(mqtt: dict, password: str | None, doors: list[dict], on_event: Callable[[DoorEvent], None]):
    """Subscribes to each door's lockActionEvent on the broker in [presence.mqtt], reconnecting by itself. Needs the
    door extra (paho-mqtt)."""
    import paho.mqtt.client as paho

    topics = {f"{d['topic'].rstrip('/')}/lockActionEvent": d["name"] for d in doors}
    client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"tars-doors-{int(time.time())}")
    if mqtt.get("user"):
        client.username_pw_set(mqtt["user"], password)

    def connected(client, userdata, flags, reason, properties=None):
        if reason.is_failure:
            print(f"(doors: the MQTT broker at {mqtt['address']} refused TARS: {reason})")
            return
        for topic in topics:
            client.subscribe(topic, qos=1)

    def message(client, userdata, msg):
        door = topics.get(msg.topic)
        e = parse(door, msg.payload.decode(errors="replace"), time.time()) if door else None
        if e is None:
            return
        try:
            on_event(e)
        except Exception as ex:  # noqa: BLE001 - one event's trouble never stops the listening
            print(f"(doors: couldn't act on the {door}: {ex!r})")

    client.on_connect, client.on_message = connected, message
    client.reconnect_delay_set(1, 60)
    client.connect_async(mqtt["address"], int(mqtt.get("port", 1883)))
    client.loop_start()
    return client


def broker_answers(mqtt: dict, password: str | None, timeout_s: float = 5.0) -> str:
    """--check's: "" if the broker in [presence.mqtt] lets TARS in, otherwise why not."""
    try:
        import paho.mqtt.client as paho
    except ImportError:
        return "listening to the locks needs the door extra: uv sync --extra door"
    client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"tars-check-{int(time.time())}")
    if mqtt.get("user"):
        client.username_pw_set(mqtt["user"], password)
    answer: list[str] = []
    client.on_connect = lambda c, u, f, reason, p=None: answer.append(str(reason) if reason.is_failure else "")
    try:
        client.connect(mqtt["address"], int(mqtt.get("port", 1883)), keepalive=10)
        deadline = time.monotonic() + timeout_s
        while not answer and time.monotonic() < deadline:
            client.loop(timeout=0.2)
    except OSError as e:
        return f"can't reach it: {e}"
    finally:
        client.disconnect()
    if not answer:
        return "it didn't answer"
    return f"it refused TARS: {answer[0]}" if answer[0] else ""
