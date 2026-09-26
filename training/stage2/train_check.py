"""Train stage 2's learned layer: a logistic regression over how Vosk ranks every phrase it listens for.

    DATA/eval/.venv/bin/python -m training.stage2.train_check generic     # no household recordings
    DATA/eval/.venv/bin/python -m training.stage2.train_check personal    # + the owner's training half

Features per clip, computed by the assistant's own code (verify.TunedCheck): for each of the 20 phrases, Vosk's best
n-best confidence for an alternative containing it, relative to its top guess (-40 if absent), plus one flag for
"heard nothing or [unk]". Every training clip goes through one random training condition (clean, TV or babble at
5/10/15 dB, a simulated room, a room plus TV at 10 dB), using training interference only.
  generic:  6,000 wake phrases and 7,000 lookalikes from Kokoro, Piper voices, OpenAI, accented Piper voices,
            kNN-VC conversions into LibriSpeech and VCTK speakers (counted twice), and real LibriSpeech lookalikes
            (counted twice). --no-accent leaves out the accented and VCTK clips (3,000 and 3,500).
  personal: the owner's training half (wake phrases and lookalikes in every condition, weight 5; "TARS stop" and
            sentences, weight 3) plus 500 synthetic wake phrases and 700 synthetic lookalikes.
Then tests on held-out data (the owner's test half, the held-out OpenAI voices, test interference) and prints a
table per decision rule. A few minutes. Output: DATA/models/<setup>/hey_tars_check.json (the format the assistant
loads) and DATA/check/<setup>.pkl.
"""

import json
import pickle
import random
import sys
import time

import numpy as np

from training.common import (
    REPO,
    Layout,
    add_user_arg,
    bench_tools,
    parser,
    test_sets,
    user_split,
)

THRESHOLD = 0.3
TRAIN_KINDS = ["clean", "tv_5", "tv_10", "tv_15", "babble_5", "babble_10", "babble_15", "room", "room_tv_10"]
ABOUT = {
    "generic": "Generic learned layer over Vosk's n-best scores for 'hey tars' vs lookalikes. Trained WITHOUT any "
    "user recordings: synthetic voices, accented Piper voices in ~50 languages, kNN-VC conversions into "
    "250 LibriSpeech and 110 VCTK (accented) speakers, and real LibriSpeech lookalike words, with training "
    "noise. Made by training/stage2/train_check.py generic.",
    "personal": "Learned layer over Vosk's n-best scores for 'hey tars' vs lookalikes, trained on the owner's "
    "recordings (training half) + synthetic clips with training noise. Made by "
    "training/stage2/train_check.py personal.",
}


class Features:
    """Vosk with the check's grammar, turned into the numbers the learned layer sees."""

    def __init__(self, phrases: list[str]):
        from vosk import Model, SetLogLevel

        sys.path.insert(0, str(REPO / "src"))
        from voice_assistant.verify import TunedCheck

        SetLogLevel(-1)
        self.vosk = Model(str(REPO / "models" / "vosk-model-small-en-us-0.15"))
        self.spec = {"phrases": phrases, "extra_grammar": ["hey", "[unk]"], "max_alternatives": 8, "floor": -40.0}
        self.check = TunedCheck({**self.spec, "weights": [0.0] * (len(phrases) + 1), "bias": 0.0, "threshold": 0.5})

    def __call__(self, pcm: np.ndarray) -> np.ndarray:
        from vosk import KaldiRecognizer

        r = KaldiRecognizer(self.vosk, 16000, self.check.grammar)
        r.SetMaxAlternatives(self.check.max_alternatives)
        r.AcceptWaveform(pcm.astype(np.int16).tobytes())
        return self.check.features(json.loads(r.FinalResult()).get("alternatives", [])).astype(np.float32)


def augment(wb, clip, rng, kind, tv, babble, rooms, cache):
    def load(path):
        if path not in cache:
            cache[path] = wb.read_wav(path)
        return cache[path]

    if kind == "clean":
        return clip
    if kind.startswith("room"):
        clip = wb.in_room(clip, load(rooms[rng.integers(len(rooms))]))
        if kind == "room":
            return clip
    bank = tv if "tv" in kind else babble
    snr = int(kind.split("_")[-1])
    return wb.mix(clip, load(bank[rng.integers(len(bank))]), snr, rng)


def synthetic(layout: Layout, kind: str, n: int, seed: int) -> list:
    files = []
    for src in ["kokoro", "piper_voices", "openai"]:
        files += sorted(layout.clip_dir(src, "hey_tars", kind).glob("*.wav"))
    random.Random(seed).shuffle(files)
    return files[:n]


def generic_pool(layout: Layout, kind: str, n: int, seed: int, accent: bool) -> list:
    files = []
    for src in ["kokoro", "piper_voices", "openai"]:
        files += sorted(layout.clip_dir(src, "hey_tars", kind).glob("*.wav"))
    files += sorted((layout.vc / kind).glob("*.wav")) * 2  # real voices: count double
    if accent:
        files += sorted((layout.clips / "accent/hey_tars" / kind).glob("*.wav"))
        files += sorted((layout.vc_vctk / kind).glob("*.wav")) * 2
    if kind == "near_miss":
        files += sorted(layout.real_lookalikes.glob("*.wav")) * 2
    random.Random(seed).shuffle(files)
    return files[:n]


