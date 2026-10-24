"""TARS's errands in its browser: asked for aloud at home or in a Telegram chat, and run by the Telegram service, which
is where what an errand needs from the person on the way reaches them (telegram.py): a yes before anything is
ordered, a login TARS lacks (a link to a one-time page), a code a site sent them. When it's done, its answer comes to
their chat, and if they asked aloud, it's said aloud at home too (unless they're known to be out).

The brain's browse tool puts an errand in the queue (the errands table, store.py, so the voice's process can hand one
to the Telegram service's), and the Telegram service runs them in turn: one at a time, since there's one browser and
a site's basket is one. Each person has a browser profile of their own, so one person's sign-in never becomes
another's (the household's first keeps the profile TARS had before).

This module imports nothing of the browser itself, so the voice's process can queue an errand without it."""

import hashlib
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..config import BrowserConfig
from ..events import person_key

if TYPE_CHECKING:
    from ..store import Store
    from .login import Answer
    from .login_page import Field

ERRANDS = "voice_data/browser/errands"  # under TARS's folder: each errand's trace; its site notes beside it
VOICE, TELEGRAM = "voice", "telegram"  # how an errand was asked for
QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
LOOK_EVERY_S = 2.0

ERRAND = {
    "type": "string",
    "description": "The errand in full, as the person asked it, with the site, what, how many, when, and any limit "
    "they gave: 'On Wolt, order a large margherita from the nearest pizza place, under 80 shekels'. Everything said "
    "about it in the conversation goes in: the browser hears nothing else.",
}
BROWSE_TOOL = {
    "type": "function",
    "name": "browse",
    "description": "Do an errand on a website in TARS's own browser: order or book something, or find something on a "
    "site (a menu, a price, an order's status, an account's details). It runs on its own for a few minutes; its "
    "answer comes to the person's chat when it's done.",
    "strict": True,
    "parameters": {"type": "object", "additionalProperties": False, "required": ["errand"],
                   "properties": {"errand": ERRAND}},
}  # fmt: skip
CHAT_RULES = (
    "You can do errands on websites with the browse tool: order food on Wolt, book a table, find something on a "
    "site or in someone's account there. It works a real browser signed in to the person's accounts, and takes a few "
    "minutes; say you're on it in one short line, and nothing more until it's done: its answer comes to this chat by "
    "itself. Before anything is ordered, booked or paid for, the person is asked here to approve it, with what and "
    "the total; never say something is ordered until they've approved it and the browser says it's done. Use the "
    "web search for a quick question that needs no site of their own. One errand runs at a time."
)
VOICE_RULES = (
    "You can do errands on websites with the browse tool: order food on Wolt, book a table, find something on a "
    "site or in someone's account there. It works a real browser signed in to the person's own accounts, so it's "
    "only for someone whose voice TARS knows (the [Speaker: name] tag) and who has a Telegram chat: for anyone else, "
    "say you can't tell whose account to use. It takes a few minutes; say in one short line that you're on it and "
    "that anything it needs, like approving the order, comes to their phone. The answer is said here when it's done, "
    "and comes to their phone too. Never say something is ordered: they approve it on their phone first. Use the web "
    "search for a quick question that needs no site of their own."
)
CAN_BROWSE = (
    "do errands on websites in TARS's own browser (ordering, booking, looking something up on a site), with the "
    "browse tool"
)
NEEDS_BROWSING = "an errand on a website: ordering or booking something, or finding something in an account on a site"


class Queue:
    """The errands asked for and not yet done: in the event database when there is one (shared by the processes),
    else in memory (the Telegram service's own, then)."""

    def __init__(self, store: Store | None = None, clock: Callable[[], float] = time.time):
        self.store, self._clock = store, clock
        self._rows: list[dict] = []  # with no store
        self._lock = threading.Lock()

    def add(self, person: str, goal: str, via: str) -> int:
        now = self._clock()
        if self.store:
            return self.store.write("INSERT INTO errands (created, person, goal, asked_via, status) VALUES "
                                    "(?, ?, ?, ?, ?)", (now, person, goal, via, QUEUED))  # fmt: skip
        with self._lock:
            row = {"id": len(self._rows) + 1, "created": now, "person": person, "goal": goal, "asked_via": via,
                   "status": QUEUED, "started": None, "ended": None, "answer": None}  # fmt: skip
            self._rows.append(row)
            return row["id"]

    def ahead(self) -> list[dict]:
        """The errands running or waiting their turn, oldest first."""
        if self.store:
            return self.store.rows("SELECT * FROM errands WHERE status IN (?, ?) ORDER BY id", (RUNNING, QUEUED))
        with self._lock:
            return [dict(r) for r in self._rows if r["status"] in (RUNNING, QUEUED)]

    def claim(self) -> dict | None:
        """The oldest errand waiting its turn, now running; None if there's none."""
        now = self._clock()
        if self.store:
            with self.store.transaction() as db:  # read and marked at once: two services never take the same one
                row = db.execute("SELECT * FROM errands WHERE status=? ORDER BY id LIMIT 1", (QUEUED,)).fetchone()
                if row is None:
                    return None
                db.execute("UPDATE errands SET status=?, started=? WHERE id=?", (RUNNING, now, row["id"]))
                return {**dict(row), "status": RUNNING, "started": now}
        with self._lock:
            found = next((r for r in self._rows if r["status"] == QUEUED), None)
            if found:
                found.update(status=RUNNING, started=now)
            return dict(found) if found else None

    def finish(self, errand_id: int, status: str, answer: str) -> None:
        now = self._clock()
        if self.store:
            self.store.write("UPDATE errands SET status=?, ended=?, answer=? WHERE id=?",
                             (status, now, answer, errand_id))  # fmt: skip
            return
        with self._lock:
            for r in self._rows:
                if r["id"] == errand_id:
                    r.update(status=status, ended=now, answer=answer)

    def left_running(self) -> int:
        """Errands a stopped service left running: failed, since nobody is working on them any more."""
        if not self.store:
            return 0
        with self.store.transaction() as db:
            gone = "TARS stopped while it was on this errand"
            return db.execute("UPDATE errands SET status=?, ended=?, answer=? WHERE status=?",
                              (FAILED, self._clock(), gone, RUNNING)).rowcount  # fmt: skip


