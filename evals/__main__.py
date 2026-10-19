"""Runs the scenarios against TARS's brain as config.toml sets it up, reports what each got wrong, keeps every run's
transcript, and can compare with a saved baseline and fail a check.

    uv run python -m evals                                  # every scenario, 3 runs each
    uv run python -m evals --tag hand-off --runs 5
    uv run python -m evals --scenario weather-follow-up --runs 1 --show
    uv run python -m evals --baseline evals/results.jsonl   # what got better or worse since the last saved result
    uv run python -m evals --baseline evals/results.jsonl --no-regressions --fail-under 0.85   # as a check
    uv run python -m evals --save evals/results.jsonl       # keep the totals as the new baseline

Qwen runs on Cerebras (CEREBRAS_API_KEY; --brain groq for Groq, whose free key allows only a few turns a minute), with
the reasoning effort and tools config.toml gives it; hand-offs really go to OpenAI's model and the thinking model
(OPENAI_API_KEY), on the standard tier unless --fast asks for config.toml's (priority processing, which costs more
and only changes the times). A run that hits a real "too many requests" is run again after a pause, and isn't
counted if it never gets through. Every run's transcript goes to evals/runs/<time>/ (not committed).

Every run starts with an estimate of what it will cost, from earlier runs and evals/prices.toml; --budget refuses to
start a run estimated over it, and stops one that gets there (web searches are most of it: $10 per 1,000).

Exit status: 0 if it passes, 1 if --fail-under or --no-regressions fails it, 2 if some runs couldn't be run (still
rate-limited, or a service refused the account: out of credits, a bad key; the eval then stops early, and --save keeps
nothing), 3 if it was over --budget: estimated over it, so not started, or stopped when it got there.
"""

import argparse
import dataclasses
import hashlib
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from voice_assistant.__main__ import brain_config, make_openai_client, make_quick_client
from voice_assistant.config import QUICK_PROVIDERS, LLMConfig, load_config

from .costs import RUNS, cost, estimate, load_prices, save_profile
from .report import compare, gate, summarize, wilson
from .runner import RunResult, play
from .scenarios import Scenario, load

REPO = Path(__file__).parents[1]
PROVIDERS = {p.name.lower(): p for p in QUICK_PROVIDERS}
WAITS_S = (10, 20, 40, 60, 60)
# What the brain's behavior comes from besides config.toml: its rules, prompts and tools.
BRAIN_CODE = (
    "src/voice_assistant/llm.py",
    "src/voice_assistant/reminders.py",
    "src/voice_assistant/conversations.py",
    "src/voice_assistant/home.py",
    "src/voice_assistant/hass.py",
    "src/voice_assistant/androidtv.py",
)


class Stop:
    """Set when a service refuses the account: the runs not started yet are skipped, since they'd all fail the same."""

    def __init__(self):
        self.why, self._lock = "", threading.Lock()

    def set(self, why: str) -> None:
        with self._lock:
            self.why = self.why or why


OVER_BUDGET = "over the budget"


class Spent:
    """What the runs so far have cost; past `budget`, the reason to stop."""

    def __init__(self, prices, fast: bool, budget: float | None):
        self.prices, self.fast, self.budget, self.total = prices, fast, budget, 0.0
        self._lock = threading.Lock()

    def add(self, result: RunResult) -> str:
        with self._lock:
            self.total += cost(result.usage, self.prices, self.fast)
            if self.budget is not None and self.total >= self.budget:
                return f"{OVER_BUDGET} of ${self.budget:.2f}: ${self.total:.2f} spent"
        return ""


