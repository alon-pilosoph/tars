"""Does Qwen do what it can itself, hand over what it can't, and get the details right? Run before quick_tools.

    uv run python tools/capability_bench.py              # every request, 6 times each
    uv run python tools/capability_bench.py --runs 2     # a quicker look
    uv run python tools/capability_bench.py --brain claude --model claude-haiku-5-5    # another quick model
    uv run python tools/capability_bench.py --brain openai --model gpt-6-luna

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
import json
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

from voice_assistant.__main__ import api_key, brain_config, make_cerebras_client, make_openai_client
from voice_assistant.config import load_config
from voice_assistant.events import EventLog
from voice_assistant.llm import LOOKED_UP, PONDERED, QUICK, CerebrasChat, ClaudeQuickChat, ResponsesQuickChat
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
    tiers: list[str] = field(default_factory=list)  # the service tier the responses say they were served on


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
    """The quick model's client, noting on the way to the brain a "too many requests" (which the brain answers by
    handing the turn to OpenAI) and the service tier each response says it was served on."""

    def __init__(self, client):
        self._client, self.limited, self.tiers = client, False, set()
        if hasattr(client, "chat"):  # Chat Completions (Cerebras, OpenAI) and the Responses API (OpenAI)
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))
            self.responses = types.SimpleNamespace(create=self._respond)
        else:  # Anthropic's Messages API
            self.messages = types.SimpleNamespace(stream=self._stream)

    def _create(self, **kw):
        try:
            return _Tiers(self._client.chat.completions.create(**kw), self.tiers)
        except RateLimitError:
            self.limited = True
            raise

    def _respond(self, **kw):
        try:
            return _Tiers(self._client.responses.create(**kw), self.tiers)
        except RateLimitError:
            self.limited = True
            raise

    def _stream(self, **kw):
        import anthropic

        try:
            return self._client.messages.stream(**kw)
        except anthropic.RateLimitError:
            self.limited = True
            raise


class _Tiers:
    """A streamed chat completion, noting the service tier its chunks say they were served on."""

    def __init__(self, stream, seen: set):
        self._stream, self._seen = stream, seen

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *exc):
        return self._stream.__exit__(*exc)

    def close(self):
        self._stream.close()

    def __iter__(self):
        for chunk in self._stream:
            # A chat completion chunk has it; a Responses API event has it on the response it's about.
            tier = getattr(chunk, "service_tier", None) or getattr(
                getattr(chunk, "response", None), "service_tier", None
            )
            if tier:
                self._seen.add(tier)
            yield chunk


BRAINS = ("cerebras", "openai", "responses", "claude")


def make_quick_client(brain: str):
    """(the quick model's client, the brain class that uses it). Keys come from .env, as TARS's do."""
    env = REPO / ".env"
    if brain == "cerebras":
        return make_cerebras_client(env), CerebrasChat
    if brain == "openai":
        return make_openai_client(env), CerebrasChat  # Chat Completions, as Cerebras's API is
    if brain == "responses":
        return make_openai_client(env), ResponsesQuickChat  # OpenAI's Responses API: reasoning and tools together
    import anthropic

    return anthropic.Anthropic(api_key=api_key(env, "ANTHROPIC_API_KEY"), max_retries=2), ClaudeQuickChat


def run_once(make_brain: Callable, client, text: str) -> Run:
    """One run, tried again after a pause while the service says "too many requests" (a rate limit, not an answer):
    only the attempt that went through is timed."""
    for wait in (10, 20, 40, 60, 60, 60):
        watched = Watched(client)
        run = _run_once(make_brain(watched), text)
        run.tiers = sorted(watched.tiers)
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
    parser.add_argument(
        "--parallel", type=int, default=1, help="requests at once (default 1, so waiting in a queue isn't timed)"
    )
    parser.add_argument("--no-tools", action="store_true", help="today's way: Qwen hands reminders and sending over")
    parser.add_argument("--no-ponder", action="store_true", help="no <ponder>: Qwen answers hard questions itself")
    parser.add_argument("--effort", help="the quick model's reasoning effort, instead of quick_reasoning_effort")
    parser.add_argument(
        "--brain",
        choices=BRAINS,
        default="cerebras",
        help="where the quick model runs: cerebras (default), openai (Chat Completions), responses (OpenAI's "
        "Responses API: Luna with reasoning) or claude",
    )
    parser.add_argument("--model", help="the quick model, instead of [llm] cerebras_model (e.g. claude-haiku-5-5)")
    parser.add_argument("--save", help="add the result, as a line of JSON, to this file (docs/models.jsonl)")
    parser.add_argument("--table", metavar="JSONL", help="print the table of saved results in this file, and stop")
    parser.add_argument(
        "--follow",
        action="store_true",
        help="really ask OpenAI after a hand-off (OPENAI_API_KEY), and time the first words the person would hear",
    )
    args = parser.parse_args()
    if args.table:
        print(TABLE_HEAD)
        rows = [json.loads(line) for line in Path(args.table).read_text().splitlines() if line.strip()]
        for r in sorted(rows, key=lambda r: (r["where"], r["model"], EFFORTS.index(r["effort"]), r["date"])):
            print(table_row(r))
        return

    cfg = load_config(REPO / "config.toml")
    model = args.model or cfg.llm.cerebras_model
    if not model:
        sys.exit("No quick model: give --model, or set [llm] cerebras_model.")
    llm = dataclasses.replace(
        brain_config(cfg, typed=False), cerebras_model=model, quick_tools=not args.no_tools, send=True
    )
    if args.no_ponder:
        llm = dataclasses.replace(llm, think_effort="")
    if args.effort is not None:
        llm = dataclasses.replace(llm, quick_reasoning_effort=args.effort)
    # What each request should get, this time: without its own tools Qwen hands those over; without <ponder> it
    # answers hard questions itself.
    for case in CASES:
        if args.no_tools and case.want in (REMIND, CANCELLED, SNOOZED, SENT):
            case.want, case.check = LOOK_UP, None
        if args.no_ponder and case.want == PONDER:
            case.want = ANSWER
    client, brain_class = make_quick_client(args.brain)
    folder = Path(tempfile.mkdtemp(prefix="tars-bench-"))
    reminders = Reminders(EventLog(folder / "events").store)
    SEEDED["pasta"] = reminders.add(NewReminder(TIMER, "pasta", due=time.time() + 600), WEB)
    SEEDED["bank"] = reminders.add(NewReminder(REMINDER, "call the bank", "alon", due=time.time() + 1800), WEB)
    tools = ReminderTools(reminders, voices=lambda: VOICES)

    openai_side = make_openai_client(REPO / ".env") if args.follow else None

    def make_brain(quick) -> CerebrasChat:
        brain = brain_class(openai_side or handed_over_client(), llm, quick, reminders=tools)
        if args.brain == "openai" and cfg.llm.service_tier:
            brain._quick_extra["service_tier"] = cfg.llm.service_tier  # as TARS asks OpenAI
        return brain

    own = "off" if args.no_tools else "on"
    print(f"{model} on {args.brain}, its own tools {own}, <ponder> {'off' if args.no_ponder else 'on'},")
    effort = llm.quick_reasoning_effort or llm.reasoning_effort or "default"
    print(f"reasoning effort {effort}; {len(CASES)} requests x {args.runs}\n")
    jobs = [(case, n) for case in CASES for n in range(args.runs)]
    with ThreadPoolExecutor(args.parallel) as pool:
        runs = list(pool.map(lambda job: run_once(make_brain, client, job[0].text), jobs))

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
    timing = {
        # To the first words of an answer it wrote itself, and of a reminder or send turn (tool, then words).
        "answer": spread([r.first_s for r in runs if r.did == ANSWER and r.first_s is not None]),
        "tool": spread([r.first_s for r in runs if r.did in TOOL_TURNS and r.first_s is not None]),
        # To the hand-off marker: the run ends there, since OpenAI isn't really asked.
        "hand_off": spread([r.total_s for r in runs if r.did in (LOOK_UP, PONDER)]),
    }
    if args.follow:  # then a hand-off's run goes on to OpenAI's reply: its first words are what's heard
        timing["hand_off"] = spread([r.first_s for r in runs if r.did == LOOK_UP and r.first_s is not None])
    for name, (median, p90) in timing.items():
        if median is not None:
            print(f"{name:>9}: {median:.2f} s median, {p90:.2f} s at the 90th percentile")
    tiers = sorted({t for r in runs for t in r.tiers})
    if tiers:
        print(f"Served on: {', '.join(tiers)}")
    result = {
        "date": time.strftime("%Y-%m-%d"),
        "model": model,
        "where": args.brain,
        "effort": effort,
        "tier": ", ".join(tiers) or None,
        "runs": args.runs,
        "groups": groups,
        "timing": timing,
        "wrong": sum(t - r for r, t in groups.values()),
        "total": sum(t for _, t in groups.values()),
    }
    print("\nThe row for docs/models.md:\n" + table_row(result))
    if args.save:
        with open(args.save, "a") as f:
            f.write(json.dumps(result) + "\n")
        print(f"(Saved to {args.save})")
    print(f"\n(Its event database, for a look: {folder})")


TOOL_TURNS = (REMIND, CANCELLED, SNOOZED, SENT)
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "default")
TABLE_HEAD = (
    "| Model | Where | Reasoning | Reminders | Sending | Web hand-off | Hard questions | Itself | All "
    "| First words, own answer (s) | First words, reminder or send (s) | Hand-off decided (s) | Date |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|"
)


def spread(seconds: list[float]) -> tuple[float | None, float | None]:
    """(median, 90th percentile), or (None, None) if nothing to measure."""
    if not seconds:
        return None, None
    p90 = statistics.quantiles(seconds, n=10, method="inclusive")[-1] if len(seconds) > 1 else seconds[0]
    return round(statistics.median(seconds), 2), round(p90, 2)


def table_row(r: dict) -> str:
    def score(group: str) -> str:
        right, total = r["groups"].get(group, (0, 0))
        return f"{right}/{total}"

    def secs(key: str) -> str:
        median, p90 = r["timing"][key]
        return "–" if median is None else f"{median:.2f} / {p90:.2f}"

    where = r["where"] + (f" ({r['tier']})" if r["tier"] and r["tier"] != "default" else "")
    right = r["total"] - r["wrong"]
    cells = [
        f"`{r['model']}`",
        where,
        r["effort"],
        *(score(g) for g in ("reminders", "sending", "the web", "thinking", "itself")),
        f"**{right}/{r['total']}**",
        secs("answer"),
        secs("tool"),
        secs("hand_off"),
        r["date"],
    ]
    return "| " + " | ".join(cells) + " |"


if __name__ == "__main__":
    main()
