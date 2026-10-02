"""Train stage 2's learned layer: a logistic regression over how Vosk ranks every phrase it listens for.

    DATA/eval/.venv/bin/python -m training.stage2.train_check generic     # no household recordings
    DATA/eval/.venv/bin/python -m training.stage2.train_check personal    # + the owner's training half

Features per clip, computed by the assistant's own code (verify.TunedCheck): for each of the 20 phrases (the wake
phrase, "hey darts" and the lookalikes, all from verify.PHRASES), Vosk's best n-best confidence for an alternative
containing it, relative to its top guess (-40 if absent), plus one flag for "heard nothing or [unk]". Every training
clip goes through one random training condition (clean, TV or babble at 5/10/15 dB, a simulated room, a room plus TV
at 10 dB), using training interference only.
  generic:  6,000 wake phrases and 7,000 lookalikes from Kokoro, Piper voices, OpenAI, accented Piper voices,
            kNN-VC conversions into LibriSpeech and VCTK speakers (counted twice), and real LibriSpeech lookalikes
            (counted twice). --no-accent leaves out the accented and VCTK clips (3,000 and 3,500).
  personal: the owner's training half (wake phrases and lookalikes in every condition, weight 5; "TARS stop" and
            sentences, weight 3) plus 500 synthetic wake phrases and 700 synthetic lookalikes.
Then tests on held-out data (the owner's test half, the held-out OpenAI voices, test interference) and prints a
table per decision rule. A few minutes. Output: DATA/models/<setup>/hey_tars_check.json (the format the assistant
loads), DATA/check/<setup>.pkl, and the training rows themselves, DATA/check/<setup>_base.npz.
"""

import json
import pickle
import random
import sys
import time
from pathlib import Path

import numpy as np

from training.audio import Interference, conditioned, in_room, mix, read_wav
from training.common import REPO, SR, Layout, add_user_arg, log, parser, test_sets, user_split

sys.path.insert(0, str(REPO / "src"))  # this runs in DATA/eval/.venv, where the assistant isn't installed
from voice_assistant.verify import PHRASES, TunedCheck, ensure_model

THRESHOLD = 0.3
TRAIN_KINDS = ["clean", "tv_5", "tv_10", "tv_15", "babble_5", "babble_10", "babble_15", "room", "room_tv_10"]
ABOUT = {
    "generic": "Generic learned layer over Vosk's n-best scores for 'hey tars' vs lookalikes. Trained WITHOUT any "
    "user recordings: synthetic voices, accented Piper voices in 45 languages, kNN-VC conversions into "
    "251 LibriSpeech and 110 VCTK (accented) speakers, and real LibriSpeech lookalike words, with training "
    "noise. Made by training/stage2/train_check.py generic.",
    "personal": "Learned layer over Vosk's n-best scores for 'hey tars' vs lookalikes, trained on the owner's "
    "recordings (training half) + synthetic clips with training noise. Made by "
    "training/stage2/train_check.py personal.",
}


def check_phrases() -> list[str]:
    """A bare "hey" is in every grammar (see Features) but isn't scored."""
    spec = PHRASES["hey tars"]
    return spec["accept"] + [p for p in spec["lookalikes"] if p != "hey"]


class Features:
    """Vosk with the check's grammar, turned into the numbers the learned layer sees."""

    def __init__(self, phrases: list[str]):
        from vosk import Model, SetLogLevel

        SetLogLevel(-1)
        self.vosk = Model(str(ensure_model(REPO / "models")))
        self.spec = {"phrases": phrases, "extra_grammar": ["hey", "[unk]"], "max_alternatives": 8, "floor": -40.0}
        self.check = TunedCheck({**self.spec, "weights": [0.0] * (len(phrases) + 1), "bias": 0.0, "threshold": 0.5})

    def __call__(self, pcm: np.ndarray) -> np.ndarray:
        from vosk import KaldiRecognizer

        r = KaldiRecognizer(self.vosk, SR, self.check.grammar)
        r.SetMaxAlternatives(self.check.max_alternatives)
        r.AcceptWaveform(pcm.astype(np.int16).tobytes())
        return self.check.features(json.loads(r.FinalResult()).get("alternatives", [])).astype(np.float32)


class Training:
    """The training interference (DATA/aug: simulated rooms, babble and TV), loaded lazily."""

    def __init__(self, layout: Layout):
        aug = layout.aug
        self.tv, self.babble = sorted((aug / "train/tv").glob("*.wav")), sorted((aug / "train/babble").glob("*.wav"))
        self.rooms = sorted((aug / "train_rirs").glob("*.wav"))
        self._cache: dict[Path, np.ndarray] = {}

    def _load(self, path: Path) -> np.ndarray:
        if path not in self._cache:
            self._cache[path] = read_wav(path)
        return self._cache[path]

    def apply(self, clip: np.ndarray, kind: str, rng: np.random.Generator) -> np.ndarray:
        if kind == "clean":
            return clip
        if kind.startswith("room"):
            clip = in_room(clip, self._load(self.rooms[rng.integers(len(self.rooms))]))
            if kind == "room":
                return clip
        bank = self.tv if "tv" in kind else self.babble
        return mix(clip, self._load(bank[rng.integers(len(bank))]), int(kind.split("_")[-1]), rng)


