"""End to end, the way the assistant runs: audio streams through the wake model in 80 ms blocks, and on a wake the check
hears the last few seconds. A clip counts as answered only when both say yes. Also false answers per hour on an hour
of test TV and on audiobooks (LibriSpeech test-clean).

    DATA/eval/.venv/bin/python -m training.eval.pipeline models/generic/hey_tars.tflite \
        --checks models/generic/hey_tars_check.json plain --window 3.0 --all-user

A check is a learned layer (.json, loaded by the assistant's own PhraseVerifier) or "plain" (Vosk with the phrase
grammar and no learned layer). Every test clip gets 2 s of faint noise before and after it, as in a live stream.
Test sets: the owner's recordings (test half, or all of them with --all-user for a setup that never trained on
them) and the held-out OpenAI voices, in 8 conditions. Writes DATA/results/pipeline_<model>_t<threshold>_w<window>.json.
"""

import json
import sys
from pathlib import Path

import numpy as np

from training.common import REPO, Layout, add_user_arg, bench_tools, parser, test_sets

CONDITIONS = [
    ("clean", None, None, False),
    ("tv_5dB", "tv", 5, False),
    ("babble_5dB", "babble", 5, False),
    ("far_room+tv_10dB", "tv", 10, True),
    ("tv_15dB", "tv", 15, False),
    ("babble_15dB", "babble", 15, False),
    ("tv_10dB", "tv", 10, False),
    ("babble_10dB", "babble", 10, False),
]
MUTE_BLOCKS = 25  # after a wake, 2 s before the next one counts: one phrase is one wake


def make_check(spec: str):
    from voice_assistant.verify import PhraseVerifier

    verifier = PhraseVerifier("hey tars", REPO / "models", None if spec == "plain" else Path(spec))
    return lambda pcm: verifier.check(pcm)[0]


def stream(wake, audio, checks, window_s, stop_at_first=True):
    """Returns how often the wake model fired, and per check how many of those wakes it answered."""
    from voice_assistant.audio import BLOCK_SAMPLES
    from voice_assistant.verify import RecentAudio

    wake.reset()
    recent = RecentAudio(window_s)
    answered = {name: 0 for name in checks}
    fired, mute = 0, 0
    for i in range(0, len(audio) - BLOCK_SAMPLES + 1, BLOCK_SAMPLES):
        block = audio[i : i + BLOCK_SAMPLES]
        recent.add(block)
        score = wake.score(block)
        if mute:
            mute -= 1
            continue
        if score >= wake.threshold:
            fired += 1
            wake.reset()
            mute = MUTE_BLOCKS
            pcm = recent.audio()
            for name, check in checks.items():
                answered[name] += bool(check(pcm))
            if stop_at_first:
                break
    return fired, answered


def main():
    p = parser(__doc__)
    p.add_argument("wake_model", type=Path)
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--checks", nargs="+", default=["plain"])
    p.add_argument("--hours", type=float, default=1.0, help="hours of audiobooks for false answers per hour")
    p.add_argument("--window", type=float, default=3.0, help="seconds of audio the check hears")
    p.add_argument(
        "--all-user", action="store_true", help="test on ALL the owner's clips (for setups that never saw them)"
    )
    add_user_arg(p)
    args = p.parse_args()
    sys.path.insert(0, str(REPO / "src"))
    from voice_assistant.wake import MicroWakeWordTrigger

    layout = Layout(args.data)
    wb, _ = bench_tools(layout, args.user)
    sets = test_sets(layout, args.user, args.all_user)
    wake = MicroWakeWordTrigger(str(args.wake_model), args.threshold)
    checks = {c: make_check(c) for c in args.checks}
    rng = np.random.default_rng(0)
    banks = {n: wb.bank(n) for n in ["tv", "babble"]}
    rooms = wb.bank("rooms", limit=1000)
    rows = []
    for cond, noise, snr, room in CONDITIONS:
        for name, (files, _should) in sets.items():
            for f in files:
                c = wb.read_wav(f)
                c = wb.in_room(c, rooms[rng.integers(len(rooms))]) if room else c
                c = wb.mix(c, banks[noise][rng.integers(len(banks[noise]))], snr, rng) if noise else c
                pad = rng.normal(0, 40, 32000).astype(np.int16)  # a quiet room before and after
                fired, answered = stream(wake, np.concatenate([pad, c, pad]), checks, args.window)
                rows.append({"cond": cond, "set": name, "fired": fired > 0, **{k: v > 0 for k, v in answered.items()}})
        print(f"{cond} done", flush=True)
    long = {}
    tv = np.concatenate([wb.read_wav(f) for f in sorted((wb.INTERFERENCE / "tv_hour").glob("*.wav"))])
    for label, audio in [("TV hour", tv), ("audiobooks", wb.long_speech(args.hours))]:
        fired, answered = stream(wake, audio, checks, args.window, stop_at_first=False)
        hours = len(audio) / 16000 / 3600
        long[label] = {
            "wake model fired /h": round(fired / hours, 1),
            **{k: round(v / hours, 1) for k, v in answered.items()},
        }
    layout.results.mkdir(parents=True, exist_ok=True)
    out = layout.results / f"pipeline_{args.wake_model.stem}_t{args.threshold}_w{args.window}.json"
    out.write_text(json.dumps({"rows": rows, "long": long, "checks": args.checks}))
    conds = [c for c, *_ in CONDITIONS]
    for col in ["fired"] + args.checks:
        title = "wake model alone" if col == "fired" else f"answered with {col}"
        print(f"\n{args.wake_model} @ {args.threshold}: {title}")
        print(f"{'':<28}" + "".join(f"{c[:11]:>12}" for c in conds))
        for s in sets:
            print(
                f"{s:<28}"
                + "".join(
                    f"{np.mean([r[col] for r in rows if r['set'] == s and r['cond'] == c]):>12.0%}" for c in conds
                )
            )
    print("\nfalse answers per hour:", json.dumps(long, indent=1))


if __name__ == "__main__":
    main()
