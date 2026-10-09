"""End to end, the way the assistant runs: audio streams through the wake model in 80 ms blocks, and on a wake the check
hears the last few seconds. A clip counts as answered only when both say yes. Also false answers per hour on an hour
of test TV and on audiobooks (LibriSpeech test-clean).

    DATA/eval/.venv/bin/python -m training.eval.pipeline models/generic/hey_tars.tflite \
        --checks models/generic/hey_tars_check.json plain --window 3.0 [--user voice_data/<name>/laptop --all-user]
    DATA/eval/.venv/bin/python -m training.eval.pipeline DATA/models/generic/tars_stop.tflite --phrase tars_stop \
        --threshold 0.4 --window 3.0

A check is a learned layer (.json, loaded by the assistant's own PhraseVerifier) or "plain" (Vosk with the phrase
grammar and no learned layer). Every test clip gets 2 s of faint noise before and after it, as in a live stream.
Test sets: the held-out OpenAI voices and, with --user, the owner's recordings (the test half, or all of them with
--all-user for a setup that never trained on them), in 8 conditions.
Writes DATA/results/pipeline_<model>_t<threshold>_w<window>.json.
"""

import json
import sys
from pathlib import Path

import numpy as np

from training.audio import CONDITIONS, Interference, conditioned, long_speech, quiet_room, read_wav
from training.common import PHRASES, REPO, SR, Layout, add_user_arg, log, parser, test_sets

MUTE_BLOCKS = 25  # 2 s after a wake before another counts, so one phrase is one wake


def make_check(spec: str, phrase: str = "hey tars"):
    from voice_assistant.verify import PhraseVerifier

    verifier = PhraseVerifier(phrase, REPO / "models", None if spec == "plain" else Path(spec))
    return lambda pcm: verifier.check(pcm)[0]


def stream(wake, audio, checks, window_s, stop_at_first=True):
    """Returns (times the wake model fired, {check: how many of those wakes it answered})."""
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
    p.add_argument("--phrase", default="hey_tars", choices=PHRASES)
    p.add_argument(
        "--all-user", action="store_true", help="test on ALL the owner's clips (for setups that never saw them)"
    )
    add_user_arg(p)
    args = p.parse_args()
    if args.phrase != "hey_tars" and (args.user or args.all_user):
        p.error("the owner's takes are tested for hey_tars only")
    sys.path.insert(0, str(REPO / "src"))
    from voice_assistant.wake import MicroWakeWordTrigger

    layout = Layout(args.data)
    sets = test_sets(layout, args.user, args.all_user, args.phrase)
    wake = MicroWakeWordTrigger(str(args.wake_model), args.threshold)
    checks = {c: make_check(c, args.phrase.replace("_", " ")) for c in args.checks}
    rng = np.random.default_rng(0)
    rows = []
    log(f"{sum(len(files) for files, _ in sets.values())} clips in {len(CONDITIONS)} conditions")
    for cond, name, _should, clip in conditioned(sets, Interference(layout.interference), rng):
        pad = quiet_room(rng)
        fired, answered = stream(wake, np.concatenate([pad, clip, pad]), checks, args.window)
        rows.append({"cond": cond, "set": name, "fired": fired > 0, **{k: v > 0 for k, v in answered.items()}})
    long = {}
    tv = np.concatenate([read_wav(f) for f in sorted((layout.interference / "tv_hour").glob("*.wav"))])
    for label, audio in [("TV hour", tv), ("audiobooks", long_speech(layout.librispeech_test, args.hours))]:
        fired, answered = stream(wake, audio, checks, args.window, stop_at_first=False)
        hours = len(audio) / SR / 3600
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
