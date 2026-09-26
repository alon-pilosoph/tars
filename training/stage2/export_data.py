"""Export what "Retrain now" needs to retrain a setup's learned layer on the machine TARS runs on, without this folder.

    DATA/eval/.venv/bin/python -m training.stage2.export_data generic

Writes DATA/models/<setup>/hey_tars_check_data.npz (copy it next to the layer in the repo's models/<setup>/):
  train_*  the layer's own training examples, as the features train_check.py fits on (the same clips, conditions
           and weights), so a retrain starts from everything the layer learned and adds the household's wakes.
  test_*   the fixed test set a candidate must not do worse on, end to end: every held-out clip in every
           condition of training/eval/pipeline.py, streamed through the setup's wake model; where it fired, the
           features of the window the check heard. Plus every window it fired on in an hour of test TV and an hour
           of audiobooks. The generic setup's test uses no household recordings.
First it refits the layer from train_* and checks it matches the committed one, so the two can't drift apart.
About 30 minutes, most of it streaming the test audio.
"""

import json
import sys

import numpy as np

from training.common import (
    REPO,
    Layout,
    add_user_arg,
    bench_tools,
    log,
    parser,
    test_sets,
)
from training.eval.pipeline import CONDITIONS, stream
from training.stage2.train_check import Features, build_train

WINDOW_S = {"generic": 3.0, "personal": 2.5}  # the check window each setup runs with (config.toml)


def main():
    p = parser(__doc__)
    p.add_argument("setup", choices=list(WINDOW_S))
    p.add_argument("--threshold", type=float, default=0.5, help="the wake model's threshold")
    p.add_argument("--hours", type=float, default=1.0, help="hours of audiobooks")
    add_user_arg(p)
    args = p.parse_args()
    sys.path.insert(0, str(REPO / "src"))
    from sklearn.linear_model import LogisticRegression

    from voice_assistant.wake import MicroWakeWordTrigger

    layout = Layout(args.data)
    wb, _ = bench_tools(layout, args.user)
    folder = REPO / "models" / args.setup
    spec = json.loads((folder / "hey_tars_check.json").read_text())
    feat = Features(spec["phrases"])

    X, y, w = build_train(wb, feat, layout, args.setup, args.user, accent=True)
    clf = LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced").fit(X, y, sample_weight=w)
    drift = float(np.abs(clf.coef_[0].round(6) - np.array(spec["weights"])).max())
    if drift > 1e-5 or abs(round(float(clf.intercept_[0]), 6) - spec["bias"]) > 1e-5:
        sys.exit(f"The training examples don't reproduce {folder}/hey_tars_check.json (weights off by {drift}).")
    log(f"{len(y)} training examples reproduce the committed layer")

    sets = test_sets(layout, args.user)
    if args.setup == "generic":  # the generic layer never heard the household: neither does its test
        sets = {k: v for k, v in sets.items() if not k.startswith("your ")}
    wake = MicroWakeWordTrigger(str(folder / "hey_tars.tflite"), args.threshold)
    heard = []

    def keep(pcm):
        heard.append(feat(pcm))
        return True

    check = {"features": keep}
    rng = np.random.default_rng(0)
    banks = {n: wb.bank(n) for n in ["tv", "babble"]}
    rooms = wb.bank("rooms", limit=1000)
    rows = {"set": [], "cond": [], "should": [], "fired": [], "X": []}
    for cond, noise, snr, room in CONDITIONS:
        for name, (files, should) in sets.items():
            for f in files:
                c = wb.read_wav(f)
                c = wb.in_room(c, rooms[rng.integers(len(rooms))]) if room else c
                c = wb.mix(c, banks[noise][rng.integers(len(banks[noise]))], snr, rng) if noise else c
                pad = rng.normal(0, 40, 32000).astype(np.int16)
                heard.clear()
                fired, _ = stream(wake, np.concatenate([pad, c, pad]), check, WINDOW_S[args.setup])
                rows["set"].append(name)
                rows["cond"].append(cond)
                rows["should"].append(should)
                rows["fired"].append(fired > 0)
                rows["X"].append(heard[0] if heard else np.zeros(len(spec["weights"]), np.float32))
        log(f"{cond} done")

    long_name, long_X, hours = [], [], {}
    tv = np.concatenate([wb.read_wav(f) for f in sorted((wb.INTERFERENCE / "tv_hour").glob("*.wav"))])
    for label, audio in [("TV", tv), ("audiobooks", wb.long_speech(args.hours))]:
        heard.clear()
        stream(wake, audio, check, WINDOW_S[args.setup], stop_at_first=False)
        long_name += [label] * len(heard)
        long_X += heard
        hours[label] = len(audio) / 16000 / 3600
        log(f"{label}: the wake model fired {len(heard)} times in {hours[label]:.2f} h")

    out = layout.models / args.setup / "hey_tars_check_data.npz"
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out,
        train_X=X.astype(np.float32),
        train_y=y.astype(np.int8),
        train_w=w.astype(np.float32),
        test_set=np.array(rows["set"]),
        test_cond=np.array(rows["cond"]),
        test_should=np.array(rows["should"]),
        test_fired=np.array(rows["fired"]),
        test_X=np.array(rows["X"], np.float32),
        long_name=np.array(long_name, dtype=str),
        long_X=np.array(long_X, np.float32).reshape(-1, len(spec["weights"])),
        long_hours=json.dumps(hours),
    )
    log(f"wrote {out} (copy it to the repo's models/{args.setup}/)")


if __name__ == "__main__":
    main()
