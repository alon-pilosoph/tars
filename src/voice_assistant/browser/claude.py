"""TARS's browser worked by Claude Code instead of TARS's own loop: `claude -p` on the person's own Claude
subscription, with TARS's browser (mcp_server.py) as its only tools and none of its own. It's the same view, gate and
approval as TARS's agent; only the brain is different, so the two can be compared on the same tasks.

Claude Code is started with no built-in tools (`--tools ""`), only TARS's browser allowed, anything else refused
(`--permission-mode dontAsk`), its own system prompt replaced by TARS's browser instructions, and no session kept.
It runs in a folder of its own with only that folder's settings (no plugins or hooks of the person's) and
auto-memory off: its CLAUDE.md there holds TARS's browsing rules (agent.RULES) and, with notes, how to keep notes on
sites in notes/, the only files it may read or write. It's still Claude Code's loop: tool calls, the whole
history kept (TARS's own loop shrinks old pages to a line), prompt caching, and compacting when it's long.
What it did comes back two ways: Claude Code's stream (time, turns, tokens, what the same use would cost on the API)
and the MCP server's trace (each call and what it did, the answer, the approvals asked)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .agent import RULES, header_for
from .chrome import close as close_chrome

MODEL = "claude-opus-5-5"
SERVER = "tars-browser"
ROOT = Path(__file__).resolve().parents[3]
# Where Claude Code runs: a folder of its own, so only its own CLAUDE.md and notes come along, no other project's
# CLAUDE.md or memory (just the account's email and the machine's environment, which a plan sign-in brings anyway).
HOME = ROOT / "voice_data/browser/claude-home"
SYSTEM = "You are TARS's browser agent. How to work is in CLAUDE.md; the person's goal is in their message."
# Claude Code's CLAUDE.md in its folder: the same rules TARS's own agent follows, and how to keep notes on sites.
CLAUDE_MD = """# TARS's browser

You work a real web browser for the person through TARS's browser tools (page, act, find, look, request_approval,
sign_in, done). Start with page, and finish with done.

## Working the browser

{rules}
"""
NOTES_MD = """
## Notes on sites

TARS keeps notes on the sites it works, one file a site in notes/ (notes/wolt.com.md): what earlier runs learned
about how the site works. The addresses that go straight to something, what things are called and where they are,
what failed and what worked instead, what the site asks for on the way to checkout.

- When the task takes you to a site that has a notes file, Read it before you start there. It's a hint: where the
  page shows otherwise, the page is right.
