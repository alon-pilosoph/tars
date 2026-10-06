"""Reminders, timers and messages: set by voice or on the web UI, said aloud when they're due, and said again until
someone acknowledges them, if they wait for that.

The assistant says them, the brain's tools set them, and the web UI does both, all through Reminders, over one table
in the event database (store.REMINDERS). The web UI is another process, so the assistant reads the table again
rather than keeping it in memory. See docs/reminders.md.
"""

import dataclasses
import math
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime

from .config import RemindersConfig
from .events import person_key
from .store import Store

TIMER, REMINDER, MESSAGE = "timer", "reminder", "message"
KINDS = (TIMER, REMINDER, MESSAGE)
SCHEDULED, WAITING, ACKNOWLEDGED, SAID, MISSED, CANCELLED = (
    "scheduled",  # not said yet
    "waiting",  # said, waiting for an acknowledgement, and said again until then
    "acknowledged",
    "said",  # said once; it didn't wait for an acknowledgement
    "missed",  # said max_tries times and never acknowledged
    "cancelled",
)
ACTIVE = (SCHEDULED, WAITING)
CANCELLABLE = ACTIVE
SNOOZABLE = (*ACTIVE, MISSED)  # a missed one can be put off and tried again
VOICE, WEB = "voice", "web"
ADD, CANCEL, SNOOZE = "add", "cancel", "snooze"  # a Change

# Reminders and messages are said again every 5 minutes, 4 times at most. A timer rings like a kitchen timer until
# someone turns it off: a chime every 10 s (TARS listens in between, since it can't hear while it makes a sound), its
# line on the first ring and about once a minute after, for at most 15 minutes.
REPEAT_EVERY_S = 300.0
MAX_TRIES = 4
TIMER_EVERY_S = 10.0
TIMER_RING_S = 15 * 60.0
SAY_TIMER_EVERY = 6  # rings
MIN_REPEAT_S = 30.0
MIN_TIMER_EVERY_S = 5.0
MAX_TRIES_LIMIT = 30
MAX_RINGS = 360
MAX_AHEAD_S = 366 * 24 * 3600
# Said later than this after its time (TARS was off, or busy in a long conversation), a reminder says when it was due.
LATE_S = 90.0
MAX_TEXT = 300


@dataclass
class NewReminder:
    """What someone asked for, before it's checked. `due` is a Unix time; None = when `for_name` is next heard."""

    kind: str
    text: str | None = None
    for_name: str | None = None
    from_name: str | None = None
    due: float | None = None
    needs_ack: bool = True
    repeat_every_s: float | None = None  # None: the configured default
    max_tries: int | None = None


