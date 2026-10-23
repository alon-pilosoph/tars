"""One task, start to end: TARS's Chrome started, the agent run in it, Chrome shut."""

import re
import traceback
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from ..check import env_value
from .agent import Agent, Approver, Settings
from .chrome import PORT, PROFILE, own_chrome
from .notes import Notes, site
from .session import Session

ROOT = Path(__file__).resolve().parents[3]
TIDY = (
    "Empty the basket or cart on {site}: everything in it was put there by test runs (it was empty before them). "
    "Open it, remove every item, and check it's empty; if the site keeps a basket for each store or restaurant, "
    "empty each one the last run used ({clicks} were its clicks). Then say what you took out."
)
CLICKED = re.compile(r"clicked (?:(?:f\d+)?e\d+ |the text )(['\"])(.+?)\1")


FILLS = re.compile(r"\b(add|order again|reorder|cart|basket|bag)\b|הוסף|הוספה|לסל|לעגלה", re.IGNORECASE)


def _filled(turns: list[dict[str, Any]]) -> bool:
    """Whether a run clicked something that may have put things in a basket ("Add to order", "Order again")."""
    return any(t["call"] == "act" and FILLS.search(str(t.get("said", ""))) for t in turns)


def tidy_goal(turns: list[dict[str, Any]]) -> str:
    """What a tidying run is asked: to empty the basket on the run's site. That's for evals, whose rounds start
    with empty baskets (anything in one is a test run's); taking out only what the run added was tried, and missed
    what a click like "Order again" puts in without naming it. The run's clicks say which baskets it used."""
    clicks = [m.group(2) for t in turns if t["call"] == "act" for m in CLICKED.finditer(str(t.get("said", "")))]
    where = site(next((t["url"] for t in reversed(turns) if t.get("url")), "")) or "the site"
    return TIDY.format(site=where, clicks="; ".join(f"'{c}'" for c in clicks[-30:]) or "(nothing)")


class _NoOne:
    """The person, for a tidying run: it never commits, so any approval it asks for is a no."""

    def approve(self, draft: str, picture: bytes | None = None) -> bool:
        return False


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
    notes: Notes | None = None,
) -> dict[str, Any]:
    """Runs the agent on `goal` in a fresh TARS Chrome; what it did, as a dict.

    `tidy` (for evals): after a run that asked to commit or may have filled a basket, a fresh run empties the basket
    on that site, so the next starts as this one did; its tokens are counted in the cost, and nothing else of it is.
    `notes`: the agent reads what it noted on the sites before, and writes what this run taught it."""
    settings = settings or Settings(turns=steps)
    with own_chrome(profile, port) as address, sync_playwright() as p:
        context = p.chromium.connect_over_cdp(address).contexts[0]
        session = Session(context)
        model = client()
        agent = Agent(model, session, approver, settings, trace=trace_to, notes=notes)
        try:
            result = agent.run(goal)
        except Exception as e:  # noqa: BLE001 - a crash is a result like any other, and the trace says where
            result = agent.result
            result.ended, result.error = "error", f"{type(e).__name__}: {e}"
            agent._log({"event": "error", "error": result.error, "where": traceback.format_exc()[-3000:]})
        learned, learn_in, learn_out = "", 0, 0
        if notes is not None and result.ended != "error":
            try:
                learned, learn_in, learn_out = notes.learn(model, settings.model, goal, result.ended, agent.turns_done)
            except Exception as e:  # noqa: BLE001 - notes not written this time is fine
                agent._log({"event": "error", "error": f"learning: {type(e).__name__}: {e}"})
        tidied = None
        if tidy and (result.commits_asked or _filled(agent.turns_done)):
            agent._log({"event": "follow_up", "request": "tidy"})
            tidier = Agent(model, session, _NoOne(), Settings(model=settings.model, turns=12), trace=trace_to)
            try:
                tidied = tidier.run(tidy_goal(agent.turns_done))
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
        "notes_used": sorted(agent.noted),
        "learned": learned,
        "learn_tokens_in": learn_in,
        "learn_tokens_out": learn_out,
    }
