"""TARS's errands in its browser, asked for in a Telegram chat: the brain's browse tool starts one, and it runs on its
own while the chat goes on. What it needs from the person on the way comes to their chat (Chat, from telegram.py): a
yes before anything is ordered, a login TARS lacks (a link to a one-time page), a code a site sent them. When it's
done, its answer comes to the chat too.

One errand at a time: there's one browser, and a site's basket is one. Each person has a browser profile of their
own, so one person's sign-in never becomes another's (the household's first keeps the profile TARS had before)."""

import re
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
    from .login import Answer
    from .login_page import Field

ERRANDS = Path(__file__).resolve().parents[3] / "voice_data/browser/errands"

BROWSE_TOOL = {
    "type": "function",
    "name": "browse",
    "description": "Do an errand on a website in TARS's own browser: order or book something, or find something on a "
    "site (a menu, a price, an order's status, an account's details). It runs on its own for a few minutes; its "
    "answer comes to this chat when it's done.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["errand"],
        "properties": {
            "errand": {
                "type": "string",
                "description": "The errand in full, as the person asked it, with the site, what, how many, when, "
                "and any limit they gave: 'On Wolt, order a large margherita from the nearest pizza place, under 80 "
                "shekels'. Everything said about it in the conversation goes in: the browser hears nothing else.",
            }
        },
    },
}
BROWSE_RULES = (
    "You can do errands on websites with the browse tool: order food on Wolt, book a table, find something on a "
    "site or in someone's account there. It works a real browser signed in to the person's accounts, and takes a few "
    "minutes; say you're on it in one short line, and nothing more until it's done: its answer comes to this chat by "
    "itself. Before anything is ordered, booked or paid for, the person is asked here to approve it, with what and "
    "the total; never say something is ordered until they've approved it and the browser says it's done. Use the "
    "web search for a quick question that needs no site of their own. One errand runs at a time."
)
CAN_BROWSE = (
    "do errands on websites in TARS's own browser (ordering, booking, looking something up on a site), with the "
    "browse tool"
)
NEEDS_BROWSING = "an errand on a website: ordering or booking something, or finding something in an account on a site"


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

    def approve(self, draft: str, picture: bytes | None = None) -> bool:
        return self.chats.approve(self.name, draft, picture)

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
    return PROFILE.with_name("profile-" + re.sub(r"[^a-z0-9]+", "-", person_key(person)).strip("-"))


class Errands:
    """The browse tool: starts an errand for whoever asked, and tells their chat how it went. The brain (llm.py)
    gets its tool, its rules, what it says TARS can do, and what a quick model hands over for it."""

    tool, rules, can, needs = BROWSE_TOOL, BROWSE_RULES, CAN_BROWSE, NEEDS_BROWSING

    def __init__(
        self,
        cfg: BrowserConfig,
        chats: Chats | None,
        people: list[str],
        run: Callable[..., dict[str, Any]] | None = None,
        folder: Path = ERRANDS,
    ):
        """`chats`: where an errand reaches the person (set once the chats are made, if not yet). `people`: who may
        ask, the household's first first. `run`: run.run_task (the tests pass their own)."""
        self._cfg, self.chats, self._people, self._folder = cfg, chats, people, folder
        self._run = run
        self._lock = threading.Lock()
        self.current: tuple[str, str] | None = None  # (whose, what) while one runs
        self.thread: threading.Thread | None = None

    def call(self, args: dict, request) -> str:
        """The brain's browse call, for whoever `request` says is asking."""
        person = getattr(request, "speaker", None)
        known = {person_key(p): p for p in self._people}
        if not person or person_key(person) not in known:
            return "error: TARS doesn't know who's asking, and an errand is done in someone's own accounts"
        goal = str(args.get("errand", "")).strip() if isinstance(args, dict) else ""
        if len(goal) < 8:
            return "error: say the errand in full: the site, what, how many, and any limit"
        person = known[person_key(person)]
        with self._lock:
            if self.current:
                whose, what = self.current
                return f"busy: the browser is on {whose}'s errand ({what[:120]}); say it'll have to wait for that"
            self.current = (person, goal)
        self.thread = threading.Thread(target=self._errand, args=(person, goal), daemon=True, name="errand")
        self.thread.start()
        return "started: say you're on it, in a few words; the answer comes to this chat when it's done"

    def _errand(self, person: str, goal: str) -> None:
        from .agent import Settings

        started = time.time()
        trace = self._folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^a-z0-9]+', '-', person.lower())}.jsonl"
        settings = Settings(model=self._cfg.model, effort=self._cfg.effort, turns=self._cfg.turns,
                            really_commit=self._cfg.really_order)  # fmt: skip
        you = ByChat(person, self.chats)
        try:
            run = self._run or _run_task()
            result = run(goal, you, trace_to=trace, settings=settings, person=you,
                         profile=profile_for(person, self._people[0]), notes=_notes())  # fmt: skip
            text = _told(result)
        except Exception as e:  # noqa: BLE001 - an errand that broke is said so, never left hanging
            traceback.print_exc()
            text = f"The errand broke before it was done ({type(e).__name__}), so I stopped. Nothing was ordered."
        finally:
            with self._lock:
                self.current = None
        print(f"(browser: {person}'s errand took {time.time() - started:.0f} s)", flush=True)
        try:
            self.chats.tell(person, text)
        except Exception as e:  # noqa: BLE001 - the chat unreachable: it's in the log and the trace
            print(f"(browser: couldn't tell {person} how the errand went: {e!r})")


def _run_task():
    from .run import run_task

    return run_task


def _notes():
    from .notes import Notes

    return Notes()


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
