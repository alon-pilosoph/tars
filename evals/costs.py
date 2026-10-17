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


def estimate(scenarios: list[Scenario], runs: int, folder: Path = RUNS) -> tuple[Use, list[str]]:
    """What `runs` runs of each scenario will use, by model, from the latest runs of it on record: its own if it has
    any, else the average scenario's. Returns (the use, the scenarios with no runs on record)."""
    per_run, unknown = {}, []
    for s in scenarios:
        rows = _latest_runs(s.id, folder)
        if rows:
            per_run[s.id] = _average(rows)
        else:
            unknown.append(s.id)
    typical = _average_use(list(per_run.values())) if per_run else {}
    total: Use = {}
    for s in scenarios:
        for model, u in per_run.get(s.id, typical).items():
            row = total.setdefault(model, {})
            for k, v in u.items():
                row[k] = row.get(k, 0.0) + v * runs
    return total, unknown


def _latest_runs(scenario: str, folder: Path, keep: int = 10) -> list[dict]:
    rows = []
    for path in sorted(folder.glob("*/runs.jsonl"), reverse=True):
        for line in path.read_text().splitlines():
            row = json.loads(line)
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
