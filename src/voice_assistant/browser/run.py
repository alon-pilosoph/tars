"""One task, start to end: TARS's Chrome started, the agent run in it, Chrome shut."""

import traceback
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from ..check import env_value
from .agent import Agent, Approver, Settings
from .chrome import PORT, PROFILE, own_chrome
from .session import Session

ROOT = Path(__file__).resolve().parents[3]
TIDY = (
    "That was a dry run, and it's over. Take out of the basket or cart what you put in it during this task, and "
    "nothing else (leave anything that was there before), then call done saying what you took out."
)


def client(env: Path = ROOT / ".env"):
    from openai import OpenAI

    key = env_value(env, "OPENAI_API_KEY")
    if not key:
        raise RuntimeError(f"no OPENAI_API_KEY in {env}")
    return OpenAI(api_key=key, timeout=90, max_retries=2)


def run_task(
    goal: str,
    approver: Approver,
    steps: int = 30,
    trace_to: Path | None = None,
    settings: Settings | None = None,
    profile: Path = PROFILE,
    port: int = PORT,
    tidy: bool = False,
) -> dict[str, Any]:
    """Runs the agent on `goal` in a fresh TARS Chrome; what it did, as a dict. `tidy`: after a dry-run commit, it's
    asked to take out of the basket what it put in, so the next run starts where this one did (its tokens are counted
    in the cost, and nothing else of it is)."""
    settings = settings or Settings(turns=steps)
    with own_chrome(profile, port) as address, sync_playwright() as p:
        context = p.chromium.connect_over_cdp(address).contexts[0]
        session = Session(context)
        agent = Agent(client(), session, approver, settings, trace=trace_to)
        try:
            result = agent.run(goal)
        except Exception as e:  # noqa: BLE001 - a crash is a result like any other, and the trace says where
            result = agent.result
            result.ended, result.error = "error", f"{type(e).__name__}: {e}"
            agent._log({"event": "error", "error": result.error, "where": traceback.format_exc()[-3000:]})
        tidied = None
        if tidy and result.commits_asked:
            try:
                tidied = agent.follow_up(TIDY, turns=10)
            except Exception as e:  # noqa: BLE001 - a basket left as it is is said so, not raised
                agent._log({"event": "error", "error": f"tidying: {type(e).__name__}: {e}"})
    return {
        "turns": result.turns,
        "seconds": result.seconds,
        "model_s": round(result.model_s, 1),
        "browser_s": round(result.browser_s, 1),
        "tokens_in": result.tokens_in,
        "tokens_cached": result.tokens_cached,
        "tokens_out": result.tokens_out,
        "screenshots": result.screenshots,
        "model": settings.model,
        "ended": result.ended,
        "claimed": result.success,
        "answer": result.answer,
        "commits_asked": result.commits_asked,
        "error": result.error,
        "tidied": tidied.answer if tidied else "",
        "tidy_tokens_in": tidied.tokens_in if tidied else 0,
        "tidy_tokens_out": tidied.tokens_out if tidied else 0,
    }
