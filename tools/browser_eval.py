"""Runs the browser agent's tasks (evals/browser/tasks.toml) on the real sites, one at a time, and keeps what each
did: its answer, its steps, how long it took and what it cost. Nothing is ordered or booked: whatever the agent
asks to commit is approved as a dry run, which stops it there.

    uv run --all-extras python tools/browser_eval.py [--agent new|proto] [--task ID ...] [--steps N] [--cap DOLLARS]

--agent proto runs the first prototype (tools/browser_proto.py), on a copy of TARS's browser profile, as the
baseline. Every task's record goes to voice_data/browser/evals/<time>-<agent>/results.jsonl, with its transcript
beside it; what all runs have spent is kept in voice_data/browser/evals/spend.json, and no task starts once it
reaches --cap. Grading is by reading: a task's `check` says what a right answer is."""

import argparse
import contextlib
import io
import json
import re
import shutil
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "evals/browser/tasks.toml"
OUT = ROOT / "voice_data/browser/evals"
SPEND = OUT / "spend.json"
TURNS = re.compile(r"(?:done in|stuck after|stopped after) (\d+) steps")
PRICES = tomllib.loads((ROOT / "evals/prices.toml").read_text())["models"]


def cost(model: str, tokens_in: int, tokens_out: int) -> float:
    price = PRICES[model]
    return (tokens_in * price["input"] + tokens_out * price["output"]) / 1e6


def spent() -> float:
    return json.loads(SPEND.read_text())["dollars"] if SPEND.exists() else 0.0


def add_spend(dollars: float) -> None:
    SPEND.parent.mkdir(parents=True, exist_ok=True)
    SPEND.write_text(json.dumps({"dollars": round(spent() + dollars, 6)}))


class DryRun:
    """The person, for a run nobody is watching: every commit is approved, and none is carried out."""

    name = "eval"

    def __init__(self) -> None:
        self.asked: list[str] = []

    def approve(self, draft: str, *args, **kwargs) -> bool:
        self.asked.append(draft)
        return True

    def addresses(self) -> list[str]:
        return []

    def code(self, site: str) -> None:
        return None

    def add_login(self, *args, **kwargs) -> bool:
        return False

    def say(self, text: str) -> None:
        return None


def run_proto(task: dict, steps: int, folder: Path) -> dict:
    """The first prototype on one task, on its own copy of the profile, with the same guard as the new agent (its own
    missed "Complete booking", Hebrew, and Enter in a checkout field)."""
    sys.path.insert(0, str(ROOT / "tools"))
    import browser_order
    import browser_proto

    from voice_assistant.browser.gate import commits

    baseline = Path.home() / ".cache/tars-browser/profile-baseline"
    if not baseline.exists():
        shutil.copytree(browser_proto.PROFILE, baseline, symlinks=True)
        for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            (baseline / lock).unlink(missing_ok=True)
    browser_proto.PROFILE, browser_proto.PORT = baseline, 9334
    browser_proto.places_order = browser_order.places_order = commits
    person = DryRun()
    browser_proto.Person = lambda *args, **kwargs: person
    browser_proto.tailscale_address = lambda: "127.0.0.1"
    minds = []
    make_brain = browser_proto.brain

    def brain(*args, **kwargs):
        minds.append(make_brain(*args, **kwargs))
        return minds[-1]

    browser_proto.brain = brain
    act = browser_proto.act

    def guarded_act(page, steps_, failed=None):
        checkout = any(w in page.url.lower() for w in ("checkout", "cart", "basket", "payment", "book", "order"))
        if checkout:
            steps_ = [{k: v for k, v in s.items() if k != "enter"} if "type" in s else s for s in steps_]
        return act(page, steps_, failed)

    browser_proto.act = guarded_act
    printed = io.StringIO()
    started = time.perf_counter()
    error = ""
    try:
        with contextlib.redirect_stdout(printed):
            browser_proto.run(task["goal"], "luna", steps, headless=False)
    except Exception as e:  # noqa: BLE001 - a crash is a result like any other
        error = f"{type(e).__name__}: {e}"
    seconds = time.perf_counter() - started
    log = printed.getvalue()
    (folder / f"{task['id']}.log").write_text(log + (f"\nERROR {error}\n" if error else ""))
    mind = minds[-1] if minds else None
    tokens_in, tokens_out = (mind.tokens_in, mind.tokens_out) if mind else (0, 0)
    answer = log.split("\nsuccess: ", 1)[1].split("\n", 1)[1].strip() if "\nsuccess: " in log else ""
    ended = (
        "done" if "\ndone in " in log else "stuck" if "\nstuck after" in log else "error" if error else "out of turns"
    )
    return {
        "turns": int(found.group(1)) if (found := TURNS.search(log)) else steps,
        "seconds": round(seconds, 1),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "model": mind.model if mind else "",
        "ended": ended,
        "claimed": "success: True" in log,
        "answer": answer,
        "commits_asked": person.asked,
        "error": error,
    }


