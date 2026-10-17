"""Evals on request, from a pull request comment: `#evals`, with optional settings, runs them on the pull request's
code and answers with a comment of what came out. Never a check: nothing waits on it, nothing fails because of it.

    #evals                                   every scenario, 3 runs each
    #evals runs=5 tag=hand-off               one kind, more runs
    #evals scenario=cancel-that,got-it       just these
    #evals budget=3                          a higher cap (default $2, at most $5)
    #evals estimate                          only what it would cost: nothing is run

.github/workflows/evals.yml calls `python -m evals.comment "<the comment>"`, which runs `python -m evals` and prints
the reply, as markdown, for the workflow to post. Its parts are here, apart from the workflow, so they can be tested.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .costs import RUNS, cost, estimate, load_prices
from .report import compare
from .scenarios import load

TRIGGER = "#evals"
REPO = Path(__file__).parents[1]
BASELINE = Path(__file__).parent / "results.jsonl"
DEFAULT_RUNS, MAX_RUNS = 3, 10
DEFAULT_BUDGET, MAX_BUDGET = 2.0, 5.0
SHOWN_WRONG = 12  # failing turns quoted in the reply; the rest are in the transcripts
HELP = """`#evals` runs the scenario evals on this pull request and answers here. Settings, all optional:
`runs=5` (1-10, default 3), `tag=hand-off,reminders`, `scenario=cancel-that,got-it`, `budget=3` (dollars, default 2,
at most 5), `estimate` (only say what it would cost)."""


@dataclass
class Request:
    runs: int = DEFAULT_RUNS
    tags: list[str] = field(default_factory=list)
    scenarios: list[str] = field(default_factory=list)
    budget: float = DEFAULT_BUDGET
    estimate_only: bool = False

    def args(self) -> list[str]:
        """For `python -m evals`. Always on OpenAI's standard tier, always with a budget."""
        args = ["--runs", str(self.runs), "--budget", f"{self.budget:g}", "--parallel", "6"]
        args += [a for t in self.tags for a in ("--tag", t)] + [a for s in self.scenarios for a in ("--scenario", s)]
        if BASELINE.exists():
            args += ["--baseline", str(BASELINE)]
        return args


def parse(body: str) -> Request:
    """The request in a comment's first line; ValueError saying what's wrong, to answer with."""
    words = body.strip().splitlines()[0].split() if body.strip() else []
    if not words or words[0].lower() != TRIGGER:
        raise ValueError(f"A request starts with {TRIGGER}.")
    request, known = Request(), {s.id: s for s in load()}
    for word in words[1:]:
        key, _, value = word.partition("=")
        key = key.lower()
        if key == "estimate" and not value:
            request.estimate_only = True
        elif key == "runs" and value.isdigit() and 1 <= int(value) <= MAX_RUNS:
            request.runs = int(value)
        elif key == "budget" and _number(value) is not None and 0 < _number(value) <= MAX_BUDGET:
            request.budget = _number(value)
        elif key == "tag" and value:
            request.tags += value.split(",")
            if unknown := set(request.tags) - {t for s in known.values() for t in s.tags}:
                raise ValueError(f"No scenario has the tag {', '.join(sorted(unknown))}.")
        elif key == "scenario" and value:
            request.scenarios += value.split(",")
            if unknown := set(request.scenarios) - set(known):
                raise ValueError(f"No scenario {', '.join(sorted(unknown))}.")
        else:
            raise ValueError(f"I don't understand `{word}`.")
    return request


def _number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def chosen(request: Request) -> list:
    scenarios = load()
    if request.scenarios:
        scenarios = [s for s in scenarios if s.id in request.scenarios]
    if request.tags:
        scenarios = [s for s in scenarios if set(request.tags) & set(s.tags)]
    return scenarios


def estimate_reply(request: Request) -> str:
    scenarios = chosen(request)
    use, unknown = estimate(scenarios, request.runs)
    prices = load_prices()
    searches = sum(u.get("searches", 0) for u in use.values())
    lines = [
        (
            f"**Evals estimate:** ${cost(use, prices):.2f} for {len(scenarios)} scenarios × {request.runs} runs "
            f"(about {searches:.0f} web searches; prices checked {prices.checked}). Nothing was run."
        )
    ]
    if unknown:
        lines.append(f"No earlier runs of {', '.join(unknown)}: estimated as an average scenario.")
    if cost(use, prices) > request.budget:
        lines.append(f"That's over the budget of ${request.budget:g}: add `budget=` to run it.")
    return "\n\n".join(lines)


