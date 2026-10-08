"""Could Jev (TypeSafe's decision model) route TARS's requests? Its picks and its speed, on capability_bench's requests.

    uv run python tools/router_bench.py              # every request, 6 times each
    uv run python tools/router_bench.py --runs 2

Jev doesn't write text: it answers a typed question with a probability for each option. Here the question is which
way a request should go: answered by the quick model, a reminder or a send (the quick model's tools), the web (OpenAI),
real thinking (<ponder>), or something TARS can't do. Each of capability_bench's 26 requests is asked 6 times, one at
a time on a kept-open connection (as TARS would), and the pick is checked against where the request belongs. Also:
how sure Jev was when right and when wrong, which is what a threshold for acting on its pick would rest on.
TYPESAFE_API_KEY in .env.
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from capability_bench import (
    ANSWER,
    CANCELLED,
    CASES,
    GROUPS,
    LOOK_UP,
    PONDER,
    REMIND,
    SENT,
    SNOOZED,
    SPEAKER,
    spread,
)

from voice_assistant.__main__ import api_key

REPO = Path(__file__).parents[1]
URL = "https://api.typesafe.ai/v1/systemone"
# Where each kind of request should go, as Jev's options. Descriptions follow TARS's own (llm.NEEDS_..., llm.CANT).
ROUTES = {
    "answer": "A reply TARS can say from general knowledge or the conversation: a fact, a joke, arithmetic, the "
    "time, small talk, or a simple how-to.",
    "reminders": "Setting, cancelling or putting off a timer, a reminder, or a message for someone in the house.",
    "send": "Sending, saving or sharing a note, a list, a recipe or a text file to the household's TARS page.",
    "web": "Anything current or live, which needs the web: the weather, news, sports results, prices or opening "
    "hours, or a real link to a web page.",
    "ponder": "Real thinking: planning something, comparing options, or working through several steps or a tricky "
    "calculation.",
    "cant": "Something TARS can't do at all: playing music or sounds, calling anyone, or controlling anything in the "
    "house, like lights.",
}
INSTRUCTIONS = (
    "This is a request to TARS, a voice assistant in someone's home; it may start with a [Speaker: name] tag. "
    "Which way should it go?"
)
# What each of capability_bench's expectations means as a route. "Can't" requests are answered by the quick model,
# so either pick is right for them.
RIGHT = {
    REMIND: {"reminders"},
    CANCELLED: {"reminders"},
    SNOOZED: {"reminders"},
    SENT: {"send"},
    LOOK_UP: {"web"},
    PONDER: {"ponder"},
    ANSWER: {"answer", "cant"},
}


def ask(client: httpx.Client, key: str, text: str) -> tuple[dict | None, float, str]:
    """(Jev's answer, seconds, error) for one request; tried again after a pause on 429 or 529, untimed."""
    body = {
        "state": SPEAKER + text,
        "model": "jev-latest",
        "questions": {"route": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": ROUTES}},
    }
    for wait in (2, 5, 10, 30):
        start = time.monotonic()
        r = client.post(URL, json=body, headers={"Authorization": f"Bearer {key}"})
        took = time.monotonic() - start
        if r.status_code in (429, 529):
            time.sleep(wait)
            continue
        if r.status_code != 200:
            return None, took, f"{r.status_code}: {r.text[:120]}"
        return r.json()["answers"]["route"], took, ""
    return None, 0.0, "still rate-limited"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=6, help="how many times to ask each (default 6)")
    args = parser.parse_args()
    key = api_key(REPO / ".env", "TYPESAFE_API_KEY")

    results = []  # (case, answer, seconds, error)
    with httpx.Client(timeout=10) as client:
        ask(client, key, "hello")  # open the connection, as TARS would on waking
        for case in CASES:
            for _ in range(args.runs):
                results.append((case, *ask(client, key, case.text)))

    groups: dict[str, list[int]] = {}
    right_conf, wrong_conf, times = [], [], []
    for case in CASES:
        runs = [r for r in results if r[0] is case]
        picks = [a["choice"] if a else f"error {e}" for _, a, _, e in runs]
        right = sum(1 for p in picks if p in RIGHT[case.want])
        tally = groups.setdefault(GROUPS[case.want], [0, 0])
        tally[0], tally[1] = tally[0] + right, tally[1] + len(runs)
        for (_, a, took, _), p in zip(runs, picks, strict=True):
            if a:
                times.append(took)
                (right_conf if p in RIGHT[case.want] else wrong_conf).append(a["confidence"])
        wrong = [p for p in picks if p not in RIGHT[case.want]]
        print(f"{'✓' if not wrong else '✗'} {right}/{len(runs)}  {case.text}  [{'/'.join(sorted(RIGHT[case.want]))}]")
        for p in dict.fromkeys(wrong):
            print(f"      {wrong.count(p)}x {p}")

    print("\nBy kind:")
    for group, (right, total) in groups.items():
        print(f"  {group:<10} {right}/{total}")
    median, p90 = spread(times)
    print(f"\nTime: {median:.2f} s median, {p90:.2f} s at the 90th percentile ({len(times)} requests)")
    if right_conf:
        print(f"Confidence when right: median {statistics.median(right_conf):.2f}, lowest {min(right_conf):.2f}")
    if wrong_conf:
        print(f"Confidence when wrong: median {statistics.median(wrong_conf):.2f}, highest {max(wrong_conf):.2f}")
    # Design B acts on a web or ponder pick alone, and only above a threshold: what each threshold would catch.
    for threshold in (0.8, 0.9, 0.95):
        early = [
            (c, a) for c, a, _, _ in results if a and a["choice"] in ("web", "ponder") and a["confidence"] >= threshold
        ]
        wrongly = sum(1 for c, a in early if a["choice"] not in RIGHT[c.want])
        should = sum(1 for c, _, _, _ in results if c.want in (LOOK_UP, PONDER))
        print(
            f"At {threshold:.2f}: {len(early) - wrongly} of {should} web and ponder requests sent on early, "
            f"{wrongly} sent on that shouldn't have been"
        )


if __name__ == "__main__":
    main()