NOTES = "use"  # --notes: use (read and write), write (learn but don't read: first contact), or off


def run_new(task: dict, steps: int, folder: Path) -> dict:
    from voice_assistant.browser.notes import Notes
    from voice_assistant.browser.run import run_task

    from voice_assistant.browser.agent import Settings

    settings = Settings(turns=steps, read_notes=NOTES == "use")
    return run_task(task["goal"], DryRun(), trace_to=folder / f"{task['id']}.jsonl", tidy=True, settings=settings,
                    notes=None if NOTES == "off" else Notes())  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", choices=("new", "proto"), default="new")
    parser.add_argument("--task", action="append", help="a task's id (default: all)")
    parser.add_argument("--steps", type=int, default=25, help="at most this many model turns a task")
    parser.add_argument("--cap", type=float, default=14.0, help="no task starts once all runs have spent this")
    parser.add_argument("--label", default="", help="added to the run's folder name")
    parser.add_argument("--notes", choices=("use", "write", "off"), default="use",
                        help="the new agent's site notes: read and write them, only write them, or neither")  # fmt: skip
    args = parser.parse_args()
    global NOTES
    NOTES = args.notes
    tasks = tomllib.loads(TASKS.read_text())["task"]
    if args.task:
        unknown = set(args.task) - {t["id"] for t in tasks}
        if unknown:
            raise SystemExit(f"no such task: {', '.join(sorted(unknown))}")
        tasks = [t for t in tasks if t["id"] in args.task]
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    folder = OUT / f"{stamp}-{args.agent}{'-' + args.label if args.label else ''}"
    folder.mkdir(parents=True)
    runner = run_proto if args.agent == "proto" else run_new
    for task in tasks:
        if spent() >= args.cap:
            print(f"stopping: ${spent():.2f} spent, the cap is ${args.cap:.2f}")
            break
        print(f"{task['id']} ...", flush=True)
        result = runner(task, args.steps, folder)
        tokens_in = result["tokens_in"] + result.get("tidy_tokens_in", 0) + result.get("learn_tokens_in", 0)
        tokens_out = result["tokens_out"] + result.get("tidy_tokens_out", 0) + result.get("learn_tokens_out", 0)
        dollars = cost(result["model"], tokens_in, tokens_out) if result["model"] else 0.0
        add_spend(dollars)
        record = {"id": task["id"], "kind": task["kind"], "agent": args.agent, **result, "dollars": round(dollars, 4)}
        with (folder / "results.jsonl").open("a") as out:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(
            f"  {record['ended']}, {record['turns']} turns, {record['seconds']}s, ${dollars:.4f}"
            f" (all runs: ${spent():.2f})\n  {record['answer'][:300]}",
            flush=True,
        )
    print(f"\n{folder}")


if __name__ == "__main__":
    main()