class Reminders:
    def __init__(
        self,
        store: Store,
        repeat_every_s: float = REPEAT_EVERY_S,
        max_tries: int = MAX_TRIES,
        timer_ring_s: float = TIMER_RING_S,
    ):
        self.store = store
        self.repeat_every_s, self.max_tries = repeat_every_s, max_tries
        self.timer_ring_s = timer_ring_s

    @classmethod
    def from_config(cls, store: Store, cfg: RemindersConfig) -> "Reminders":
        return cls(store, cfg.repeat_every_min * 60, cfg.max_tries, cfg.timer_ring_min * 60)

    def add(
        self,
        new: NewReminder,
        set_via: str,
        voices: Collection[str] = (),
        conversation_id: int | None = None,
        now: float | None = None,
    ) -> int:
        """Raises ValueError, saying what's wrong, for anything that can't be done. `voices`: the names TARS knows by
        voice, which waiting until someone's back needs."""
        now = time.time() if now is None else now
        r = self.check(new, voices, now)
        return self.store.write(
            "INSERT INTO reminders (created, kind, text, for_name, from_name, set_via, conversation_id, due, "
            "needs_ack, repeat_every_s, max_tries, status, next_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                r.kind,
                r.text,
                r.for_name,
                r.from_name,
                set_via,
                conversation_id,
                r.due,
                int(r.needs_ack),
                r.repeat_every_s,
                r.max_tries,
                SCHEDULED,
                r.due,
            ),
        )

    def check(self, new: NewReminder, voices: Collection[str] = (), now: float | None = None) -> NewReminder:
        """The reminder tidied up and with its defaults, or ValueError saying what's wrong."""
        now = time.time() if now is None else now
        if new.kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        text = " ".join((new.text or "").split())[:MAX_TEXT] or None
        for_name = " ".join((new.for_name or "").split()) or None
        from_name = " ".join((new.from_name or "").split()) or None
        if new.kind != TIMER and not text:
            raise ValueError(f"a {new.kind} needs something to say")
        if new.kind == MESSAGE and not for_name:
            raise ValueError("a message needs someone it's for")
        if not all(math.isfinite(x) for x in (new.due, new.repeat_every_s, new.max_tries) if x is not None):
            raise ValueError("the numbers must be real numbers")
        if new.due is None:
            if not for_name:
                raise ValueError("it needs a time, or someone to wait for")
            if person_key(for_name) not in {person_key(v) for v in voices}:
                raise ValueError(
                    f"TARS doesn't know {for_name}'s voice yet, so it can't wait until they're back; give a time"
                )
        elif new.due < now - LATE_S:
            raise ValueError("that time has already passed")
        elif new.due > now + MAX_AHEAD_S:
            raise ValueError("that's more than a year away")
        if new.kind == TIMER:  # rings until someone turns it off, whatever was asked
            repeat = TIMER_EVERY_S if new.repeat_every_s is None else new.repeat_every_s
            tries = math.ceil(self.timer_ring_s / repeat) if new.max_tries is None else new.max_tries
            if repeat < MIN_TIMER_EVERY_S:
                raise ValueError(f"a timer can ring at most every {MIN_TIMER_EVERY_S:.0f} seconds")
            if not 1 <= tries <= MAX_RINGS:
                raise ValueError(f"a timer can ring 1 to {MAX_RINGS} times")
            return NewReminder(new.kind, text, for_name, from_name, new.due, True, repeat, tries)
        repeat = self.repeat_every_s if new.repeat_every_s is None else new.repeat_every_s
        tries = self.max_tries if new.max_tries is None else new.max_tries
        if repeat < MIN_REPEAT_S:
            raise ValueError(f"it can be said again at most every {MIN_REPEAT_S:.0f} seconds")
        if not 1 <= tries <= MAX_TRIES_LIMIT:
            raise ValueError(f"it can be said 1 to {MAX_TRIES_LIMIT} times")
        return NewReminder(new.kind, text, for_name, from_name, new.due, bool(new.needs_ack), repeat, tries)

    def get(self, reminder_id: int) -> dict | None:
        rows = self.store.rows("SELECT * FROM reminders WHERE id=?", (reminder_id,))
        return rows[0] if rows else None

    def all(self, limit: int = 200) -> list[dict]:
        """The active ones first, soonest first; then the rest, newest first."""
        marks = ",".join("?" * len(ACTIVE))
        return self.store.rows(
            f"SELECT * FROM reminders ORDER BY status IN ({marks}) DESC, "
            f"CASE WHEN status IN ({marks}) THEN coalesce(next_at, 1e18) ELSE -created END LIMIT ?",
            (*ACTIVE, *ACTIVE, limit),
        )

    def active(self) -> list[dict]:
        marks = ",".join("?" * len(ACTIVE))
        return self.store.rows(
            f"SELECT * FROM reminders WHERE status IN ({marks}) ORDER BY coalesce(next_at, 1e18), id", ACTIVE
        )

    def waiting(self) -> list[dict]:
        """Said, and waiting for someone to acknowledge them: the most recently said first."""
        return self.store.rows("SELECT * FROM reminders WHERE status=? ORDER BY last_said DESC, id DESC", (WAITING,))

    def next_at(self) -> float | None:
        rows = self.store.rows(
            f"SELECT min(next_at) AS t FROM reminders WHERE status IN ({','.join('?' * len(ACTIVE))})", ACTIVE
        )
        return rows[0]["t"]

    def due(self, now: float | None = None) -> list[dict]:
        """What to say now. One said max_tries times and still not acknowledged is Missed instead."""
        now = time.time() if now is None else now
        marks = ",".join("?" * len(ACTIVE))
        with self.store.transaction() as db:
            db.execute(
                f"UPDATE reminders SET status=?, next_at=NULL WHERE status IN ({marks}) AND next_at <= ? "
                "AND tries >= max_tries",
                (MISSED, *ACTIVE, now),
            )
            rows = db.execute(
                f"SELECT * FROM reminders WHERE status IN ({marks}) AND next_at <= ? ORDER BY next_at, id",
                (*ACTIVE, now),
            ).fetchall()
        return [dict(r) for r in rows]

    def held_for(self, name: str | None) -> list[dict]:
        """Messages waiting until `name`'s voice is heard."""
        if not person_key(name):
            return []
        rows = self.store.rows(
            "SELECT * FROM reminders WHERE status=? AND due IS NULL AND tries=0 ORDER BY id", (SCHEDULED,)
        )
        return [r for r in rows if person_key(r["for_name"]) == person_key(name)]

    def said(self, reminder_id: int, now: float | None = None) -> None:
        """It was just said: once and done, or again in repeat_every_s unless someone acknowledges it."""
        now = time.time() if now is None else now
        self.store.write(
            "UPDATE reminders SET tries=tries+1, last_said=?, "
            "status=CASE WHEN needs_ack THEN ? ELSE ? END, "
            "next_at=CASE WHEN needs_ack THEN ? + repeat_every_s ELSE NULL END "
            "WHERE id=? AND status IN (?, ?)",
            (now, WAITING, SAID, now, reminder_id, *ACTIVE),
        )

    def ack(self, reminder_id: int, by: str | None, via: str, now: float | None = None) -> bool:
        """False if it was no longer active (acknowledged already, cancelled, missed)."""
        now = time.time() if now is None else now
        return self._change(
            "UPDATE reminders SET status=?, next_at=NULL, acked_at=?, acked_by=?, acked_via=? "
            "WHERE id=? AND status IN (?, ?)",
            (ACKNOWLEDGED, now, by, via, reminder_id, *ACTIVE),
        )

    def snooze(self, reminder_id: int, minutes: float, now: float | None = None) -> bool:
        """Said again in `minutes`, as if new: it gets its full number of tries again."""
        now = time.time() if now is None else now
        if not (math.isfinite(minutes) and 0 < minutes <= MAX_AHEAD_S / 60):
            raise ValueError("snooze for a positive number of minutes")
        return self._change(
            f"UPDATE reminders SET status=?, tries=0, next_at=? WHERE id=? AND status IN ({_marks(SNOOZABLE)})",
            (SCHEDULED, now + minutes * 60, reminder_id, *SNOOZABLE),
        )

    def cancel(self, reminder_id: int) -> bool:
        return self._change(
            f"UPDATE reminders SET status=?, next_at=NULL WHERE id=? AND status IN ({_marks(CANCELLABLE)})",
            (CANCELLED, reminder_id, *CANCELLABLE),
        )

    def _change(self, sql: str, args: tuple) -> bool:
        with self.store.transaction() as db:
            return db.execute(sql, args).rowcount > 0


