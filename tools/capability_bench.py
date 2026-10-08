"""Does Qwen do what it can itself, hand over what it can't, and get the details right? Run before quick_tools.

    uv run python tools/capability_bench.py              # every request, 6 times each
    uv run python tools/capability_bench.py --runs 2     # a quicker look

Qwen on Cerebras, with its own tools (quick_tools on, whatever config.toml says), hears each request with no
conversation before it, and each answer is sorted by what it did: set, cancelled or snoozed a reminder, sent
something, handed the turn over (<look-up> or <ponder>), or just answered. A tool call is then checked in detail:
a timer's minutes, a reminder's moment (worked out by TARS from the day and time Qwen gives), who it's for, a list's
entries. OpenAI is never asked: a hand-off ends the run, since what matters is that Qwen made it. Reminders are
checked, never set. A run costs nothing on Cerebras's free tier and takes about a minute.

Turn quick_tools on when every request comes out right (or nearly: say 6/6 and a few 5/6, none of them a wrong time).
"""

import argparse
import dataclasses
import statistics
import sys
import tempfile
import time
import types
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from openai import RateLimitError

from voice_assistant.__main__ import brain_config, make_cerebras_client
from voice_assistant.config import load_config
from voice_assistant.events import EventLog
from voice_assistant.llm import LOOKED_UP, PONDERED, QUICK, CerebrasChat
from voice_assistant.reminders import (
    ADD,
    CANCEL,
    MESSAGE,
    REMINDER,
    SNOOZE,
    TIMER,
    WEB,
    NewReminder,
    Reminders,
    ReminderTools,
    due_at,
)

REPO = Path(__file__).parents[1]
VOICES = ["alon", "stacey"]
SPEAKER = "[Speaker: alon] "

# What a run did.
REMIND, CANCELLED, SNOOZED, SENT, LOOK_UP, PONDER, ANSWER, FAILED = (
    "remind",
    "cancel",
    "snooze",
    "send",
    "look-up",
    "ponder",
    "answer",
    "failed",
)


@dataclass
class Case:
    text: str
    want: str
    check: Callable[["Run"], str] | None = None  # "" if the details are right, else what's wrong


@dataclass
class Run:
    did: str
    said: str
    first_s: float | None  # to the first words, if any
    total_s: float
    changes: list = field(default_factory=list)
    sent: list = field(default_factory=list)
    error: str = ""
    tool_errors: list[str] = field(default_factory=list)  # what the tools answered that was an error


def in_minutes(minutes: float, kind: str = TIMER) -> Callable[[Run], str]:
    def check(run: Run) -> str:
        c = run.changes[0]
        want = c.asked_at + minutes * 60
        if c.reminder.kind != kind:
            return f"a {c.reminder.kind}, not a {kind}"
        return "" if abs(c.reminder.due - want) < 2 else f"due in {(c.reminder.due - c.asked_at) / 60:.1f} min"

    return check


def at(day: str | None, clock: str, kind: str = REMINDER, for_name: str | None = "alon") -> Callable[[Run], str]:
    def check(run: Run) -> str:
        r = run.changes[0].reminder
        want = due_at(day, clock, run.changes[0].asked_at)
        if r.kind != kind:
            return f"a {r.kind}, not a {kind}"
        if r.due is None or abs(r.due - want) > 1:
            return f"due {time.strftime('%a %d %b %H:%M', time.localtime(r.due)) if r.due else 'when back'}"
        return "" if (r.for_name or "").lower() == (for_name or "") else f"for {r.for_name}"

    return check


def when_back(for_name: str) -> Callable[[Run], str]:
    def check(run: Run) -> str:
        r = run.changes[0].reminder
        if r.kind != MESSAGE or r.due is not None:
            return f"a {r.kind}, due {r.due}"
        return "" if (r.for_name or "").lower() == for_name else f"for {r.for_name}"

    return check


def the_one(rid_of: str, minutes: float | None = None) -> Callable[[Run], str]:
    def check(run: Run) -> str:
        c = run.changes[0]
        if c.id != SEEDED[rid_of]:
            return f"number {c.id}, not {SEEDED[rid_of]}"
        return "" if minutes is None or c.minutes == minutes else f"{c.minutes} minutes"

    return check


def sent(kind: str | tuple[str, ...], entries: int | None = None) -> Callable[[Run], str]:
    kinds = (kind,) if isinstance(kind, str) else kind

    def check(run: Run) -> str:
        item = run.sent[0]
        if item.kind not in kinds:
            return f"a {item.kind}"
        return "" if entries is None or len(item.entries or []) == entries else f"{len(item.entries or [])} entries"

    return check


def next_date(month: int, day: int) -> str:
    today = datetime.now().astimezone().date()
    d = date(today.year, month, day)
    return (d if d > today else date(today.year + 1, month, day)).isoformat()


