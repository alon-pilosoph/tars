"""TARS's browser as MCP tools, so another agent can be its brain: Claude Code, say, on the person's own Claude
subscription (`claude -p` with this as its only MCP server and no tools of its own; run.py's run_claude).

The tools are TARS's agent's own (tools.py): act, find, look, request_approval and done, with the same view of the
page, the same gate on every click and the same approval, so what keeps an order from being placed doesn't depend on
which model is asking. Each answer brings the page as it is after the call, when it changed.

It owns TARS's Chrome for as long as it runs. Playwright's sync API runs on one thread of its own, since the MCP
server's event loop runs on another. How it runs comes from the environment, as Claude Code starts it:

    TARS_BROWSER_TRACE   a trace file (tools.py's turns, as agent.py writes them)
    TARS_BROWSER_TURNS   at most this many calls (default 30); after that, only done
    TARS_BROWSER_COMMIT  "really" to press an approved commit; anything else is a dry run, approved and not pressed

There's no one to ask from here, so a commit is approved only as a dry run: with "really", it's refused."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from .chrome import PROFILE, own_chrome
from .session import Session
from .tools import Tools

TURNS = int(os.environ.get("TARS_BROWSER_TURNS", "30"))
TRACE = Path(os.environ["TARS_BROWSER_TRACE"]) if os.environ.get("TARS_BROWSER_TRACE") else None
REALLY = os.environ.get("TARS_BROWSER_COMMIT") == "really"


class DryRun:
    """The person, when nobody can be asked: a dry run is approved (nothing is pressed), a real commit isn't."""

    def approve(self, draft: str, picture: bytes | None = None) -> bool:
        return not REALLY


class Browser:
    """TARS's Chrome and its tools, all on one thread (Playwright's sync API stays on the thread that started it)."""

    def __init__(self) -> None:
        self.thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")
        self.stack = ExitStack()
        self.tools: Tools | None = None
        self.turns = 0

    def _start(self) -> Tools:
        from playwright.sync_api import sync_playwright

        address = self.stack.enter_context(own_chrome(PROFILE))
        playwright = self.stack.enter_context(sync_playwright())
        session = Session(playwright.chromium.connect_over_cdp(address).contexts[0])
        self.tools = Tools(session, DryRun(), really_commit=REALLY, log=log)
        return self.tools

    def _call(self, name: str, args: dict[str, Any]) -> list[str | Image]:
        tools = self.tools or self._start()
        self.turns += 1
        if self.turns > TURNS and name != "done":
            return [f"out of turns ({TURNS}): call done now, saying where you got to"]
        started = time.perf_counter()
        if name == "page":
            view = tools.session.look()
            said, picture, changed = "", None, True
        else:
            did = tools.call(name, args)
            said, picture, changed = did.said, did.picture, did.changed
        view = tools.session.current
        log({"event": "turn", "turn": self.turns, "call": name, "args": args, "said": said,
             "url": tools.session.page.url, "browser_s": round(time.perf_counter() - started, 2),
             "network": tools.session.take_network()})  # fmt: skip
        if name == "done":
            log({"event": "end", "ended": "done", "success": bool(args.get("success")),
                 "answer": str(args.get("answer", "")), "turns": self.turns, "screenshots": tools.screenshots,
                 "commits_asked": tools.commits_asked})  # fmt: skip
            return ["ok: that's the answer. Stop now."]
        out: list[str | Image] = [said] if said else []
        if changed and view is not None:
            empty = view.url == "about:blank"
            out.append("A new, empty tab: start by going to an address." if empty else view.text)
        if picture:
            out.append(Image(data=picture, format="jpeg"))
        return out or ["(nothing to say)"]

    def call(self, name: str, args: dict[str, Any]) -> list[str | Image]:
        return self.thread.submit(self._call, name, args).result()

    def close(self) -> None:
        self.thread.submit(self.stack.close).result()
        self.thread.shutdown()


def log(entry: dict[str, Any]) -> None:
    if TRACE is None:
        return
    TRACE.parent.mkdir(parents=True, exist_ok=True)
    with TRACE.open("a") as out:
        out.write(json.dumps({"t": round(time.time(), 2), **entry}, ensure_ascii=False, default=str) + "\n")


browser = Browser()
server = MCPServer("tars-browser")


@server.tool()
def page() -> list[str | Image]:
    """The page as it is now: its title and address, its headings and text, and everything on it to click or type
    into, each with a ref ([e36 button "Add to order"]). Call it first."""
    return browser.call("page", {})


@server.tool()
def act(steps: list[dict[str, Any]], note: str = "") -> list[str | Image]:
    """Steps to take in order on the page, then the page as it is after them. Each step is an object with an
    action: {"action": "click", "ref": "e36"}; {"action": "click", "text": "20:00", "near": "other text on the same
    card"} for text with no ref; {"action": "type", "ref": "e46", "text": "...", "submit": true, "slowly": false};
    {"action": "select", "ref": "e12", "option": "label"}; {"action": "press", "key": "Escape"};
    {"action": "scroll", "direction": "down"} (with "ref" to scroll inside a panel), or {"action": "scroll",
    "ref": "e88"} to bring it into view; {"action": "hover", "ref": "e5"}; {"action": "goto", "url": "https://..."};
    {"action": "back"}; {"action": "wait", "seconds": 2}; {"action": "click_at", "x": 640, "y": 300} on the
    screenshot, the last resort. A step that opens something ends the batch. `note`: a few words for the person's
    progress line, saying what these steps are for ("Opening the cart"), or, after one that didn't work, what you're
    trying instead."""
    return browser.call("act", {"steps": steps, "note": note})


@server.tool()
def find(text: str, note: str = "") -> list[str | Image]:
    """Lines anywhere on the page with all of these words in them (any case): quicker than scrolling. `note`: a few
    words for the person's progress line, saying what you're looking for and why."""
    return browser.call("find", {"text": text, "note": note})


@server.tool()
def look(note: str = "") -> list[str | Image]:
    """A screenshot of the screen as it is, with the refs drawn on it. `note`: a few words for the person's progress
    line, saying what you want to check."""
    return browser.call("look", {"note": note})


@server.tool()
def request_approval(ref: str, summary: str) -> list[str | Image]:
    """At the last step: ask the person to approve pressing this button (it places the order, pays, books or sends),
    with what it commits them to (what, how many, when, the total). TARS presses it only if they say yes."""
    return browser.call("request_approval", {"ref": ref, "summary": summary})


@server.tool()
def done(success: bool, answer: str) -> list[str | Image]:
    """Finish, with the answer for the person."""
    return browser.call("done", {"success": success, "answer": answer})


def main() -> None:
    try:
        server.run("stdio")
    finally:
        browser.close()


if __name__ == "__main__":
    main()