class Clock:
    """When the next reminder is due, read from the table every `every_s` on a thread of its own, so the wake loop's
    question, asked every 80 ms, costs a comparison. `on_read` gets the active reminders each time (to make their
    audio ahead).

    The table can fail (a full disk, a broken file), and then what it says is due stays due. So the clock also keeps
    its own holds: a reminder held isn't due again until the hold ends, whatever the table says, and nothing is due
    while the clock is paused. Without them TARS would say the same line over and over, or never hear "hey TARS"
    again (the wake loop stops the moment something is due)."""

    def __init__(self, reminders: Reminders, every_s: float = 2.0, on_read: Callable[[list[dict]], None] | None = None):
        self._reminders, self._every_s, self._on_read = reminders, every_s, on_read
        self._next: float | None = None
        self._holds: dict[int, float] = {}
        self._paused_until = 0.0

    def start(self) -> None:
        def loop():
            while True:
                self.read()
                time.sleep(self._every_s)

        threading.Thread(target=loop, daemon=True, name="reminders").start()

    def read(self) -> None:
        """Never raises: a failed read leaves nothing due until the next good one."""
        try:
            active = self._reminders.active()
            now = time.time()
            ids = {r["id"] for r in active}
            self._holds = {i: until for i, until in self._holds.items() if i in ids and until > now}
            self._next = min(
                (max(r["next_at"], self._holds.get(r["id"], 0.0)) for r in active if r["next_at"] is not None),
                default=None,
            )
            if self._on_read:
                self._on_read(active)
        except Exception as e:  # noqa: BLE001 - a locked or broken table, or the audio made ahead failing
            self._next = None
            print(f"(couldn't read the reminders: {e!r})")

    def hold(self, reminder_id: int, until: float) -> None:
        self._holds[reminder_id] = until
        self.read()

    def held(self, reminder_id: int, now: float | None = None) -> bool:
        return self._holds.get(reminder_id, 0.0) > (time.time() if now is None else now)

    def pause(self, seconds: float) -> None:
        self._paused_until = time.time() + seconds

    def is_due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        at = self._next
        return now >= self._paused_until and at is not None and at <= now