class BrowseTool:
    """The browse tool in a brain: queues an errand for whoever is asking. `via`: VOICE or TELEGRAM, which says how
    the brain is told about it, and whether the answer is said aloud as well."""

    tool, can, needs = BROWSE_TOOL, CAN_BROWSE, NEEDS_BROWSING

    def __init__(self, queue: Queue, people: list[str], via: str):
        """`people`: who may ask: the household's Telegram chats."""
        self.queue, self._people, self.via = queue, {person_key(p): p for p in people}, via
        self.rules = VOICE_RULES if via == VOICE else CHAT_RULES

    def call(self, args: dict, request) -> str:
        """The brain's browse call, for whoever `request` says is asking."""
        person = getattr(request, "speaker", None)
        if not person or person_key(person) not in self._people:
            return ("error: TARS doesn't know who's asking (an errand is done in someone's own accounts, and they "
                    "need a Telegram chat for it): say so, and start nothing")  # fmt: skip
        goal = str(args.get("errand", "")).strip() if isinstance(args, dict) else ""
        if len(goal) < 8:
            return "error: say the errand in full: the site, what, how many, and any limit"
        if not request.go_ahead():  # a draft thrown away, or a turn withdrawn: nothing is queued
            return "error: the request was withdrawn before anything was done; nothing was started"
        person = self._people[person_key(person)]
        ahead = self.queue.ahead()
        self.queue.add(person, goal, self.via)
        if ahead:
            whose = ahead[0]["person"]
            return (f"queued: the browser is on {whose}'s errand first ({ahead[0]['goal'][:100]}); say this one "
                    "comes after it")  # fmt: skip
        where = "comes to their phone, and is said here too" if self.via == VOICE else "comes to this chat"
        return f"started: say you're on it, in a few words; the answer {where} when it's done"


class Chats(Protocol):
    """How an errand reaches the person it's for: their Telegram chat."""

    def tell(self, person: str, text: str, picture: bytes | None = None) -> None: ...

    def approve(self, person: str, draft: str, picture: bytes | None) -> bool: ...

    def login(self, person: str, site: str, goal: str, fields: list[Field]) -> dict[str, str] | None: ...

    def code(self, person: str, site: str, answer: Answer) -> str | None: ...


@dataclass
class ByChat:
    """The person an errand is for, as run_task needs them (its approver, its login.Person), through their chat."""

    name: str
    chats: Chats
    approved: bool = False  # they said yes to a commit: what the errand did can't then be said to be nothing

    def approve(self, draft: str, picture: bytes | None = None) -> bool:
        yes = self.chats.approve(self.name, draft, picture)
        self.approved = self.approved or yes
        return yes

    def login(self, site: str, goal: str, fields: list[Field]) -> dict[str, str] | None:
        return self.chats.login(self.name, site, goal, fields)

    def code(self, site: str, answer: Answer) -> str | None:
        return self.chats.code(self.name, site, answer)


def profile_for(person: str, first: str) -> Path:
    """`person`'s own browser profile: the one TARS's browser always had for the household's first, a folder of
    their own for anyone else."""
    from .chrome import PROFILE

    if person_key(person) == person_key(first):
        return PROFILE
    return PROFILE.with_name(f"profile-{_slug(person)}")


def _slug(person: str) -> str:
    """A folder or file name for `person`, the same for every spelling TARS takes for theirs, and unlike anyone
    else's whatever the alphabet: דנה and יעל aren't one person."""
    return hashlib.sha256(person_key(person).encode()).hexdigest()[:12]