- Before you call done, write what this run taught you into the notes file of the site you worked most (Edit it,
  or Write it if there's none yet): at most 12 short bullets in all, keeping what still holds. Only how the site
  works, never how this run went (not what you found, couldn't find or gave up on), and nothing about the person
  (no names, addresses, orders, accounts).
- The notes are the only files you can read or write.
"""
NOTES = HOME / "notes"
# For studying how it works: a progress note with each call, kept in the trace and the stream. Framed as what each
# step is for, for the person to follow: asked to set out its reasoning, Opus 5.5 declines (reasoning_extraction).
NARRATE = ("The person follows your progress: give every act, find and look call a note, a few words saying what the "
           "call is for (\"Opening the cart\", \"Checking the dates took\"). After a step that didn't work, the next "
           "note says what you're trying instead.")  # fmt: skip


def run_claude(goal: str, trace_to: Path, model: str = MODEL, effort: str = "medium", turns: int = 30,
               timeout_s: float = 900, tidy: bool = False, notes: bool = False,
               narrate: bool = False) -> dict[str, Any]:  # fmt: skip
    """Runs Claude Code on `goal` with TARS's browser; what it did, in run_task's shape. `tidy`: then empties a
    basket the run may have filled (as run_task does for evals). `notes`: it reads the notes on the sites it goes to,
    and writes what the run taught it (in notes/, the only files it can touch). `narrate`: it writes a short note
    before each step, for studying how it works (kept with the rest of its stream, beside the trace)."""
    trace_to.parent.mkdir(parents=True, exist_ok=True)
    config = trace_to.with_suffix(".mcp.json")
    env = {"TARS_BROWSER_TRACE": str(trace_to), "TARS_BROWSER_TURNS": str(turns)}
    config.write_text(json.dumps({"mcpServers": {SERVER: {
        "command": sys.executable, "args": ["-m", "voice_assistant.browser.mcp_server"], "env": env}}}))  # fmt: skip
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "CLAUDE.md").write_text(CLAUDE_MD.format(rules=RULES) + (NOTES_MD if notes else ""))
    prompt = header_for(goal)
    files, allowed = "", [f"mcp__{SERVER}"]
    if notes:
        NOTES.mkdir(exist_ok=True)
        known = sorted(p.stem for p in NOTES.glob("*.md"))
        prompt += f"Notes so far: {', '.join(f'notes/{k}.md' for k in known)}." if known else "No notes yet."
        files, allowed = "Read,Edit,Write", [*allowed, *(f"{t}(./notes/**)" for t in ("Read", "Edit", "Write"))]
    if narrate:
        prompt += "\n\n" + NARRATE
    command = [
        "claude", "-p", prompt, "--model", model, "--effort", effort,
        "--mcp-config", str(config), "--strict-mcp-config", "--tools", files, "--allowedTools", *allowed,
        "--permission-mode", "dontAsk", "--system-prompt", SYSTEM,
        "--output-format", "stream-json", "--verbose", "--no-session-persistence",
        "--setting-sources", "project", "--settings", json.dumps({"autoMemoryEnabled": False}),
    ]  # fmt: skip
    _log(trace_to, {"event": "start", "goal": goal, "model": model, "effort": effort})
    started = time.perf_counter()
    error = ""
    try:
        ran = subprocess.run(command, cwd=HOME, capture_output=True, text=True, timeout=timeout_s,
                             env={**os.environ, **env}, check=False)  # fmt: skip
        stream, error = ran.stdout, ran.stderr.strip()[-500:] if ran.returncode else ""
    except subprocess.TimeoutExpired as e:
        stream, error = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), "timed out"
    seconds = time.perf_counter() - started
    close_chrome()  # Claude Code stops its MCP server without letting it shut the browser
    trace_to.with_suffix(".stream.jsonl").write_text(stream)  # every message and call, as Claude Code streamed them
    final = next((m for m in map(_json, reversed(stream.splitlines())) if m.get("type") == "result"), {})
    events = [_json(line) for line in trace_to.read_text().split("\n") if line] if trace_to.exists() else []
    calls = [e for e in events if e.get("event") == "turn"]
    end = next((e for e in events if e.get("event") == "end"), None)
    usage = final.get("usage", {})
    tokens_in = sum(usage.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens",
                                              "cache_creation_input_tokens"))  # fmt: skip
    browser_s = sum(e.get("browser_s", 0) for e in calls)
    result = {
        "turns": len(calls),
        "seconds": round(seconds, 1),
        "model_s": round(seconds - browser_s, 1),
        "browser_s": round(browser_s, 1),
        "tokens_in": tokens_in,
        "tokens_cached": usage.get("cache_read_input_tokens", 0),
        "tokens_out": usage.get("output_tokens", 0),
        "api_usd": final.get("total_cost_usd", 0.0),  # what the same use would cost on the API; on a plan, $0
        "screenshots": end.get("screenshots", 0) if end else 0,
        "model": model,
        "ended": "done" if end else ("error" if error else "no answer"),
        "claimed": bool(end and end.get("success")),
        "answer": (end or {}).get("answer") or final.get("result", ""),
        "commits_asked": end.get("commits_asked", []) if end else [],
        "error": error or (final.get("result", "") if final.get("is_error") else ""),
    }
    if tidy:
        result |= _tidy(calls, result["commits_asked"], trace_to, model, effort)
    return result


def _tidy(calls: list[dict[str, Any]], commits_asked: list[str], trace_to: Path, model: str,
          effort: str) -> dict[str, Any]:  # fmt: skip
    """Empties the basket the run may have filled, by Claude too (TARS's own agent took twice as long to find it)."""
    from .run import _filled, tidy_goal

    if not (commits_asked or _filled(calls)):
        return {}
    _log(trace_to, {"event": "follow_up", "request": "tidy"})
    tidied = run_claude(tidy_goal(calls), trace_to.with_suffix(".tidy.jsonl"), model=model, effort=effort, turns=25)
    return {"tidied": tidied["answer"], "tidy_api_usd": tidied["api_usd"]}


def _json(line: str) -> dict[str, Any]:
    try:
        found = json.loads(line)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _log(trace: Path, entry: dict[str, Any]) -> None:
    with trace.open("a") as out:
        out.write(json.dumps({"t": round(time.time(), 2), **entry}, ensure_ascii=False, default=str) + "\n")
