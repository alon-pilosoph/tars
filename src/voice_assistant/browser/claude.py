"""TARS's browser worked by Claude Code instead of TARS's own loop: `claude -p` on the person's own Claude
subscription, with TARS's browser (mcp_server.py) as its only tools and none of its own. It's the same view, gate and
approval as TARS's agent; only the brain is different, so the two can be compared on the same tasks.

Claude Code is started with no built-in tools (`--tools ""`), only TARS's browser allowed, anything else refused
(`--permission-mode dontAsk`), its own system prompt replaced by TARS's browser instructions, and no session kept.
What it did comes back two ways: Claude Code's stream (time, turns, tokens, what the same use would cost on the API)
and the MCP server's trace (each call and what it did, the answer, the approvals asked)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .agent import Settings, instructions_for

MODEL = "claude-opus-5-5"
SERVER = "tars-browser"
ROOT = Path(__file__).resolve().parents[3]
PREFACE = ("Your tools are TARS's browser (page, act, find, look, request_approval, done) and nothing else. Start "
           "with page, and finish with done.\n\n")  # fmt: skip


def run_claude(goal: str, trace_to: Path, model: str = MODEL, effort: str = "medium", turns: int = 30,
               timeout_s: float = 900, tidy: bool = False) -> dict[str, Any]:  # fmt: skip
    """Runs Claude Code on `goal` with TARS's browser; what it did, in run_task's shape. `tidy`: then empties, with
    TARS's own agent, a basket the run may have filled (as run_task does for evals)."""
    trace_to.parent.mkdir(parents=True, exist_ok=True)
    config = trace_to.with_suffix(".mcp.json")
    env = {"TARS_BROWSER_TRACE": str(trace_to), "TARS_BROWSER_TURNS": str(turns)}
    config.write_text(json.dumps({"mcpServers": {SERVER: {
        "command": sys.executable, "args": ["-m", "voice_assistant.browser.mcp_server"], "env": env}}}))  # fmt: skip
    command = [
        "claude", "-p", f"The person's goal: {goal}", "--model", model, "--effort", effort,
        "--mcp-config", str(config), "--strict-mcp-config", "--tools", "", "--allowedTools", f"mcp__{SERVER}",
        "--permission-mode", "dontAsk", "--system-prompt", PREFACE + instructions_for(goal),
        "--output-format", "stream-json", "--verbose", "--no-session-persistence",
    ]  # fmt: skip
    _log(trace_to, {"event": "start", "goal": goal, "model": model, "effort": effort})
    started = time.perf_counter()
    error = ""
    try:
        ran = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout_s,
                             env={**os.environ, **env}, check=False)  # fmt: skip
        stream, error = ran.stdout, ran.stderr.strip()[-500:] if ran.returncode else ""
    except subprocess.TimeoutExpired as e:
        stream, error = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), "timed out"
    seconds = time.perf_counter() - started
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
        result |= _tidy(calls, result["commits_asked"], trace_to)
    return result


def _tidy(calls: list[dict[str, Any]], commits_asked: list[str], trace_to: Path) -> dict[str, Any]:
    from .run import _filled, _NoOne, run_task, tidy_goal

    if not (commits_asked or _filled(calls)):
        return {}
    _log(trace_to, {"event": "follow_up", "request": "tidy"})
    tidied = run_task(tidy_goal(calls), _NoOne(), trace_to=trace_to, settings=Settings(turns=20))
    return {"tidied": tidied["answer"], "tidy_tokens_in": tidied["tokens_in"], "tidy_tokens_out": tidied["tokens_out"]}


def _json(line: str) -> dict[str, Any]:
    try:
        found = json.loads(line)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _log(trace: Path, entry: dict[str, Any]) -> None:
    with trace.open("a") as out:
        out.write(json.dumps({"t": round(time.time(), 2), **entry}, ensure_ascii=False, default=str) + "\n")
