"""One task, start to end: TARS's Chrome started, the agent run in it, Chrome shut."""

import functools
import traceback
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from ..check import env_value
from . import mail
from .agent import Agent, Approver, Settings
from .chrome import PORT, PROFILE, own_chrome
from .login import Person, Unattended, sign_in
from .notes import Notes, site
from .session import Session
from .vault import Vault, household_person

ROOT = Path(__file__).resolve().parents[3]
TIDY = (
    "Empty the basket or cart on {site}: everything in it was put there by test runs (it was empty before them). "
    "Open it, remove every item, and check it's empty; if the site keeps a basket for each store or restaurant, "
    "empty the one the last run used (it said: {said}). Then say what you took out. Only a basket or a cart: if the "
    "site has none, change nothing (not saved lists, favourites or anything else) and say so."
)


def tidy_goal(turns: list[dict[str, Any]], said: str) -> str:
    """What a tidying run is asked: to empty the basket on the run's site, which the run's answer (`said`) says
    more about. That's for evals, whose rounds start with empty baskets: anything in one is a test run's."""
    where = site(next((t["url"] for t in reversed(turns) if t.get("url")), "")) or "the site"
    return TIDY.format(site=where, said=" ".join(said.split())[:400] or "nothing")


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
    person: Person | None = None,
    vault: Vault | None = None,
    openai=None,
    home: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Runs the agent on `goal` in a fresh TARS Chrome; what it did, as a dict.

    `tidy` (for evals, on a task that fills a basket): a fresh run then empties the basket on that site, so the next
    starts as this one did; its tokens are counted in the cost, and nothing else of it is.
    `notes`: the agent reads what it noted on the sites before, and writes what this run taught it.
    `person`: whose logins it signs in with (the vault's), and who's asked for a login or a code it lacks; with no
    one, the household's first person, whom nobody can ask. `openai`: the OpenAI client to use, if there is one
    already (TARS's own), else one from .env. `home`: the names the house is reached by ([web] allowed_hosts,
    [home] url), which the browser never opens."""
    settings = settings or Settings(turns=steps)
    person = person or Unattended(household_person(ROOT / "config.toml"))
    vault = vault or Vault()
    with own_chrome(profile, port) as address, sync_playwright() as p:
        context = p.chromium.connect_over_cdp(address).contexts[0]
        session = Session(context, home=home)
        claude = settings.model.startswith("claude-")  # Anthropic's API, in TARS's own loop
        if claude:
            from .anthropic_brain import client as anthropic_client

            model = anthropic_client(ROOT / ".env")
        else:
            model = openai or client()
        # The person's email is read for sign-in codes and links by the same OpenAI model (Claude's runs go without).
        mailer = (
            None if claude else functools.partial(mail.wait_for_sign_in, read=mail.model_reader(model, settings.model))
        )
        agent = Agent(model, session, approver, settings, trace=trace_to, notes=notes,
                      sign_in=lambda s: sign_in(s.page, vault, person, goal, hide=s.secrets.add,
                                                wait_for_mail=mailer))  # fmt: skip
        try:
            result = agent.run(goal)
        except Exception as e:  # noqa: BLE001 - a crash is a result like any other, and the trace says where
            result = agent.result
            result.ended, result.error = "error", f"{type(e).__name__}: {e}"
            agent._log({"event": "error", "error": result.error, "where": traceback.format_exc()[-3000:]})
        learned, learn_in, learn_out = "", 0, 0
        if notes is not None and result.ended != "error" and not claude:  # notes.py writes them with OpenAI
            try:
                learned, learn_in, learn_out = notes.learn(model, settings.model, goal, result.ended, agent.turns_done)
            except Exception as e:  # noqa: BLE001 - notes not written this time is fine
                agent._log({"event": "error", "error": f"learning: {type(e).__name__}: {e}"})
        tidied = None
        if tidy:
            agent._log({"event": "follow_up", "request": "tidy"})
            tidier = Agent(model, session, _NoOne(), Settings(model=settings.model, turns=20), trace=trace_to)
            try:
                tidied = tidier.run(tidy_goal(agent.turns_done, result.answer))
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
        "aloud": result.aloud,
        "commits_asked": result.commits_asked,
        "error": result.error,
        "tidied": tidied.answer if tidied else "",
        "tidy_tokens_in": tidied.tokens_in if tidied else 0,
        "tidy_tokens_out": tidied.tokens_out if tidied else 0,
        "notes_used": sorted(agent.noted),
        "fallbacks": getattr(agent.brain, "fallbacks", 0),
        "learned": learned,
        "learn_tokens_in": learn_in,
        "learn_tokens_out": learn_out,
    }