def run_until_answered(scenario: Scenario, run, stop: Stop) -> RunResult:
    """A run, again after a pause while the service says "too many requests"; none once the eval has stopped."""
    if stop.why:
        return RunResult(scenario.id, [], unavailable=f"not run: {stop.why}")
    result = run(scenario)
    for wait in WAITS_S:
        if not result.limited or stop.why:
            break
        time.sleep(wait)
        result = run(scenario)
    if result.unavailable:
        stop.set(result.unavailable)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3, help="runs of each scenario (default 3)")
    parser.add_argument("--parallel", type=int, default=4, help="scenario runs at once (default 4)")
    parser.add_argument("--scenario", action="append", help="only this scenario (by id; can be given again)")
    parser.add_argument("--tag", action="append", help="only scenarios with this tag (can be given again)")
    parser.add_argument("--brain", choices=sorted(PROVIDERS), default="cerebras", help="where Qwen runs")
    parser.add_argument("--model", help="the quick model, instead of config.toml's for that service")
    parser.add_argument("--effort", help="the quick model's reasoning effort, instead of quick_reasoning_effort")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="OpenAI's requests on config.toml's service_tier, as TARS sends them (priority processing costs more per "
        "token): only for timing; by default they're on the standard tier",
    )
    parser.add_argument("--show", action="store_true", help="print every turn of every run, not only what's wrong")
    parser.add_argument("--baseline", help="compare with the last result saved in this file (results.jsonl)")
    parser.add_argument("--no-regressions", action="store_true", help="fail if a scenario got worse than --baseline")
    parser.add_argument("--fail-under", type=float, help="fail if fewer runs than this share are right (e.g. 0.85)")
    parser.add_argument(
        "--save",
        help="add the totals, as a line of JSON, to this file (e.g. evals/results.jsonl), if every run was counted",
    )
    parser.add_argument("--list", action="store_true", help="list the scenarios, and stop")
    parser.add_argument("--budget", type=float, help="dollars: don't start a run estimated over this, stop at it")
    parser.add_argument("--estimate", action="store_true", help="say what the run would cost, and stop")
    args = parser.parse_args()
    if args.no_regressions and not args.baseline:
        parser.error("--no-regressions needs --baseline")

    scenarios = pick(load(), args.scenario, args.tag)
    if args.list:
        for s in scenarios:
            print(f"{s.id:32} {len(s.turns)} turn{'s' * (len(s.turns) != 1)}  [{', '.join(s.tags)}]  {s.file}")
        return
    baseline = last_line(Path(args.baseline)) if args.baseline else None
    prices = load_prices()
    expected, unknown = estimate(scenarios, args.runs)
    guess = cost(expected, prices, args.fast)
    searches = sum(u.get("searches", 0) for u in expected.values())
    unpriced = prices.unpriced(expected)
    runs = f"{len(scenarios)} scenarios x {args.runs} runs"
    print(f"Estimated cost: ${guess:.2f} ({runs}, about {searches:.0f} web searches;")
    print(f"prices checked {prices.checked}" + (f"; no price for {', '.join(unpriced)})" if unpriced else ")"))
    if unknown:
        print(f"    no earlier runs of {', '.join(unknown)}: estimated as an average scenario")
    if args.estimate:
        return
    if args.budget is not None and guess > args.budget:
        print(f"Not run: estimated over the budget of ${args.budget:.2f}.")
        sys.exit(3)
    print()

    cfg = load_config(REPO / "config.toml")
    provider = PROVIDERS[args.brain]
    model = args.model or getattr(cfg.llm, provider.setting)
    if not model:
        sys.exit(f"No quick model: give --model, or set [llm] {provider.setting}.")
    llm = brain_config(cfg, typed=False)
    if not args.fast:
        # The tier changes how soon OpenAI answers and what it costs, not what it answers: the evals score answers.
        llm = dataclasses.replace(llm, service_tier="")
    if args.effort is not None:
        llm = dataclasses.replace(llm, quick_reasoning_effort=args.effort)
    env = REPO / ".env"
    quick_client, openai_client = make_quick_client(env, provider), make_openai_client(env)
    # TARS asks Groq first, capped for its free key, then Cerebras. On Cerebras, both are played there, each with its
    # own cap, so a reply too long for Groq's runs out here too, and goes on as it would at home.
    groq = PROVIDERS["groq"]
    services = [(provider.name, provider.max_tokens)]
    if provider.name != groq.name:
        services.insert(0, (f"{provider.name}, as {groq.name}", groq.max_tokens))
    effort = llm.quick_reasoning_effort or llm.reasoning_effort or "default"
    print(f"{model} on {provider.name}, reasoning {effort}, tools {'on' if llm.quick_tools else 'off'}; hand-offs to")
    tier = llm.service_tier or "standard"
    thinking = f"{llm.think_model or llm.model} ({llm.think_effort or 'no'} thinking)"
    print(f"{llm.model} and {thinking}, on OpenAI's {tier} tier")
    print(f"{len(scenarios)} scenarios x {args.runs} runs\n")

    def run(scenario: Scenario) -> RunResult:
        return play(scenario, llm, quick_client, model, openai_client, services)

    started = datetime.now().astimezone()
    jobs, stop, spent = [s for s in scenarios for _ in range(args.runs)], Stop(), Spent(prices, args.fast, args.budget)

    def counted_run(scenario: Scenario) -> RunResult:
        result = run_until_answered(scenario, run, stop)
        if over := spent.add(result):
            stop.set(over)
        return result

    with ThreadPoolExecutor(args.parallel) as pool:
        results = list(pool.map(counted_run, jobs))
    if stop.why:
        print(f"STOPPED, so the result is incomplete: {stop.why}\n")

    print_runs(scenarios, results, args.show)
    summary = summarize(scenarios, results) | {
        "date": started.isoformat(timespec="minutes"),
        "commit": git("rev-parse", "--short", "HEAD"),
        "dirty": bool(git("status", "--porcelain", "--", "src", "config.toml", "evals/scenarios")),
        "brain": brain_fingerprint(llm, model),
        "model": model,
        "where": provider.name.lower(),
        "effort": effort,
        "quick_tools": llm.quick_tools,
        "openai_model": llm.model,
        "openai_tier": llm.service_tier or "standard",
        "think": f"{llm.think_model or llm.model} {llm.think_effort}".strip(),
        "runs": args.runs,
    }
    summary["cost_usd"], summary["estimated_usd"] = round(spent.total, 3), round(guess, 3)
    print_totals(summary)
    print(f"Cost: ${spent.total:.2f} (estimated ${guess:.2f})")
    changes = compare(summary, baseline) if baseline else []
    if baseline:
        print_comparison(changes, baseline, summary)

    folder = keep_transcripts(started, scenarios, results, summary)
    print(f"\nTranscripts: {folder.relative_to(REPO)}/")
    if args.save:
        save_result(Path(args.save), summary, scenarios, results)

    if why := gate(summary, changes, args.fail_under, args.no_regressions):
        print("\nFAILED: " + "; ".join(why))
        sys.exit(1)
    if summary["not_run"]:
        why = stop.why or f"still rate-limited after {sum(WAITS_S)} s"
        print(f"\n{summary['not_run']} run(s) aren't counted: {why}")
        sys.exit(3 if why.startswith(OVER_BUDGET) else 2)