def _marks(values: tuple) -> str:
    return ",".join("?" * len(values))


def name(n: str) -> str:
    """How a name is said: "stacey" → "Stacey", "mary ann" → "Mary Ann"."""
    return " ".join(w[:1].upper() + w[1:] for w in n.split())


def _sentence(text: str) -> str:
    return text if text[-1:] in ".!?" else f"{text}."


def line(r: dict) -> str:
    """What TARS says for it, the same every time, so its audio is made once."""
    who = f"{name(r['for_name'])}, " if r["for_name"] else ""
    sender = r["from_name"] if person_key(r["from_name"]) != person_key(r["for_name"]) else None
    if r["kind"] == TIMER:
        label = f"{r['text']} timer" if r["text"] else "timer"
        return f"{who}your {label} is done." if who else f"Your {label} is done."
    what = "a message" if r["kind"] == MESSAGE else "a reminder"
    head = f"{who}{what}" if who else what.capitalize()
    if sender:
        head += f" from {name(sender)}"
    return f"{head}: {_sentence(r['text'])}"


def says_line(r: dict) -> bool:
    """Whether this time it's said, or (a timer still ringing) only chimes."""
    return r["kind"] != TIMER or r["tries"] % SAY_TIMER_EVERY == 0


def held_line(r: dict) -> str:
    """A message that waited until its person was heard, said in the middle of a conversation with them."""
    return f"By the way, {line(r)}"


def late(r: dict, now: float | None = None) -> str | None:
    """ "This was due at 8:00." when it's said for the first time well after it was meant to be (TARS was off, or in
    a long conversation), else None. Meant to be is next_at, not due: a snoozed one is on time at its new time."""
    now = time.time() if now is None else now
    if r["tries"] or r["next_at"] is None or now - r["next_at"] < LATE_S:
        return None
    due = datetime.fromtimestamp(r["next_at"]).astimezone()
    today = datetime.fromtimestamp(now).astimezone().date() == due.date()
    clock = f"{due:%I:%M %p}".lstrip("0")
    return f"This was due at {clock}." if today else f"This was due {due:%A} at {clock}."


def when(at: float, now: float | None = None) -> str:
    """ "7:30 PM", "tomorrow at 7:30 AM", "Tuesday at 9:00 AM", "October 20 at 9:00 AM", in local time."""
    now = time.time() if now is None else now
    t, today = datetime.fromtimestamp(at).astimezone(), datetime.fromtimestamp(now).astimezone().date()
    clock = f"{t:%I:%M %p}".lstrip("0")
    days = (t.date() - today).days
    if days == 0:
        return clock
    if days == 1:
        return f"tomorrow at {clock}"
    if 1 < days < 7:
        return f"{t:%A} at {clock}"
    return f"{t:%B} {t.day} at {clock}"


@dataclass
class Change:
    """A change asked for by voice. Made only once the turn is kept, so a draft thrown away changes nothing."""

    action: str  # ADD, CANCEL or SNOOZE
    reminder: NewReminder | None = None
    id: int | None = None
    minutes: float | None = None
    asked_at: float = 0.0  # when the model asked: an ADD due "now" mustn't count as passed by the time it's kept


