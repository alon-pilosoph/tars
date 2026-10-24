"""What a model can do with TARS's browser, whichever model it is: the calls (act, find, look, request_approval,
sign_in, done), carried out on a session with everything that keeps them safe and keeps a model from going round in
circles. TARS's own agent (agent.py) calls them in its loop; another agent (Claude Code) gets them as MCP tools
(mcp_server.py). Either way it's the same gate, the same approval and the same view of the page.

- A step that already failed on a page isn't tried again there, and the same steps three times running aren't done a
  fourth time; both come back with a screenshot, which is what a model reading text needs then.
- A commit is never the model's to make: request_approval asks the person, from what the page says (not what the
  model says), and code presses the button only on a yes, with the total and every price as they were."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from . import page as pages
from .gate import commits
from .session import Session

REPEATS = 3  # the same steps this many times in a row on one page: not done again
MONEY = re.compile(r"(?:[₪$€£]\s?\d[\d,]*(?:\.\d{1,2})?|\d[\d,]*(?:\.\d{1,2})?\s?(?:₪|ILS|NIS|EUR|USD|€|\$))")
# What a button that only takes the person on says ("Go to checkout", "View basket"): no approval needed for it.
NAVIGATES = re.compile(
    r"\b(go to|view|open|show|continue to)\s+(the\s+)?(checkout|cart|basket|bag)\b"
    r"|^\s*\d*\s*(checkout|cart|basket)\b|show items|לסל|לקופה|מעבר לקופה|לסל הקניות|לעגלת|לעגלה",
    re.IGNORECASE,
)
TOTAL_WORDS = re.compile(r"total|סה\"?״?כ|סך הכל|לתשלום|to pay|price for", re.IGNORECASE)


class Approver(Protocol):
    """The person, asked to approve a commit: True only for a yes."""

    def approve(self, draft: str, picture: bytes | None = None) -> bool: ...


@dataclass
class Did:
    """What a call did: what to tell the model, a screenshot to show it, whether the page is new to it, and whether
    something went wrong (the model thinks harder on the next turn)."""

    said: str
    picture: bytes | None = None
    changed: bool = False
    trouble: bool = False


@dataclass
class Tools:
    session: Session
    approver: Approver
    really_commit: bool = False  # False: an approved commit is a dry run, and nothing is pressed
    sign_in: Callable[[Session], str] | None = None
    log: Callable[[dict[str, Any]], None] = lambda entry: None
    screenshots: int = 0
    commits_asked: list[str] = field(default_factory=list)
    committed: bool = False
    signed_in: bool = False
    failed_steps: set[str] = field(default_factory=set)
    last_act: str = ""
    repeats: int = 0

    def call(self, name: str, args: dict[str, Any]) -> Did:
        """Carries out one of the model's calls."""
        session = self.session
        if name == "act":
            return self.act([s for s in args.get("steps", []) if isinstance(s, dict)])
        if name == "find":
            view = session.current or session.look()
            return Did(view.find(str(args.get("text", ""))))
        if name == "look":
            return Did("here is the screen", self.picture())
        if name == "request_approval":
            return Did(self.approval(str(args.get("ref", "")), str(args.get("summary", ""))), changed=True)
        if name == "sign_in":
            if self.signed_in:
                return Did("sign_in already ran for this task: call done and say what stopped it", trouble=True)
            self.signed_in = True
            if self.sign_in is None:
                return Did("TARS has no login for this site: call done and say it needs one", trouble=True)
            said = self.sign_in(session)
            session.network.clear()  # what the sign-in sent (the password, a code) never goes in the trace
            session.look()
            return Did(said, changed=True)
        if name == "done":
            return Did("ok")
        return Did(f"no tool called {name}", trouble=True)

    def act(self, steps: list[dict[str, Any]]) -> Did:
        session = self.session
        if not steps:
            return Did("no steps given", trouble=True)
        repeat = next((s for s in steps if _key(s, session.page.url) in self.failed_steps), None)
        if repeat is not None:
            said = (f"not done: {json.dumps(repeat, ensure_ascii=False)} already failed on this page. Look at the "
                    "screenshot and do something else.")  # fmt: skip
            return Did(said, self.picture(), trouble=True)
        key = _key({"steps": steps}, session.page.url)
        self.repeats = self.repeats + 1 if key == self.last_act else 1
        self.last_act = key
        if self.repeats >= REPEATS:
            said = (f"not done: that's {self.repeats} times in a row, and it didn't get you closer (it may open and "
                    "close something). Look at the screenshot and do something else.")  # fmt: skip
            return Did(said, self.picture(), trouble=True)
        outcome = session.act(steps)
        said = str(outcome)
        if outcome.failed and not outcome.refused:
            done = sum(1 for line in outcome.said if line.startswith("ok:"))  # the steps before it went
            self.failed_steps.add(_key(steps[min(done, len(steps) - 1)], session.page.url))
        if not outcome.changed and not outcome.failed:
            said += "\nnothing changed on the page: that wasn't it. Look at the screenshot before the next step."
            for s in steps:
                self.failed_steps.add(_key(s, session.page.url))
        trouble = outcome.failed or not outcome.changed
        return Did(said, self.picture() if trouble and not outcome.refused else None, outcome.changed, trouble)

    def approval(self, ref: str, summary: str) -> str:
        session = self.session
        view = session.current or session.look()
        node = view.refs.get(ref)
        if node is None:
            return f"{ref} isn't on the page: look again and give the ref of the button that commits"
        if self.committed:
            return "this task already committed once: call done"
        label = " ".join((node.name or pages.text_of(node)).split())
        if NAVIGATES.search(label) and not commits(label):
            return (f"{label!r} only takes you on (to the basket or the checkout): press it yourself, and call "
                    "request_approval at the last button, the one that places the order")  # fmt: skip
        total, prices = _total(view.text), MONEY.findall(view.text)
        host = urlsplit(session.page.url).hostname or session.page.url
        picture = session.screenshot()
        draft = (f"TARS wants to press {label!r} on {host}.\n\n{summary}\n\n"
                 f"The page says the total is {total or '(not shown)'}.")  # fmt: skip
        self.commits_asked.append(draft)
        self.screenshots += 1
        self.log({"event": "approval", "ref": ref, "label": label, "summary": summary, "total": total})
        if not self.approver.approve(draft, picture):
            return "the person said no: nothing was done. Call done and say so."
        if not self.really_commit:
            self.committed = True
            return f"approved, as a dry run: {label!r} was not pressed and nothing was committed. Call done."
        again = session.look().text
        now = _total(again)
        if now != total:
            return f"the total changed from {total} to {now} while waiting: nothing was done. Call done and say so."
        if MONEY.findall(again) != prices:  # a page that doesn't say "total" still says its prices
            return "the prices on the page changed while waiting: nothing was done. Call done and say so."
        session.click_approved(ref)
        self.committed = True
        session.look()
        return f"pressed {label!r}. The page now shows what happened: read it and call done."

    def picture(self) -> bytes | None:
        try:
            shot = self.session.screenshot()
        except Exception:  # noqa: BLE001 - no picture this turn is fine
            return None
        self.screenshots += 1
        return shot


def _key(step: dict[str, Any], url: str) -> str:
    return url + " " + json.dumps(step, sort_keys=True, ensure_ascii=False)


def _total(text: str) -> str | None:
    """The total the page shows: the last amount on or just after a line that says total."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    found = None
    for i, line in enumerate(lines):
        if TOTAL_WORDS.search(line):
            for candidate in (line, *lines[i + 1 : i + 3]):
                if amounts := MONEY.findall(candidate):
                    found = amounts[-1].strip()
                    break
    return found