def save_result(path: Path, summary: dict, scenarios: list[Scenario], results: list[RunResult]) -> bool:
    """Adds the totals to `path`, where they're the next baseline, and what each scenario used to evals/usage.json;
    True if it did. Not a result with runs that weren't counted: the scenarios left out would look as if they'd got
    better."""
    if summary["not_run"]:
        print(f"\nNot saved to {path}: {summary['not_run']} run(s) aren't counted, so it isn't a whole result.")
        return False
    with open(path, "a") as f:
        f.write(json.dumps(summary) + "\n")
    save_profile({s.id: [r.usage for r in results if r.scenario == s.id and r.counted] for s in scenarios})
    print(f"Saved to {path}, and what each scenario used to evals/usage.json.")
    return True


def pick(scenarios: list[Scenario], ids: list[str] | None, tags: list[str] | None) -> list[Scenario]:
    if ids:
        if unknown := set(ids) - {s.id for s in scenarios}:
            sys.exit(f"No scenario {sorted(unknown)}.")
        scenarios = [s for s in scenarios if s.id in ids]
    if tags:
        scenarios = [s for s in scenarios if set(tags) & set(s.tags)]
    if not scenarios:
        sys.exit("No scenarios match.")
    return scenarios


def last_line(path: Path) -> dict:
    lines = [line for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    if not lines:
        sys.exit(f"No saved result in {path} to compare with.")
    return json.loads(lines[-1])


def brain_fingerprint(llm: LLMConfig, model: str) -> str:
    """Of everything that decides what the brain does: its settings (prompt included), the quick model, and the code
    its rules, prompts and tools are in. Two results with the same one ran the same brain."""
    h = hashlib.sha256(json.dumps(dataclasses.asdict(llm), sort_keys=True, default=str).encode() + model.encode())
    for name in BRAIN_CODE:
        h.update((REPO / name).read_bytes())
    return h.hexdigest()[:12]


def git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)
        return out.stdout.strip()
    except OSError:
        return ""


