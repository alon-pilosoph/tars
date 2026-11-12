"""Doors that say who opened them: a Nuki smart lock's own events, over MQTT (docs/presence.md#the-front-door).

A Nuki with MQTT turned on publishes every lock action to `<topic>/lockActionEvent` as "action,trigger,auth id,code
id,auto unlock": which app (the auth id) or keypad code (the code id) did what, and whether the phone's Auto Unlock
did it. Each one is kept in the door_events table. Whose key is whose comes from [presence] people ("key:<auth id>",
"code:<code id>") or is learned: a key nobody's named that opens the door from outside while exactly one person's phone
comes home, on two separate arrivals and never while someone else's does, is theirs from then on, and the maintainers
are told.

Someone opening the door from outside (Auto Unlock, or the keypad) is home at once, before their phone has found the
Wi-Fi: presence.Watcher.at_door. Opening it from the app alone doesn't count: that can be done from anywhere. What a
lock says only ever puts someone at home: it never works the house (a skill it sets off reads the house but changes
nothing in it, home.Request.by_door), and TARS publishes nothing to the broker.

A door can have a sensor too (Home Assistant's binary_sensor for a Shelly BLU Door/Window, followed by hass.follow):
whoever the lock let in from outside has come in once the door shuts behind them, and that's an event of its own,
kept in door_events (CAME_IN) and handed on (`came_in`): skills run on it (skills.COMES_IN: the household's
"welcome home" is one), and their messages waiting until they're back are said then. Each time it moves the routers
are asked at once, and every few seconds for a while (Watcher.hurry), and the door opened without anyone being let
in is someone inside using it: a phone that drops off then has left by it (presence.Tracker.door_used).

Whether TARS can hear the locks is watched too (Link): the broker not answering, refusing TARS, or lost, is logged
when it starts, and the maintainers are told once it's lasted TELL_AFTER_S, and again when it's back.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .events import person_key
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
LEARN_AFTER = 2  # arrivals guessed, all the same person, before a key is taken to be theirs
SETTLE_EVERY_S = 30.0
# Out of touch with the broker this long, and the maintainers are told: a broker restarting, or the network coming up
# after a power cut, is back well within it.
TELL_AFTER_S = 10 * 60.0
# A lock opened from outside this near the door sensor's opening (before it, or while it's open) let that person in.
LET_IN_S = 2 * 60.0
# The door opened again this soon after someone came in by it is them still coming in (it bounced off the frame, the
# shopping from the car), never someone inside leaving: someone who does leave then waits away_after_min.
REOPEN_S = 60.0
CAME_IN = 100  # door_events.action for someone the lock let in coming in, the sensor saying the door shut behind them
PROBE = "tars-check"  # --check's: a topic beside a lock's own, which TARS may write only on a broker without an ACL


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

    def came_in(self, door: str, people: list[str], at: float) -> None:
        """`people` (person_key) came in by `door` at `at`: one row each, settled (nothing to learn from it)."""
        for person in people:
            self.store.write(
                "INSERT INTO door_events (at, door, action, trigger, key, auto_unlock, person, settled) "
                "VALUES (?, ?, ?, 0, 'sensor', 0, ?, 1)",
                (at, door, CAME_IN, person),
            )

    def came_in_since(self, after_id: int) -> list[dict]:
        """Who came in after the event `after_id`, in order: id, at, door, person (person_key)."""
        return self.store.rows(
            "SELECT id, at, door, person FROM door_events WHERE action = ? AND id > ? ORDER BY id", (CAME_IN, after_id)
        )

    def last_id(self) -> int:
        row = self.store.row("SELECT MAX(id) AS id FROM door_events")
        return (row or {}).get("id") or 0

    def unsettled(self, before: float) -> list[dict]:
        """Openings from outside by a key nobody's named, from before `before`, not yet compared with who came home.
        key:0 is the lock's own button or knob, nobody's key."""
        return self.store.rows(
            "SELECT * FROM door_events WHERE person IS NULL AND settled = 0 AND at < ? AND action IN (?, ?) "
            "AND (auto_unlock = 1 OR key LIKE 'code:%') AND key != 'key:0'",
            (before, *OPENS),
        )

    def settle(self, event_id: int, guess: str | None, arrived: float | None = None) -> str | None:
        """Keeps the guess (or that there was none) for one opening, with when the guessed person's phone came home;
        returns the person if its key is now learned."""
        with self.store.transaction() as db:
            db.execute(
                "UPDATE door_events SET settled = 1, guess = ?, guess_at = ? WHERE id = ?", (guess, arrived, event_id)
            )
            key = db.execute("SELECT key FROM door_events WHERE id = ?", (event_id,)).fetchone()["key"]
            if guess is None or db.execute("SELECT 1 FROM door_keys WHERE key = ?", (key,)).fetchone():
                return None
            rows = db.execute("SELECT guess, guess_at FROM door_events WHERE key = ? AND guess IS NOT NULL", (key,))
            guesses = [(r["guess"], r["guess_at"]) for r in rows]
            # An unlock and an unlatch in one homecoming are one arrival.
            if len({at for _, at in guesses}) < LEARN_AFTER or {p for p, _ in guesses} != {guess}:
                return None
            db.execute("INSERT INTO door_keys (key, person, learned) VALUES (?, ?, ?)", (key, guess, time.time()))
            return guess


class Doors:
    """Listens to each door's lock over MQTT, keeps what it says, puts whoever opens it from outside at home
    (watcher.at_door), and learns whose key is whose from phones coming home."""

    def __init__(
        self,
        log: DoorLog,
        watcher: Watcher | None,
        tell: Callable[[str], None] = lambda text: None,
        clock: Callable[[], float] = time.time,
        came_in: Callable[[list[str], str], None] | None = None,
    ):
        """`came_in(names, door)`: told when people the lock let in have come in, as [presence] people names them."""
        self.log, self.watcher, self.tell, self._clock = log, watcher, tell, clock
        self.on_came_in = came_in
        self.link: Link | None = None  # the broker, once listening: looked at with each settle
        self._arrivals: list[tuple[str, float]] = []  # (person, when) their phone came home, while TARS runs
        self._let_in: dict[str, list[tuple[str, float]]] = {}  # door: (person, when) its lock let them in from outside
        self._opened: dict[str, float] = {}  # door: when its sensor said it opened, while it's open
        self._came_in_at: dict[str, float] = {}  # door: when someone last came in by it
        self._lock = threading.Lock()

    def heard(self, e: DoorEvent) -> None:
        """One event from a lock."""
        person = self.log.who(e.key)
        self.log.record(e, person)
        names = self.log.people.names
        print(f"(doors: the {e.door} {e.words()}, {f'by {names[person]}' if person else f'with {e.key}'})")
        if person and e.from_outside:
            with self._lock:
                self._let_in.setdefault(e.door, []).append((person, e.at))
            if self.watcher:
                self.watcher.at_door(person, e.door)

    def sensed(self, door: str, is_open: bool) -> None:
        """The door's sensor: it opened, or shut. The routers are asked at once. As it shuts, whoever its lock let in
        from outside, from LET_IN_S before it opened until now, has come in; with nobody let in it was someone inside
        using it (Watcher.hurry's `inside`, from when it opened), unless it opened within REOPEN_S of someone coming
        in by it. Decided at the shut, not the opening: the lock's event can come a moment after the sensor's."""
        now, inside, came, opened = self._clock(), False, [], None
        print(f"(doors: the {door} {'opened' if is_open else 'shut'})")
        with self._lock:
            let_in = [(p, at) for p, at in self._let_in.get(door, []) if now - at <= 2 * LET_IN_S]
            self._let_in[door] = let_in
            if is_open:
                self._opened[door] = now
            elif (opened := self._opened.pop(door, None)) is not None:
                came = list(dict.fromkeys(p for p, at in let_in if opened - LET_IN_S <= at <= now))
                self._let_in[door] = [(p, at) for p, at in let_in if p not in came]
                if came:
                    self._came_in_at[door] = now
                else:
                    inside = opened - self._came_in_at.get(door, -REOPEN_S - 1) > REOPEN_S
        if self.watcher:
            self.watcher.hurry(inside=inside, opened=opened if inside else None)
        if not came:
            return
        names = [self.log.people.names[p] for p in came]
        print(f"(doors: {', '.join(names)} came in by the {door})")
        try:
            self.log.came_in(door, came, now)
        except Exception as e:  # noqa: BLE001 - kept or not, they're in: what's waiting for them is still told
            print(f"(doors: couldn't keep that {', '.join(names)} came in: {e!r})")
        if self.on_came_in:
            self.on_came_in(names, door)

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
            near = {p: t for p, t in arrivals if abs(t - row["at"]) <= LEARN_WINDOW_S}
            guess, arrived = next(iter(near.items())) if len(near) == 1 else (None, None)
            if learned := self.log.settle(row["id"], person_key(guess), arrived):
                name = self.log.people.names[learned]
                print(f"(doors: {row['key']} on the {row['door']} is {name}'s)")
                self.tell(
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
                try:
                    if self.link:
                        self.link.look()
                except Exception as e:  # noqa: BLE001 - the next round tries again
                    print(f"(doors: couldn't tell the maintainers about the broker: {e!r})")

        threading.Thread(target=loop, daemon=True, name="doors").start()
        return self


UNREACHABLE, REFUSED = "unreachable", "refused"  # Link's trouble: the broker doesn't answer, or won't let TARS in


class Link:
    """Whether TARS can hear the locks: its connection to the broker, for the log and TARS's maintainers. paho
    reconnects by itself; this says what's going on. Trouble is logged when it starts or changes kind (not at each
    retry), and the maintainers (`tell`) are told once it's lasted TELL_AFTER_S, the first connection included, and
    again when it's back; trouble that's over sooner tells nobody. look() is what notices the time has passed."""

    def __init__(
        self,
        address: str,
        doors: str,
        tell: Callable[[str], None] = lambda text: None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.address, self.doors, self._tell, self._clock = address, doors, tell, clock
        self.up = False
        self.trouble = ""  # UNREACHABLE or REFUSED while it's down for a known reason; "" while up, or before trying
        self.why = ""  # that trouble, in words
        self.told = False  # the maintainers were told it's down, and not yet that it's back
        self._down_since = clock()  # not connected yet counts from the start
        self._ever = False
        self._lock = threading.Lock()

    def connected(self) -> None:
        with self._lock:
            was_down, told = bool(self.trouble), self.told
            self.up, self.trouble, self.why, self.told = True, "", "", False
            first, self._ever = not self._ever, True
        if first:
            print(f"(doors: listening to the {self.doors} on the MQTT broker at {self.address})")
        elif was_down:
            print(f"(doors: the MQTT broker at {self.address} is back)")
        if told:
            self._tell(f"I can hear the {self.doors} again: the MQTT broker at {self.address} answers.")

    def unreachable(self) -> None:
        """A connection that couldn't be made: nothing listening there, no route, a name that doesn't resolve."""
        self._down(UNREACHABLE, f"can't reach the MQTT broker at {self.address}")

    def refused(self, reason: object) -> None:
        """The broker answered, and wouldn't let TARS in: a wrong user or password, or one it doesn't know."""
        self._down(REFUSED, f"the MQTT broker at {self.address} refused TARS: {reason}")

    def lost(self, reason: object) -> None:
        """A connection that was up dropped. Anything else paho reports as a disconnection (after a refusal, while
        reconnecting) is already said."""
        if self.up:
            self._down(UNREACHABLE, f"lost the MQTT broker at {self.address} ({reason})")

    def _down(self, trouble: str, why: str) -> None:
        with self._lock:
            if self.up:
                self.up, self._down_since = False, self._clock()
            new = trouble != self.trouble
            self.trouble, self.why = trouble, why
        if new:
            print(f"(doors: {why}; trying again until it answers, and nobody is let in by the door until then)")

    def look(self) -> None:
        """Tells the maintainers once TARS has been out of touch with the broker for TELL_AFTER_S."""
        with self._lock:
            if self.up or self.told or self._clock() - self._down_since < TELL_AFTER_S:
                return
            self.told = True
            minutes, refused = round((self._clock() - self._down_since) / 60), self.trouble == REFUSED
            why = self.why or f"the MQTT broker at {self.address} hasn't answered"
        fix = (
            "Check TARS's user and password for it: [presence.mqtt] in config.local.toml, its password in .env, and "
            "Mosquitto's passwords (deploy/install-pi.sh sets TARS's up)."
            if refused
            else "Is Mosquitto running? On the Pi: cd deploy/home-assistant && docker compose --profile mqtt up -d."
        )
        self._tell(
            f"I can't hear the {self.doors}: {why}, for {minutes} minutes now, so opening it lets nobody in as home "
            f"until it's back. {fix}"
        )


def listen(
    mqtt: dict,
    password: str | None,
    doors: list[dict],
    on_event: Callable[[DoorEvent], None],
    link: Link | None = None,
):
    """Subscribes to each door's lockActionEvent on the broker in [presence.mqtt], reconnecting by itself, and tells
    `link` how the connection goes. Needs the door extra (paho-mqtt)."""
    import paho.mqtt.client as paho

    topics = {f"{d['topic'].rstrip('/')}/lockActionEvent": d["name"] for d in doors}
    link = link or Link(mqtt["address"], ", ".join(topics.values()))
    client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"tars-doors-{int(time.time())}")
    if mqtt.get("user"):
        client.username_pw_set(mqtt["user"], password)

    def connected(client, userdata, flags, reason, properties=None):
        if reason.is_failure:
            link.refused(reason)
            return
        link.connected()
        for topic in topics:
            client.subscribe(topic, qos=1)

    def connect_failed(client, userdata):
        link.unreachable()

    def disconnected(client, userdata, flags, reason, properties=None):
        link.lost(reason)

    def message(client, userdata, msg):
        if msg.retain:
            return  # the broker's copy of an old event, sent on (re)connecting
        door = topics.get(msg.topic)
        e = parse(door, msg.payload.decode(errors="replace"), time.time()) if door else None
        if e is None:
            return
        try:
            on_event(e)
        except Exception as ex:  # noqa: BLE001 - one event's trouble never stops the listening
            print(f"(doors: couldn't act on the {door}: {ex!r})")

    client.on_connect, client.on_connect_fail = connected, connect_failed
    client.on_disconnect, client.on_message = disconnected, message
    client.reconnect_delay_set(1, 60)
    client.connect_async(mqtt["address"], int(mqtt.get("port", 1883)))
    client.loop_start()
    return client


def broker_answers(
    mqtt: dict, password: str | None, topics: list[str] = (), timeout_s: float = 5.0, probe_s: float = 2.0
) -> tuple[str, str]:
    """--check's: whether the broker in [presence.mqtt] lets TARS in (why not, or ""), and whether it keeps each
    lock's topics (`topics`, as [[presence.doors]] has them) to the lock (a warning, or ""). TARS should only read
    there: if what it writes beside a lock's events comes back to it, the broker has no ACL, and any user it knows
    could say a door was opened. The probe is beside the events (<topic>/tars-check), never one the lock acts on."""
    try:
        import paho.mqtt.client as paho
    except ImportError:
        return "listening to the locks needs the door extra: uv sync --extra door", ""
    client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"tars-check-{int(time.time())}")
    if mqtt.get("user"):
        client.username_pw_set(mqtt["user"], password)
    answer: list[str] = []
    echoed: list[str] = []
    client.on_connect = lambda c, u, f, reason, p=None: answer.append(str(reason) if reason.is_failure else "")
    client.on_message = lambda c, u, msg: echoed.append(msg.topic)
    probes = [f"{t.rstrip('/')}/{PROBE}" for t in topics]
    try:
        client.connect(mqtt["address"], int(mqtt.get("port", 1883)), keepalive=10)
        deadline = time.monotonic() + timeout_s
        while not answer and time.monotonic() < deadline:
            client.loop(timeout=0.2)
        if answer == [""] and probes:
            for probe in probes:
                client.subscribe(probe, qos=1)
                client.publish(probe, b"", qos=1)
            deadline = time.monotonic() + probe_s
            while not echoed and time.monotonic() < deadline:
                client.loop(timeout=0.2)
    except OSError as e:
        return f"can't reach it: {e}", ""
    finally:
        client.disconnect()
    if not answer:
        return "it didn't answer", ""
    if answer[0]:
        return f"it refused TARS: {answer[0]}", ""
    if echoed:
        return "", (
            f"it lets TARS write on {echoed[0].removesuffix('/' + PROBE)}, a lock's own topic: without an ACL "
            "keeping it to the lock, any device with a password there could say the door was opened"
        )
    return "", ""