def build_train(wb, feat, layout: Layout, setup: str, user, accent: bool):
    rng = np.random.default_rng(1)
    aug = layout.aug
    tv, babble = sorted((aug / "train/tv").glob("*.wav")), sorted((aug / "train/babble").glob("*.wav"))
    rooms = sorted((aug / "train_rirs").glob("*.wav"))
    cache = {}
    X, y, w = [], [], []

    def add(files, label, weight, kinds):
        for f in files:
            clip = wb.read_wav(f)
            for kind in kinds:
                X.append(feat(augment(wb, clip, rng, kind, tv, babble, rooms, cache)))
                y.append(label)
                w.append(weight)

    if setup == "generic":
        n = 2 if accent else 1
        pools = [
            (generic_pool(layout, "positive", 3000 * n, 2, accent), 1),
            (generic_pool(layout, "near_miss", 3500 * n, 3, accent), 0),
        ]
    else:
        add(user_split(user, "hey_tars")[0], 1, 5.0, TRAIN_KINDS)
        add(user_split(user, "hey_tars_lookalikes")[0], 0, 5.0, TRAIN_KINDS)
        add(user_split(user, "tars_stop")[0], 0, 3.0, ["clean", "tv_10", "babble_10"])
        add(user_split(user, "speech")[0], 0, 3.0, ["clean", "tv_10", "babble_10"])
        pools = [(synthetic(layout, "positive", 500, 2), 1), (synthetic(layout, "near_miss", 700, 3), 0)]
    for files, label in pools:
        for f in files:
            kind = TRAIN_KINDS[rng.integers(len(TRAIN_KINDS))]
            X.append(feat(augment(wb, wb.read_wav(f), rng, kind, tv, babble, rooms, cache)))
            y.append(label)
            w.append(1.0)
    return np.array(X), np.array(y), np.array(w)


def test(wb, vb, sets, feat, clf, out_path) -> None:
    """Held-out data only, in every test condition; prints the share each rule accepts per set and condition."""
    conditions = vb.CONDITIONS + [
        ("tv_15dB", "tv", 15, False),
        ("babble_15dB", "babble", 15, False),
        ("tv_10dB", "tv", 10, False),
        ("babble_10dB", "babble", 10, False),
    ]
    rng = np.random.default_rng(0)
    banks = {n: wb.bank(n) for n in ["tv", "babble"]}
    rooms = wb.bank("rooms", limit=1000)
    rows = []
    for cond, noise, snr, room in conditions:
        for name, (files, should) in sets.items():
            for f in files:
                c = wb.read_wav(f)
                c = wb.in_room(c, rooms[rng.integers(len(rooms))]) if room else c
                c = wb.mix(c, banks[noise][rng.integers(len(banks[noise]))], snr, rng) if noise else c
                p = float(clf.predict_proba([feat(c)])[0, 1])
                rows.append({"cond": cond, "set": name, "should": should, "p": p})
    out_path.write_text(json.dumps(rows))
    conds = [c for c, *_ in conditions]
    for rule in [0.3, 0.5, 0.7]:
        print(f"\nrule: confidence >= {rule}")
        print(f"{'':<28}" + "".join(f"{c[:11]:>12}" for c in conds))
        for name in sets:
            cells = [np.mean([r["p"] >= rule for r in rows if r["set"] == name and r["cond"] == c]) for c in conds]
            print(f"{name:<28}" + "".join(f"{v:>12.0%}" for v in cells))


def main():
    p = parser(__doc__)
    p.add_argument("setup", choices=["generic", "personal"])
    p.add_argument("--no-accent", action="store_true", help="generic without the accented and VCTK clips")
    p.add_argument("--skip-test", action="store_true")
    add_user_arg(p)
    args = p.parse_args()
    from sklearn.linear_model import LogisticRegression

    layout = Layout(args.data)
    wb, vb = bench_tools(layout, args.user)
    if args.setup == "personal" and not args.user.exists():
        sys.exit(f"The personal layer needs the owner's recordings; {args.user} doesn't exist.")
    phrases = ["hey tars", "hey darts"] + vb.LOOKALIKES
    feat = Features(phrases)
    t0 = time.time()
    X, y, w = build_train(wb, feat, layout, args.setup, args.user, accent=not args.no_accent)
    print(f"{args.setup}: {len(y)} training examples ({y.sum()} positive) in {time.time() - t0:.0f}s", flush=True)
    clf = LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced").fit(X, y, sample_weight=w)
    layout.check.mkdir(parents=True, exist_ok=True)
    with open(layout.check / f"{args.setup}.pkl", "wb") as f:
        pickle.dump(clf, f)
    spec = {
        "about": ABOUT[args.setup],
        **feat.spec,
        "weights": clf.coef_[0].round(6).tolist(),
        "bias": round(float(clf.intercept_[0]), 6),
        "threshold": THRESHOLD,
    }
    out = layout.models / args.setup / "hey_tars_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=1))
    print(f"wrote {out} (copy it to the repo's models/{args.setup}/)", flush=True)
    if not args.skip_test:
        test(wb, vb, test_sets(layout, args.user), feat, clf, layout.check / f"{args.setup}_test.json")


if __name__ == "__main__":
    main()