def keep_transcripts(started: datetime, scenarios: list[Scenario], results: list[RunResult], summary: dict) -> Path:
    """Every run, turn by turn (what was asked, the reply as written and as heard, what it did and what was wrong),
    for reading later and for grading by a judge; and the totals."""
    folder = RUNS / started.strftime("%Y-%m-%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    about = {s.id: s for s in scenarios}
    counts: dict[str, int] = {}
    with open(folder / "runs.jsonl", "w") as f:
        for r in results:
            counts[r.scenario] = counts.get(r.scenario, 0) + 1
            s = about[r.scenario]
            row = {"scenario": r.scenario, "run": counts[r.scenario], "fingerprint": s.fingerprint, "ok": r.ok}
            row |= {"limited": r.limited, "unavailable": r.unavailable, "usage": r.usage}
            row["turns"] = [dataclasses.asdict(t) for t in r.turns]
            f.write(json.dumps(row) + "\n")
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return folder


def print_runs(scenarios: list[Scenario], results: list[RunResult], show: bool) -> None:
    width = max(len(s.id) for s in scenarios)
    for s in scenarios:
        runs = [r for r in results if r.scenario == s.id]
        counted = [r for r in runs if r.counted]
        print(f"{s.id:{width}}  {sum(r.ok for r in counted)}/{len(counted)}")
        for n, r in enumerate(runs, 1):
            if not r.counted:
                print(f"    run {n}: not counted ({'still rate-limited' if r.limited else r.unavailable[:120]})")
                continue
            for t, (turn, result) in enumerate(zip(s.turns, r.turns, strict=False), 1):
                if not (show or result.wrong):
                    continue
                first = f"{result.first_s:.2f} s" if result.first_s is not None else "-"
                print(f"    run {n}, turn {t} ({result.route}, {first}): {turn.say!r}")
                print(f"        -> {result.said[:200]!r}")
                for bit in result.changes + result.sent:
                    print(f"        did: {bit}")
                for w in result.wrong:
                    print(f"        WRONG: {w}")


def print_totals(summary: dict) -> None:
    right, runs = summary["right"]
    low, high = summary["interval"]
    ok, turns = summary["turns"]
    print(f"\nScenario runs right: {right}/{runs} (95% interval {low:.0%}-{high:.0%}); turns right: {ok}/{turns}")
    for tag, (p, n) in summary["tags"].items():
        lo, hi = wilson(p, n)
        print(f"    {tag:16} {p:>3}/{n:<3} {lo:.0%}-{hi:.0%}")
    print("First words, median / 90th percentile (s):")
    for route, t in summary["first_words"].items():
        print(f"    {route:16} {t['median']:.2f} / {t['p90']:.2f}  ({t['turns']} turns)")
    print("Tokens:")
    for model, u in summary["usage"].items():
        searches = f", {u['searches']} web searches" if u.get("searches") else ""
        print(
            f"    {model:24} {u['requests']:>5} requests, {u['input']:>9,} in, {u['output']:>8,} out "
            f"({u['reasoning']:,} reasoning){searches}"
        )


def print_comparison(changes: list, baseline: dict, summary: dict) -> None:
    same_brain = "same brain" if baseline.get("brain") == summary["brain"] else "a different brain"
    print(f"\nAgainst the baseline of {baseline.get('date', '?')} ({baseline.get('commit', '?')}, {same_brain}):")
    b_right, b_runs = baseline.get("right", [0, 0])
    print(f"    all: {b_right}/{b_runs} -> {summary['right'][0]}/{summary['right'][1]}")
    shown = [c for c in changes if c.verdict != "same"]
    for c in shown:
        print(f"    {c.verdict:8} {c.scenario}: {c.before[0]}/{c.before[1]} -> {c.after[0]}/{c.after[1]}")
    if not shown:
        print("    no scenario better or worse")


if __name__ == "__main__":
    main()