def fit(X, y, w):
    from sklearn.linear_model import LogisticRegression

    return LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced").fit(X, y, sample_weight=w)


def layer(clf, feat: "Features", about: str) -> dict:
    """The learned layer in the format the assistant loads (models/*/hey_tars_check.json)."""
    return {
        "about": about,
        **feat.spec,
        "weights": clf.coef_[0].round(6).tolist(),
        "bias": round(float(clf.intercept_[0]), 6),
        "threshold": THRESHOLD,
    }


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
    files += sorted((layout.vc / kind).glob("*.wav")) * 2  # real speakers' voices count double
    if accent:
        files += sorted(layout.clip_dir("accent", "hey_tars", kind).glob("*.wav"))
        files += sorted((layout.vc_vctk / kind).glob("*.wav")) * 2
    if kind == "near_miss":
        files += sorted(layout.real_lookalikes.glob("*.wav")) * 2
    random.Random(seed).shuffle(files)
    return files[:n]


def build_train(feat, layout: Layout, setup: str, user, accent: bool):
    rng = np.random.default_rng(1)
    noise = Training(layout)
    X, y, w = [], [], []

    def add(files, label, weight, kinds):
        for f in files:
            clip = read_wav(f)
            for kind in kinds:
                X.append(feat(noise.apply(clip, kind, rng)))
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
            X.append(feat(noise.apply(read_wav(f), kind, rng)))
            y.append(label)
            w.append(1.0)
    return np.array(X), np.array(y), np.array(w)


def household_layer(layout: Layout, clips: Path) -> dict:
    """The generic layer's training rows (DATA/check/generic_base.npz) plus a household's clips/{positive,negative},
    each in every training condition at weight 5, as the personal layer weights the owner's."""
    with np.load(layout.check / "generic_base.npz") as base:
        X, y, w, phrases = list(base["X"]), list(base["y"]), list(base["w"]), [str(p) for p in base["phrases"]]
    feat = Features(phrases)
    rng = np.random.default_rng(1)
    noise = Training(layout)
    for kind, label in (("positive", 1), ("negative", 0)):
        for f in sorted((clips / kind).glob("*.wav")):
            clip = read_wav(f)
            for condition in TRAIN_KINDS:
                X.append(feat(noise.apply(clip, condition, rng)))
                y.append(label)
                w.append(5.0)
    clf = fit(np.array(X), np.array(y), np.array(w))
    return layer(
        clf,
        feat,
        "Learned layer over Vosk's n-best scores, trained on the generic layer's data plus this "
        "household's own wakes. Made by training.household.",
    )


def test(layout: Layout, sets, feat, clf, out_path) -> None:
    """Prints the share each decision rule accepts per set and condition."""
    rng = np.random.default_rng(0)
    rows = [
        {"cond": cond, "set": name, "should": should, "p": float(clf.predict_proba([feat(clip)])[0, 1])}
        for cond, name, should, clip in conditioned(sets, Interference(layout.interference), rng)
    ]
    out_path.write_text(json.dumps(rows))
    conds = list(dict.fromkeys(r["cond"] for r in rows))
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
    layout = Layout(args.data)
    if args.setup == "personal" and not (args.user and args.user.is_dir()):
        raise SystemExit("The personal layer needs the owner's recordings: pass --user.")
    phrases = check_phrases()
    feat = Features(phrases)
    t0 = time.time()
    X, y, w = build_train(feat, layout, args.setup, args.user, accent=not args.no_accent)
    log(f"{args.setup}: {len(y)} training examples ({y.sum()} positive) in {time.time() - t0:.0f}s")
    clf = fit(X, y, w)
    layout.check.mkdir(parents=True, exist_ok=True)
    # The training rows themselves: what training.hub hosts, and what a household's own wakes are added to.
    np.savez(
        layout.check / f"{args.setup}_base.npz",
        X=X.astype(np.float32),
        y=y.astype(np.int8),
        w=w.astype(np.float32),
        phrases=np.array(phrases),
    )
    with open(layout.check / f"{args.setup}.pkl", "wb") as f:
        pickle.dump(clf, f)
    out = layout.models / args.setup / "hey_tars_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(layer(clf, feat, ABOUT[args.setup]), indent=1))
    log(f"wrote {out} (copy it to the repo's models/{args.setup}/)")
    if not args.skip_test:
        test(layout, test_sets(layout, args.user), feat, clf, layout.check / f"{args.setup}_test.json")


if __name__ == "__main__":
    main()