def result_reply(folder: Path, request: Request, status: int, run_url: str = "") -> str:
    """The reply for a run whose transcripts are in `folder`, given how `python -m evals` exited."""
    summary = json.loads((folder / "summary.json").read_text())
    rows = [json.loads(line) for line in (folder / "runs.jsonl").read_text().splitlines() if line.strip()]
    right, runs = summary["right"]
    low, high = summary["interval"]
    head = {0: "", 2: " (incomplete: some runs weren't counted)", 3: " (stopped at the budget)"}.get(status, "")
    totals = (
        f"95% interval {low:.0%}–{high:.0%}; turns right {summary['turns'][0]}/{summary['turns'][1]}; "
        f"{len(summary['scenarios'])} scenarios × {request.runs} runs; "
        f"cost ${summary.get('cost_usd', 0):.2f} (estimated ${summary.get('estimated_usd', 0):.2f}, "
        f"budget ${request.budget:g})."
    )
    out = [f"### Evals: {right}/{runs} runs right{head}", totals]
    imperfect = [(sid, v) for sid, v in summary["scenarios"].items() if v["runs"] and v["right"] < v["runs"]]
    if imperfect:
        out += ["", "| Scenario | Runs right |", "|---|---|"]
        out += [f"| `{sid}` | {v['right']}/{v['runs']} |" for sid, v in sorted(imperfect, key=lambda x: x[1]["right"])]
        out.append("\nEvery other scenario: all runs right.")
    else:
        out.append("\nEvery scenario: all runs right.")
    if BASELINE.exists() and (lines := [x for x in BASELINE.read_text().splitlines() if x.strip()]):
        baseline = json.loads(lines[-1])
        moved = [c for c in compare(summary, baseline) if c.verdict in ("worse", "better")]
        b_right, b_runs = baseline.get("right", [0, 0])
        out.append(f"\n**Against the baseline** ({baseline.get('date', '?')[:10]}, {b_right}/{b_runs}):")
        out += [f"- {c.verdict}: `{c.scenario}` {c.before[0]}/{c.before[1]} → {c.after[0]}/{c.after[1]}" for c in moved]
        if not moved:
            out.append("no scenario better or worse.")
    wrong = [(r, n, t) for r in rows for n, t in enumerate(r["turns"], 1) if t["wrong"]]
    if wrong:
        out += ["", f"<details><summary>What went wrong ({len(wrong)} turns)</summary>", ""]
        for r, n, t in wrong[:SHOWN_WRONG]:
            said = t["said"][:200].replace("\n", " ")
            out.append(f"- `{r['scenario']}` run {r['run']}, turn {n} ({t['route']}): {'; '.join(t['wrong'])}")
            out.append(f"  > {said or '(nothing)'}")
        if len(wrong) > SHOWN_WRONG:
            out.append(f"- and {len(wrong) - SHOWN_WRONG} more in the transcripts")
        out += ["", "</details>"]
    if run_url:
        out.append(f"\nTranscripts: the `evals` artifact of [this run]({run_url}).")
    return "\n".join(out)


def reply(body: str, run_url: str = "") -> str:
    """The whole answer to a comment: help or what's wrong with it, an estimate, or a run and its result."""
    try:
        request = parse(body)
    except ValueError as e:
        return f"{e}\n\n{HELP}"
    if request.estimate_only:
        return estimate_reply(request)
    before = set(RUNS.glob("*")) if RUNS.exists() else set()
    # Its report goes to the log (stderr), so that what this prints is only the reply.
    command = [sys.executable, "-m", "evals", *request.args()]
    done = subprocess.run(command, cwd=REPO, stdout=sys.stderr, check=False)
    new = sorted(set(RUNS.glob("*")) - before) if RUNS.exists() else []
    if not new or not (new[-1] / "summary.json").exists():
        if done.returncode == 3:
            return f"### Evals didn't run: estimated over the budget\n\n{estimate_reply(request)}"
        return f"### Evals didn't run\n\n`python -m evals` exited with status {done.returncode}; see the run's log."
    return result_reply(new[-1], request, done.returncode, run_url)


def main() -> None:
    server, repo, run = (os.environ.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    print(reply(sys.argv[1] if len(sys.argv) > 1 else "", f"{server}/{repo}/actions/runs/{run}" if run else ""))


if __name__ == "__main__":
    main()