SEEDED: dict[str, int] = {}  # the reminders set before the runs, by name, for cancelling and snoozing
CASES = [
    # Reminders, with Qwen's own tools: the kind, and the moment, worked out by TARS from what Qwen gives.
    Case("set a timer for twelve minutes", REMIND, in_minutes(12)),
    Case("pasta timer, eight minutes", REMIND, in_minutes(8)),
    Case("remind me to call the bank in an hour and a half", REMIND, in_minutes(90, REMINDER)),
    Case("remind me at 6pm to take the bins out", REMIND, at(None, "18:00")),
    Case("remind me friday morning to pay the rent", REMIND, at("friday", "09:00")),
    Case("remind me tomorrow at 7:30 to call mum", REMIND, at("tomorrow", "07:30")),
    Case("remind me on November 3rd at 10 to renew my passport", REMIND, at(next_date(11, 3), "10:00")),
    Case("tell Stacey dinner's at eight when she's back", REMIND, when_back("stacey")),
    Case("cancel the pasta timer", CANCELLED, the_one("pasta")),
    Case("put off the bank reminder for ten minutes", SNOOZED, the_one("bank", 10)),
    # Sending, with Qwen's own tool (no links: those need the web).
    Case("send me a shopping list: eggs, milk and bread", SENT, sent("list", 3)),
    Case("save a note that the wifi password is tars1234", SENT, sent(("note", "file"))),
    Case("send me a recipe for pancakes", SENT, sent(("note", "list", "file"))),
    # Handed to OpenAI: the web, and links.
    Case("what's the weather tomorrow?", LOOK_UP),
    Case("who won the Arsenal game last night?", LOOK_UP),
    Case("send me a link to the BBC weather page", LOOK_UP),
    Case("how much is a flight to Rome?", LOOK_UP),
    # Handed to the thinking model.
    Case("plan a three day trip to Lisbon for the two of us", PONDER),
    Case("should I lease or buy my next car? I drive about 15,000 km a year", PONDER),
    Case("if I save 300 a month at 4 percent interest, how much will I have in ten years?", PONDER),
    # Answered by Qwen itself: no tool, no hand-off.
    Case("what's the capital of Australia?", ANSWER),
    Case("tell me a joke", ANSWER),
    Case("how many minutes are in a day?", ANSWER),
    Case("what time is it?", ANSWER),
    # What TARS can't do: said plainly, with nothing called and nothing handed over.
    Case("play some jazz", ANSWER),
    Case("turn off the living room lights", ANSWER),
]
GROUPS = {REMIND: "reminders", CANCELLED: "reminders", SNOOZED: "reminders", SENT: "sending", LOOK_UP: "the web"}
GROUPS |= {PONDER: "thinking", ANSWER: "itself"}
FIRST_GROUPS = {case.text: GROUPS[case.want] for case in CASES}  # by what each was meant for, before any flags


def handed_over_client():
    """An OpenAI client that's never really asked: a hand-off ends with a line, so the run can stop there."""

    def create(**kw):
        return [
            types.SimpleNamespace(type="response.output_text.delta", delta="(handed over)"),
            types.SimpleNamespace(type="response.completed"),
        ]

    return types.SimpleNamespace(responses=types.SimpleNamespace(create=create))


class Watched:
    """Cerebras's client, noting a "too many requests" on its way to the brain (which hands the turn to OpenAI)."""

    def __init__(self, client):
        self._client, self.limited = client, False
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))
        self.models = client.models

    def _create(self, **kw):
        try:
            return self._client.chat.completions.create(**kw)
        except RateLimitError:
            self.limited = True
            raise


def run_once(make_brain: Callable[[Watched], CerebrasChat], cerebras, text: str) -> Run:
    """One run, tried again after a pause while Cerebras says "too many requests": that's the free tier's limit on
    tokens a minute, not an answer."""
    for wait in (10, 20, 40, 60, 60, 60):
        watched = Watched(cerebras)
        run = _run_once(make_brain(watched), text)
        if not watched.limited:
            return run
        time.sleep(wait)
    return dataclasses.replace(run, error="still rate-limited after 4 minutes")


