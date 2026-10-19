"""What a run costs: the prices in evals/prices.toml applied to its usage, and an estimate before it starts, from what
the same scenarios used in earlier runs (their transcripts in evals/runs/)."""

import json
import statistics
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .scenarios import Scenario

PRICES = Path(__file__).parent / "prices.toml"
RUNS = Path(__file__).parent / "runs"
# What one run of each scenario used, on average, as of the last saved result (--save): for estimates where there are
# no transcripts to go on, like a fresh clone or CI.
PROFILE = Path(__file__).parent / "usage.json"
# Turns OpenAI answers, any of which may search the web: how searches are estimated from transcripts kept before runs
# counted them.
OPENAI_ROUTES = ("look-up", "fallback", "ponder")

type Use = dict[str, dict[str, float]]  # by model: requests, input, output, reasoning, searches


@dataclass
class Prices:
    models: dict[str, dict[str, float]]
    search: float
    checked: str

    def unpriced(self, use: Use) -> list[str]:
        """The models in `use` with no price set, so their tokens count as nothing."""
        priced = {m for m, p in self.models.items() if p.get("input") or p.get("output")}
        return sorted(set(use) - priced)


def load_prices(path: Path = PRICES) -> Prices:
    data = tomllib.loads(path.read_text())
    return Prices(data.get("models", {}), float(data.get("searches", {}).get("each", 0.0)), data.get("checked", "?"))


def cost(use: Use, prices: Prices, fast: bool = False) -> float:
    """Dollars, for `use`: tokens at each model's price (times its `fast` multiple on priority processing), and each
    web search at its own price."""
    total = 0.0
    for model, u in use.items():
        p = prices.models.get(model, {})
        tokens = u.get("input", 0) * p.get("input", 0.0) + u.get("output", 0) * p.get("output", 0.0)
        total += tokens / 1e6 * (p.get("fast", 1.0) if fast else 1.0) + u.get("searches", 0) * prices.search
    return total


def estimate(
    scenarios: list[Scenario], runs: int, folder: Path = RUNS, profile: Path = PROFILE
) -> tuple[Use, list[str]]:
    """What `runs` runs of each scenario will use, by model, from the latest runs of it on record (its transcripts, or
    else the saved profile): its own if it has any, else the average scenario's. Returns (the use, the scenarios with
    no runs on record)."""
    saved = json.loads(profile.read_text()) if profile.exists() else {}
    per_run, unknown = {}, []
    for s in scenarios:
        if rows := _latest_runs(s.id, folder):
            per_run[s.id] = _average(rows)
        elif s.id in saved:
            per_run[s.id] = saved[s.id]
        else:
            unknown.append(s.id)
    # A scenario never run costs what an average one does: of those picked, or else of every one on record, so a run
    # of only new scenarios isn't estimated at nothing.
    known = list(per_run.values()) or list(saved.values())
    typical = _average_use(known) if known else {}
    total: Use = {}
    for s in scenarios:
        for model, u in per_run.get(s.id, typical).items():
            row = total.setdefault(model, {})
            for k, v in u.items():
                row[k] = row.get(k, 0.0) + v * runs
    return total, unknown


def save_profile(by_scenario: dict[str, list[Use]], profile: Path = PROFILE) -> None:
    """Keeps what one run of each scenario used, on average over this result's runs, beside the ones not run."""
    saved = json.loads(profile.read_text()) if profile.exists() else {}
    for sid, uses in by_scenario.items():
        if uses:
            saved[sid] = {m: {k: round(v, 1) for k, v in u.items()} for m, u in _average_use(uses).items()}
    profile.write_text(json.dumps(dict(sorted(saved.items())), indent=1) + "\n")


def _latest_runs(scenario: str, folder: Path, keep: int = 10) -> list[dict]:
    rows = []
    for path in sorted(folder.glob("*/runs.jsonl"), reverse=True):
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            try:
                row = json.loads(line)
            except ValueError:
                if i == len(lines) - 1:
                    continue  # cut short: the run stopped while writing it
                raise
            if row["scenario"] == scenario and row.get("usage") and not row.get("unavailable"):
                rows.append(row)
        if len(rows) >= keep:
            break
    return rows[:keep]


def _average(rows: list[dict]) -> Use:
    """One run's use, on average over `rows`. Runs kept before searches were counted are given one search for each
    turn OpenAI answered."""
    uses = []
    for row in rows:
        use = {m: dict(u) for m, u in row["usage"].items()}
        openai = next((m for m in use if not m.startswith("qwen")), None)
        if openai and "searches" not in use[openai]:
            use[openai]["searches"] = sum(t["route"] in OPENAI_ROUTES for t in row["turns"])
        uses.append(use)
    return _average_use(uses)


def _average_use(uses: list[Use]) -> Use:
    models = {m for u in uses for m in u}
    keys = {k for u in uses for row in u.values() for k in row}
    return {m: {k: statistics.fmean(u.get(m, {}).get(k, 0) for u in uses) for k in keys} for m in models}