class ReminderTools:
    """The brain's side of reminders: what's set now, for its instructions, and its tools, which check what the model
    asks for and say what's wrong so it can try again."""

    def __init__(self, reminders: Reminders, voices: Callable[[], Collection[str]] = lambda: ()):
        self.reminders, self._voices = reminders, voices

    def note(self, now: float | None = None) -> str:
        """The active reminders, by number, for the instructions; "" if there are none."""
        now = time.time() if now is None else now
        active = self.reminders.active()
        if not active:
            return ""
        rows = []
        for r in active:
            at = when(r["next_at"], now) if r["next_at"] is not None else f"when {name(r['for_name'])} is next heard"
            state = (
                f"said {r['tries']} time{'s' * (r['tries'] != 1)}, waiting for someone to acknowledge it"
                if r["status"] == WAITING
                else "not said yet"
            )
            rows.append(f'[{r["id"]}] {at}: "{line(r)}" ({state})')
        note = "Timers, reminders and messages set now (answer questions about them from this list):\n" + "\n".join(
            rows
        )
        if any(r["status"] == WAITING for r in active):
            note += (
                '\nWhen someone says they got one that\'s waiting, heard it or did it ("I got the message", "I took '
                "the pills\"), or asks to turn off a timer that's ringing, start your reply with <ack N>, N being its "
                "number (the most recent one if it isn't clear which), then say one short line."
            )
        return note

    def call(self, tool: str, args: dict, now: float | None = None) -> tuple[Change | None, str]:
        """(the change to make once the turn is kept, what to tell the model), or (None, "error: ...")."""
        now = time.time() if now is None else now
        try:
            if tool == "remind":
                return self._remind(args, now)
            if tool in ("cancel_reminder", "snooze_reminder"):
                r = self.reminders.get(int(args.get("id")))
                if tool == "cancel_reminder":
                    if not r or r["status"] not in CANCELLABLE:
                        return None, "error: there's no active reminder with that number, so nothing to cancel"
                    return Change(CANCEL, id=r["id"]), f"cancelled: {line(r)}"
                if not r or r["status"] not in SNOOZABLE:
                    return None, "error: there's no active or missed reminder with that number"
                minutes = float(args.get("minutes"))
                if not (math.isfinite(minutes) and 0 < minutes <= MAX_AHEAD_S / 60):
                    return None, "error: minutes must be more than 0"
                return Change(SNOOZE, id=r["id"], minutes=minutes), f"snoozed until {when(now + minutes * 60, now)}"
        except (TypeError, ValueError) as e:
            return None, f"error: {e}"
        return None, f"error: no tool called {tool}"

    def _remind(self, args: dict, now: float) -> tuple[Change | None, str]:
        if args.get("when_back"):
            due = None
        elif args.get("in_minutes") is not None:
            due = now + float(args["in_minutes"]) * 60
        elif args.get("at"):
            at = datetime.fromisoformat(str(args["at"]))
            due = (at if at.tzinfo else at.astimezone()).timestamp()  # a time without a zone is local
        else:
            raise ValueError("give in_minutes, at, or when_back")
        new = NewReminder(
            kind=args.get("kind"),
            text=args.get("text"),
            for_name=args.get("for"),
            due=due,
            needs_ack=bool(args.get("wait_for_ack", True)),
        )
        new = self.reminders.check(new, self._voices(), now)
        said = line({**new.__dict__, "from_name": None})
        at = when(due, now) if due is not None else f"when {name(new.for_name)} is next heard"
        return Change(ADD, reminder=new, asked_at=now), f'set for {at}; TARS will say: "{said}"'

    def apply(self, change: Change, from_name: str | None, conversation_id: int | None = None) -> None:
        """Makes a change once its turn is kept. A reminder set by voice is from whoever set it."""
        if change.action == ADD:
            new = dataclasses.replace(change.reminder, from_name=from_name)
            self.reminders.add(
                new, VOICE, voices=self._voices(), conversation_id=conversation_id, now=change.asked_at or None
            )
        elif change.action == CANCEL:
            self.reminders.cancel(change.id)
        elif change.action == SNOOZE:
            self.reminders.snooze(change.id, change.minutes)