class Errands:
    """The Telegram service's errand runner: takes the queue's errands in turn, runs each in the browser, and tells
    the person how it went."""

    def __init__(
        self,
        cfg: BrowserConfig,
        chats: Chats | None,
        people: list[str],
        queue: Queue | None = None,
        say_aloud: Callable[[str, str], None] | None = None,
        run: Callable[..., dict[str, Any]] | None = None,
        folder: Path = Path(ERRANDS),
        openai=None,
        home: frozenset[str] = frozenset(),
    ):
        """`chats`: where an errand reaches the person (set once the chats are made, if not yet). `people`: who may
        ask, the household's first first. `say_aloud(person, text)`: has TARS say an errand's answer at home, for one
        asked aloud. `run`: run.run_task (the tests pass their own). `openai`: TARS's OpenAI client, for the browser's
        model."""
        self._cfg, self.chats, self._people, self._folder = cfg, chats, people, folder
        self.queue = queue or Queue()
        self._say_aloud, self._run, self._openai, self._home = say_aloud, run, openai, home
        self.current: dict | None = None  # the errand running
        self.thread: threading.Thread | None = None

    def tool(self, via: str = TELEGRAM) -> BrowseTool:
        """The browse tool for a brain that asks for errands this way."""
        return BrowseTool(self.queue, self._people, via)

    def serve(self, every_s: float = LOOK_EVERY_S) -> None:
        """Takes the queue's errands in turn, for as long as the service runs (a thread of its own)."""
        if left := self.queue.left_running():
            print(f"(browser: {left} errand(s) left running when TARS last stopped are marked failed)")

        def loop() -> None:
            while True:
                try:
                    self.next()
                except Exception as e:  # noqa: BLE001 - the queue unreadable for now: tried again
                    print(f"(browser: couldn't read the errands: {e!r})")
                time.sleep(every_s)

        threading.Thread(target=loop, daemon=True, name="errands").start()

    def next(self) -> bool:
        """Starts the next errand, if none is running; whether one was started."""
        if self.current is not None or (row := self.queue.claim()) is None:
            return False
        self.current = row
        self.thread = threading.Thread(target=self._errand, args=(row,), daemon=True, name="errand")
        self.thread.start()
        return True

    def _errand(self, row: dict) -> None:
        from .agent import Settings

        person, goal, started = row["person"], row["goal"], time.time()
        if row["asked_via"] == VOICE:  # the phone hears of it first, since that's where it'll be asked things
            self._tell(person, f"On it, as you asked at home: {goal}")
        trace = self._folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(person)}.jsonl"
        settings = Settings(model=self._cfg.model, effort=self._cfg.effort, turns=self._cfg.turns,
                            really_commit=self._cfg.really_order)  # fmt: skip
        you, result = ByChat(person, self.chats), {}
        try:
            run = self._run or _run_task()
            result = run(goal, you, trace_to=trace, settings=settings, person=you, openai=self._openai, home=self._home,
                         profile=profile_for(person, self._people[0]), notes=_notes(self._folder.parent))  # fmt: skip
            text, status = _told(result), DONE if result.get("ended") == "done" else FAILED
        except Exception as e:  # noqa: BLE001 - an errand that broke is said so, never left hanging
            traceback.print_exc()
            text, status = f"The errand broke before it was done ({type(e).__name__}), so I stopped.", FAILED
            if you.approved and self._cfg.really_order:
                text += " You'd approved the order, so check on the site whether it went through."
            else:
                text += " Nothing was ordered."
        finally:
            self.current = None
        print(f"(browser: {person}'s errand took {time.time() - started:.0f} s: {status})", flush=True)
        try:
            self.queue.finish(row["id"], status, text)
        except Exception as e:  # noqa: BLE001 - only the record of it
            print(f"(browser: couldn't record errand {row['id']}: {e!r})")
        self._tell(person, text)
        if row["asked_via"] == VOICE and self._say_aloud:
            aloud = (result.get("aloud") or "").strip() or _first_sentences(text)
            try:
                self._say_aloud(person, aloud)  # cut to a reminder's length there
            except Exception as e:  # noqa: BLE001 - it's in their chat all the same
                print(f"(browser: couldn't have {person}'s errand said aloud: {e!r})")

    def _tell(self, person: str, text: str) -> None:
        try:
            self.chats.tell(person, text)
        except Exception as e:  # noqa: BLE001 - the chat unreachable: it's in the log and the trace
            print(f"(browser: couldn't tell {person} about the errand: {e!r})")


def _run_task():
    from .run import run_task

    return run_task


def _notes(folder: Path):
    from .notes import Notes

    return Notes(folder / "notes")


def _told(result: dict[str, Any]) -> str:
    """What the person is told: the agent's answer, or why there's none."""
    answer = (result.get("answer") or "").strip()
    nothing = "" if result.get("commits_asked") else " Nothing was ordered."
    if result.get("ended") == "error":
        return "The errand broke before it was done, so I stopped." + (
            f" Where it got to: {answer}" if answer else nothing
        )
    if answer:
        return answer
    if result.get("ended") == "out of turns":
        return "That took more steps than I allow an errand, so I stopped." + nothing
    return "I stopped without an answer." + nothing


def _first_sentences(text: str, most: int = 2) -> str:
    """The start of a written answer, as TARS says text aloud (speech.py: no markdown or links), for when the
    agent gave no spoken one."""
    from ..speech import split_sentences

    return " ".join(list(split_sentences([text]))[:most])
