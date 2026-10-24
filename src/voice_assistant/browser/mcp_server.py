"""TARS's browser as MCP tools, so another agent can be its brain: Claude Code, say, on the person's own Claude
subscription (`claude -p` with this as its only MCP server and no tools of its own; run.py's run_claude).

The tools are TARS's agent's own (tools.py): act, find, look, request_approval, sign_in and done, with the same
view of the page, the same gate on every click and the same approval, so what keeps an order from being placed
doesn't depend on which model is asking. Each answer brings the page as it is after the call, when it changed.

It owns TARS's Chrome for as long as it runs. Playwright's sync API runs on one thread of its own, since the MCP
server's event loop runs on another. How it runs comes from the environment, as Claude Code starts it:

    TARS_BROWSER_TRACE   a trace file (tools.py's turns, as agent.py writes them)
    TARS_BROWSER_TURNS   at most this many calls (default 30); after that, only done
    TARS_BROWSER_COMMIT  "really" to press an approved commit; anything else is a dry run, approved and not pressed

There's no one to ask from here, so a commit is approved only as a dry run: with "really", it's refused. For the
same reason sign_in uses only the logins in the vault (the household's first person's), and has no code to give."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import login
from .agent import STEP, TOOLS
from .chrome import PROFILE, browser_session
from .tools import Tools
from .trace import Trace
from .vault import Vault, household_person

ROOT = Path(__file__).resolve().parents[3]
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
        session = self.stack.enter_context(browser_session(PROFILE))
        person, vault = login.Unattended(household_person(ROOT / "config.toml")), Vault()
        self.tools = Tools(session, DryRun(), really_commit=REALLY, log=log,
                           sign_in=lambda s: login.sign_in(s.page, vault, person, hide=s.secrets.add))  # fmt: skip
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
                 "answer": str(args.get("answer", "")), "aloud": str(args.get("aloud", "")), "turns": self.turns,
                 "screenshots": tools.screenshots, "commits_asked": tools.commits_asked})  # fmt: skip
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


log = Trace(TRACE)


browser = Browser()
server = MCPServer("tars-browser")


NOTE = " `note`: a few words for the person's progress line, saying what this call is for."


def _described(name: str, extra: str = "") -> str:
    """The tool's description as TARS's own agent has it (agent.TOOLS), so the two can't drift apart."""
    return next(t["description"] for t in TOOLS if t["name"] == name) + extra


@server.tool(description="The page as it is now: its title and address, its text, and everything on it to click or "
             "type into, each with a ref ([e36 button \"Add to order\"]). Call it first.")  # fmt: skip
def page() -> list[str | Image]:
    return browser.call("page", {})


@server.tool(description=_described("act", f" Each step is an object of this JSON schema: {json.dumps(STEP)}.{NOTE}"))
def act(steps: list[dict[str, Any]], note: str = "") -> list[str | Image]:
    return browser.call("act", {"steps": steps, "note": note})


@server.tool(description=_described("find", NOTE))
def find(text: str, note: str = "") -> list[str | Image]:
    return browser.call("find", {"text": text, "note": note})


@server.tool(description=_described("look", NOTE))
def look(note: str = "") -> list[str | Image]:
    return browser.call("look", {"note": note})


@server.tool(description=_described("request_approval"))
def request_approval(ref: str, summary: str) -> list[str | Image]:
    return browser.call("request_approval", {"ref": ref, "summary": summary})


@server.tool(description=_described("sign_in", NOTE))
def sign_in(note: str = "") -> list[str | Image]:
    return browser.call("sign_in", {"note": note})


@server.tool(description=_described("done", " `aloud`: the answer as said aloud, in one or two short sentences."))
def done(success: bool, answer: str, aloud: str = "") -> list[str | Image]:
    return browser.call("done", {"success": success, "answer": answer, "aloud": aloud})


def main() -> None:
    try:
        server.run("stdio")
    finally:
        browser.close()


if __name__ == "__main__":
    main()