def _run_once(brain: CerebrasChat, text: str) -> Run:
    start, first, said = time.monotonic(), None, ""
    try:
        for piece in brain.stream_reply(SPEAKER + text):
            if first is None and piece.strip():
                first = time.monotonic() - start
            said += piece
    except Exception as e:  # noqa: BLE001 - a failed run is counted, not fatal
        return Run(FAILED, said, first, time.monotonic() - start, error=f"{type(e).__name__}: {e}")
    total = time.monotonic() - start
    if brain.answered_by == PONDERED:
        did = PONDER
    elif brain.answered_by == LOOKED_UP:
        did = LOOK_UP
    elif brain.answered_by != QUICK:
        did = FAILED  # Cerebras failed and OpenAI was asked instead
    elif brain.changes:
        did = {ADD: REMIND, CANCEL: CANCELLED, SNOOZE: SNOOZED}[brain.changes[0].action]
    else:
        did = SENT if brain.sent else ANSWER
    errors = [c["output"] for c in brain._writing.quick_calls if str(c.get("output", "")).startswith("error")]
    return Run(did, said.strip(), first, total, list(brain.changes), list(brain.sent), tool_errors=errors)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=6, help="how many times to ask each (default 6)")
    parser.add_argument("--parallel", type=int, default=2, help="requests at once (default 2)")
    parser.add_argument("--no-tools", action="store_true", help="today's way: Qwen hands reminders and sending over")
    parser.add_argument("--no-ponder", action="store_true", help="no <ponder>: Qwen answers hard questions itself")
    parser.add_argument("--effort", help="Qwen's reasoning effort, instead of [llm] reasoning_effort")
    args = parser.parse_args()

    cfg = load_config(REPO / "config.toml")
    if not cfg.llm.cerebras_model:
        sys.exit("[llm] cerebras_model is empty: there's no quick model to test.")
    llm = dataclasses.replace(brain_config(cfg, typed=False), quick_tools=not args.no_tools, send=True)
    if args.no_ponder:
        llm = dataclasses.replace(llm, think_effort="")
    if args.effort is not None:
        llm = dataclasses.replace(llm, reasoning_effort=args.effort)
    # What each request should get, this time: without its own tools Qwen hands those over; without <ponder> it
    # answers hard questions itself.
    for case in CASES:
        if args.no_tools and case.want in (REMIND, CANCELLED, SNOOZED, SENT):
            case.want, case.check = LOOK_UP, None
        if args.no_ponder and case.want == PONDER:
            case.want = ANSWER
    cerebras = make_cerebras_client(REPO / ".env")
    folder = Path(tempfile.mkdtemp(prefix="tars-bench-"))
    reminders = Reminders(EventLog(folder / "events").store)
    SEEDED["pasta"] = reminders.add(NewReminder(TIMER, "pasta", due=time.time() + 600), WEB)
    SEEDED["bank"] = reminders.add(NewReminder(REMINDER, "call the bank", "alon", due=time.time() + 1800), WEB)
    tools = ReminderTools(reminders, voices=lambda: VOICES)

    def make_brain(client: Watched) -> CerebrasChat:
        return CerebrasChat(handed_over_client(), llm, client, reminders=tools)

    own = "off" if args.no_tools else "on"
    print(f"{cfg.llm.cerebras_model} on Cerebras, its own tools {own}, <ponder> {'off' if args.no_ponder else 'on'},")
    print(f"reasoning effort {llm.reasoning_effort or 'default'}; {len(CASES)} requests x {args.runs}\n")
    jobs = [(case, n) for case in CASES for n in range(args.runs)]
    with ThreadPoolExecutor(args.parallel) as pool:
        runs = list(pool.map(lambda job: run_once(make_brain, cerebras, job[0].text), jobs))

    by_case: dict[str, list[Run]] = {}
    for (case, _), run in zip(jobs, runs, strict=True):
        by_case.setdefault(case.text, []).append(run)
    groups: dict[str, list[int]] = {}
    for case in CASES:
        results = by_case[case.text]
        wrong = []
        for run in results:
            if run.did != case.want:
                why = run.error or "; ".join(run.tool_errors)
                wrong.append(f"{run.did}{f' ({why})' if why else ''}: {run.said[:70]!r}")
            elif case.check and (why := case.check(run)):
                wrong.append(f"{run.did}, but {why}")
        right = len(results) - len(wrong)
        tally = groups.setdefault(FIRST_GROUPS[case.text], [0, 0])
        tally[0] += right
        tally[1] += len(results)
        mark = "✓" if not wrong else "✗"
        print(f"{mark} {right}/{len(results)}  {case.text}  [{case.want}]")
        for w in dict.fromkeys(wrong):  # each different mistake once
            print(f"      {wrong.count(w)}x {w}")

    print("\nBy kind:")
    for group, (right, total) in groups.items():
        print(f"  {group:<10} {right}/{total}")
    for name, did in [("tool turns", (REMIND, CANCELLED, SNOOZED, SENT)), ("own answers", (ANSWER,))]:
        firsts = [r.first_s for r in runs if r.did in did and r.first_s is not None]
        totals = [r.total_s for r in runs if r.did in did]
        if firsts:
            print(
                f"\n{name}: first words after {statistics.median(firsts):.2f} s (median), "
                f"done after {statistics.median(totals):.2f} s"
            )
    print(f"\n(Its event database, for a look: {folder})")


if __name__ == "__main__":
    main()
