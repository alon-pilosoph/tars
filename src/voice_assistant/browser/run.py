"""One task, start to end: TARS's Chrome started, the agent run in it, Chrome shut; what it did, as a Run."""

import functools
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..check import env_value
from . import mail
from .agent import Agent, Approver, Result, Settings
from .chrome import PORT, PROFILE, browser_session
from .login import Person, Unattended, sign_in
from .notes import Notes
from .session import Session
from .vault import Vault, household_person

ROOT = Path(__file__).resolve().parents[3]  # the clone, for the command line and the evals: TARS passes its own


@dataclass
class Run:
    """What a run did: the agent's Result, and what the run around it did (the sites' notes read and learned)."""

    result: Result
    model: str
    turns: list[dict[str, Any]] = field(default_factory=list)  # each turn's call and what it did, as the trace has
    notes_used: list[str] = field(default_factory=list)
    fallbacks: int = 0
    learned: str = ""
    learn_tokens_in: int = 0
    learn_tokens_out: int = 0

    def record(self) -> dict[str, Any]:
        """The run as one flat record, for the evals' results.jsonl."""
        found = asdict(self.result)
        found["claimed"] = found.pop("success")
        found["model_s"], found["browser_s"] = round(found["model_s"], 1), round(found["browser_s"], 1)
        return {**found, "model": self.model, "notes_used": self.notes_used, "fallbacks": self.fallbacks,
                "learned": self.learned, "learn_tokens_in": self.learn_tokens_in,
                "learn_tokens_out": self.learn_tokens_out}  # fmt: skip


def client(env: Path = ROOT / ".env"):
    """An OpenAI client from `env`'s key, for the command line and the evals (TARS passes its own)."""
    from openai import OpenAI

    key = env_value(env, "OPENAI_API_KEY")
    if not key:
        raise RuntimeError(f"no OPENAI_API_KEY in {env}")
    return OpenAI(api_key=key, timeout=120, max_retries=3)


def model_client(settings: Settings, openai=None, env: Path = ROOT / ".env"):
    """The client for `settings.model`: Anthropic's for a Claude model (the evals try it in TARS's loop), else
    `openai`, or one from `env`."""
    if settings.model.startswith("claude-"):
        from .anthropic_brain import client as anthropic_client

        return anthropic_client(env)
    return openai or client(env)


def run_in(
    session: Session,
    goal: str,
    approver: Approver,
    model,
    settings: Settings,
    trace_to: Path | None = None,
    notes: Notes | None = None,
    person: Person | None = None,
    vault: Vault | None = None,
) -> Run:
    """Runs the agent on `goal` in `session`, with the `model` client; a crash is a Run like any other, and the
    trace says where. `notes`: the agent reads what it noted on the sites before, and writes what this run taught
    it. `person`: whose logins it signs in with (the vault's), and who's asked for a login or a code it lacks; with
    no one, the household's first person, whom nobody can ask."""
    person = person or Unattended(household_person(ROOT / "config.toml"))
    vault = vault or Vault()
    openai = not settings.model.startswith("claude-")
    # The person's email is read for sign-in codes and links by the same OpenAI model (Claude's runs go without).
    mailer = functools.partial(mail.wait_for_sign_in, read=mail.model_reader(model, settings.model)) if openai else None
    agent = Agent(model, session, approver, settings, trace=trace_to, notes=notes,
                  sign_in=lambda s: sign_in(s.page, vault, person, goal, hide=s.secrets.add,
                                            wait_for_mail=mailer))  # fmt: skip
    try:
        result = agent.run(goal)
    except Exception as e:  # noqa: BLE001 - a crash is a result like any other, and the trace says where
        result = agent.result
        result.ended, result.error = "error", f"{type(e).__name__}: {e}"
        agent.log({"event": "error", "error": result.error, "where": traceback.format_exc()[-3000:]})
    run = Run(result, settings.model, agent.turns_done, sorted(agent.noted), getattr(agent.brain, "fallbacks", 0))
    if notes is not None and result.ended != "error" and openai:  # notes.py writes them with OpenAI
        try:
            run.learned, run.learn_tokens_in, run.learn_tokens_out = notes.learn(
                model, settings.model, goal, result.ended, agent.turns_done
            )
        except Exception as e:  # noqa: BLE001 - notes not written this time is fine
            agent.log({"event": "error", "error": f"learning: {type(e).__name__}: {e}"})
    return run


def run_task(
    goal: str,
    approver: Approver,
    trace_to: Path | None = None,
    settings: Settings | None = None,
    profile: Path = PROFILE,
    port: int = PORT,
    notes: Notes | None = None,
    person: Person | None = None,
    vault: Vault | None = None,
    openai=None,
    home: frozenset[str] = frozenset(),
) -> Run:
    """Runs the agent on `goal` in a fresh TARS Chrome (run_in), then shuts it. `openai`: the OpenAI client to use,
    if there is one already (TARS's own), else one from .env. `home`: the names the house is reached by ([web]
    allowed_hosts, [home] url), which the browser never opens."""
    settings = settings or Settings()
    with browser_session(profile, port, home) as session:
        return run_in(session, goal, approver, model_client(settings, openai), settings, trace_to, notes, person,
                      vault)  # fmt: skip
