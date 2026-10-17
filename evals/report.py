"""What a set of runs adds up to: per scenario and per tag, with confidence intervals, first-word times by route and
tokens by model; and how it compares with a saved baseline. Nothing here prints or asks a model."""

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass

from .runner import RunResult
from .scenarios import Scenario

# A scenario has got worse when its share of runs right drops by at least this much from the baseline's: with 3 runs
# each, 3/3 to 1/3 or 2/3 to 0/3. One run either way (3/3 to 2/3) is within what the same code does from run to run.
WORSE_BY = 0.5


def wilson(passed: int, runs: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% Wilson score interval for `passed` of `runs`: honest at 0/n and n/n, where the plain one isn't."""
    if runs == 0:
        return 0.0, 1.0
    p = passed / runs
    centre = (p + z * z / (2 * runs)) / (1 + z * z / runs)
    spread = z * math.sqrt(p * (1 - p) / runs + z * z / (4 * runs * runs)) / (1 + z * z / runs)
    return max(0.0, centre - spread), min(1.0, centre + spread)


def summarize(scenarios: list[Scenario], results: list[RunResult]) -> dict:
    """The totals, as saved to results.jsonl. Runs still rate-limited, or refused for the account, aren't results:
    they count in `not_run` only."""
    by_id: dict[str, list[RunResult]] = defaultdict(list)
    for r in results:
        by_id[r.scenario].append(r)
    per_scenario, tags = {}, defaultdict(lambda: [0, 0])
    turns_ok = turns = not_run = 0
    first: dict[str, list[float]] = defaultdict(list)
    usage: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for s in scenarios:
        counted = [r for r in by_id[s.id] if r.counted]
        not_run += len(by_id[s.id]) - len(counted)
        passed = sum(r.ok for r in counted)
        per_scenario[s.id] = {"right": passed, "runs": len(counted), "fingerprint": s.fingerprint}
        for tag in s.tags:
            tags[tag][0] += passed
            tags[tag][1] += len(counted)
        for r in by_id[s.id]:
            for model, row in r.usage.items():
                for k, v in row.items():
                    usage[model][k] += v
        for r in counted:
            for t in r.turns:
                turns += 1
                turns_ok += not t.wrong
                if t.first_s is not None:
                    first[t.route].append(t.first_s)
    right = sum(v["right"] for v in per_scenario.values())
    runs = sum(v["runs"] for v in per_scenario.values())
    return {
        "scenarios": per_scenario,
        "right": [right, runs],
        "interval": [round(x, 3) for x in wilson(right, runs)],
        "turns": [turns_ok, turns],
        "not_run": not_run,
        "tags": {tag: pr for tag, pr in sorted(tags.items())},
        "first_words": {
            route: {"median": round(statistics.median(ts), 2), "p90": round(_p90(ts), 2), "turns": len(ts)}
            for route, ts in sorted(first.items())
        },
        "usage": {model: dict(row) for model, row in sorted(usage.items())},
    }


def _p90(times: list[float]) -> float:
    return statistics.quantiles(times, n=10)[-1] if len(times) >= 2 else times[0]


@dataclass
class Change:
    scenario: str
    before: tuple[int, int]
    after: tuple[int, int]
    verdict: str  # "worse", "better", "same", "changed" (its definition did: not comparable) or "new"


def compare(summary: dict, baseline: dict) -> list[Change]:
    """Each scenario against the baseline's run of it, when both ran the same definition."""
    changes = []
    for sid, now in summary["scenarios"].items():
        was = baseline.get("scenarios", {}).get(sid)
        after = (now["right"], now["runs"])
        if was is None:
            changes.append(Change(sid, (0, 0), after, "new"))
            continue
        before = (was["right"], was["runs"])
        if was.get("fingerprint") != now["fingerprint"]:
            changes.append(Change(sid, before, after, "changed"))
        elif not now["runs"] or not was["runs"]:
            changes.append(Change(sid, before, after, "same"))
        else:
            drop = was["right"] / was["runs"] - now["right"] / now["runs"]
            verdict = "worse" if drop >= WORSE_BY else "better" if drop <= -WORSE_BY else "same"
            changes.append(Change(sid, before, after, verdict))
    return changes


def gate(summary: dict, changes: list[Change], fail_under: float | None, no_regressions: bool) -> list[str]:
    """Why this run should fail a check (CI, or before merging), if it should: [] if it passes."""
    why = []
    right, runs = summary["right"]
    if fail_under is not None and runs and right / runs < fail_under:
        why.append(f"{right}/{runs} runs right is under {fail_under:.0%}")
    if no_regressions:
        why += [
            f"{c.scenario} got worse: {c.before[0]}/{c.before[1]} -> {c.after[0]}/{c.after[1]}"
            for c in changes
            if c.verdict == "worse"
        ]
    return why
