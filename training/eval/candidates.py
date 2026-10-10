"""Stage-1 wake models side by side, offline: per-block scores on the same audio, then a ranking at matched strictness.

    DATA/eval/.venv/bin/python -m training.eval.candidates score DATA/runs/shipped.tflite DATA/runs/<name>.tflite \
        [--user voice_data/<name>/laptop] [--rescore]
    DATA/eval/.venv/bin/python -m training.eval.candidates rank [--baseline shipped]

score: for each model, every clip's per-block scores (the wake model alone, no check) on
  panel   real speakers it never trained on (kNN-VC conversions into LibriSpeech and VCTK voices): 8 "hey TARS" and
          4 lookalikes per speaker, in clean, babble at 10 dB, and far room with TV at 10 dB;
  extras  with --user, the owner's "hey TARS" takes (both halves), the held-out OpenAI voices in the 8 test conditions,
          and an hour of test TV and one of audiobooks.
Scores are kept per block, so single models and averages of models can be compared without running them again.
A model already scored is skipped, unless --rescore; two models can't share a file name.
rank: every scored model at its own threshold, the lowest at which it lets through no more lookalikes (the panel's
and the held-out voices') and fires no more often on TV and audiobooks than the baseline at 0.5. Ranked by the share
of panel speakers with at least 80% of their "hey TARS" answered; the owner's takes are only reported.
Writes DATA/results/candidates/{panel,extras}/<model>.npz (scores, lengths, meta; extras also tv, books, tv_hours,
books_hours); rank prints a table.
"""

import sys
from pathlib import Path

import numpy as np

from training.audio import CONDITIONS, Interference, long_speech, quiet_room, read_wav
from training.common import REPO, Layout, add_user_arg, log, parser, user_split
from training.eval.pipeline import MUTE_BLOCKS

POSITIVE, LOOKALIKE = 8, 4
PANEL_CONDITIONS = [c for c in CONDITIONS if c[0] in ("clean", "babble_10dB", "far_room+tv_10dB")]
THRESHOLDS = np.round(np.arange(0.30, 0.96, 0.025), 3)
COLUMNS = ["speakers >=80%", "panel p10", "lookalikes", "fires/h", "others", "you train", "you test"]


def panel_clips(layout: Layout) -> list[tuple[str, str, Path]]:
    out = []
    for folder in (layout.vc, layout.vc_vctk):
        for kind, n in (("positive", POSITIVE), ("near_miss", LOOKALIKE)):
            by_speaker: dict[str, list[Path]] = {}
            for f in sorted((folder / kind).glob("*.wav")):
                by_speaker.setdefault(f.stem.rsplit("_", 1)[0], []).append(f)
            out += [(f"{folder.name}/{s}", kind, f) for s, files in sorted(by_speaker.items()) for f in files[:n]]
    return out


def extras_clips(layout: Layout, user: Path | None) -> list[tuple[str, str, Path]]:
    out = []
    for kind in ("hey_tars",) if user else ():
        train, test = user_split(user, kind)
        out += [(f"you:{kind}", "train", f) for f in train] + [(f"you:{kind}", "test", f) for f in test]
    held = layout.heldout
    out += [("others:hey_tars", "-", f) for f in sorted((held / "hey_tars").glob("*.wav"))]
    out += [("others:lookalikes", "-", f) for f in sorted((held / "hey_tars_near_miss").glob("*.wav"))]
    return out


def blocks(wake, audio: np.ndarray) -> np.ndarray:
    from voice_assistant.audio import BLOCK_SAMPLES

    wake.reset()
    return np.asarray(
        [wake.score(audio[i : i + BLOCK_SAMPLES]) for i in range(0, len(audio) - BLOCK_SAMPLES + 1, BLOCK_SAMPLES)],
        dtype=np.float16,
    )


def score_clips(wake, interference, conditions, todo) -> dict:
    rng = np.random.default_rng(0)
    scores, meta = [], []
    for condition in conditions:
        for group, half, f in todo:
            clip = interference.apply(read_wav(f), condition, rng)
            pad = quiet_room(rng)
            scores.append(blocks(wake, np.concatenate([pad, clip, pad])))
            meta.append((condition[0], group, half))
    return {
        "scores": np.concatenate(scores),
        "lengths": np.array([len(s) for s in scores]),
        "meta": np.array(meta),
    }


def score(args):
    sys.path.insert(0, str(REPO / "src"))
    from voice_assistant.wake import MicroWakeWordTrigger

    layout = Layout(args.data)
    if args.user is not None and not args.user.is_dir():
        raise SystemExit(f"--user {args.user} doesn't exist.")
    stems = [m.stem for m in args.models]
    if len(set(stems)) != len(stems):
        raise SystemExit("two models have the same file name; scores are kept under it")
    panel, extras = panel_clips(layout), extras_clips(layout, args.user)
    out_dir = layout.results / "candidates"
    for name in ("panel", "extras"):
        (out_dir / name).mkdir(parents=True, exist_ok=True)
    interference = Interference(layout.interference)
    tv = np.concatenate([read_wav(f) for f in sorted((layout.interference / "tv_hour").glob("*.wav"))])
    books = long_speech(layout.librispeech_test, 1.0)
    for model in args.models:
        wake = MicroWakeWordTrigger(str(model), 0.5)
        out = out_dir / "panel" / f"{model.stem}.npz"
        if args.rescore or not out.exists():
            result = score_clips(wake, interference, PANEL_CONDITIONS, panel)
            np.savez_compressed(out, **result)
            log(f"{model.stem}: panel, {len(result['lengths'])} clips")
        out = out_dir / "extras" / f"{model.stem}.npz"
        if args.rescore or not out.exists():
            result = score_clips(wake, interference, CONDITIONS, extras)
            np.savez_compressed(
                out,
                **result,
                tv=blocks(wake, tv),
                books=blocks(wake, books),
                tv_hours=len(tv) / 16000 / 3600,
                books_hours=len(books) / 16000 / 3600,
            )
            log(f"{model.stem}: extras, {len(result['lengths'])} clips")


def split(npz) -> list[np.ndarray]:
    return np.split(npz["scores"].astype(np.float32), np.cumsum(npz["lengths"])[:-1])


def events(seq: np.ndarray, t: float) -> int:
    n, i = 0, 0
    while i < len(seq):
        if seq[i] >= t:
            n, i = n + 1, i + MUTE_BLOCKS
        else:
            i += 1
    return n


class Model:
    def __init__(self, folder: Path, name: str):
        p, e = np.load(folder / "panel" / f"{name}.npz"), np.load(folder / "extras" / f"{name}.npz")
        self.p_peak = np.array([s.max() for s in split(p)])
        self.p_meta = p["meta"]
        self.e_peak = np.array([s.max() for s in split(e)])
        self.e_meta = e["meta"]
        self.tv, self.books = e["tv"].astype(np.float32), e["books"].astype(np.float32)
        self.hours = float(e["tv_hours"]) + float(e["books_hours"])

    def at(self, t: float) -> dict[str, float]:
        pf, ef = self.p_peak >= t, self.e_peak >= t
        pos = self.p_meta[:, 2] == "positive"
        speakers = sorted(set(self.p_meta[pos, 1]))
        per = np.array([pf[pos & (self.p_meta[:, 1] == s)].mean() for s in speakers])

        def e(group, half=None):
            m = self.e_meta[:, 1] == group
            m = m & (self.e_meta[:, 2] == half) if half else m
            return ef[m].mean() if m.any() else float("nan")

        return {
            "speakers >=80%": (per >= 0.8).mean(),
            "panel p10": np.percentile(per, 10),
            "lookalikes": (pf[self.p_meta[:, 2] == "near_miss"].mean() + e("others:lookalikes")) / 2,
            "fires/h": (events(self.tv, t) + events(self.books, t)) / self.hours,
            "others": e("others:hey_tars"),
            "you train": e("you:hey_tars", "train"),
            "you test": e("you:hey_tars", "test"),
        }


def rank(args):
    folder = Layout(args.data).results / "candidates"
    names = sorted(
        {f.stem for f in (folder / "panel").glob("*.npz")} & {f.stem for f in (folder / "extras").glob("*.npz")}
    )
    if args.baseline not in names:
        raise SystemExit(f"the baseline {args.baseline} isn't scored yet")
    base = Model(folder, args.baseline).at(0.5)
    rows = {f"{args.baseline} @0.50": base}
    for name in names:
        if name == args.baseline:
            continue
        m = Model(folder, name)
        for t in THRESHOLDS:
            r = m.at(t)
            if r["lookalikes"] <= base["lookalikes"] + 1e-9 and r["fires/h"] <= base["fires/h"] + 1e-9:
                rows[f"{name} @{t:.3f}"] = r
                break
    ranked = sorted((k for k in rows if not k.startswith(f"{args.baseline} @")), key=lambda k: -rows[k][COLUMNS[0]])
    print(f"wake model alone, each run at the threshold where it's as strict as {args.baseline}\n")
    shown = [f"{args.baseline} @0.50"] + ranked
    width = max(16, *(len(k) for k in shown)) + 1

    def cell(v: float, c: str) -> str:
        text = "—" if np.isnan(v) else f"{v:.1f}" if c == "fires/h" else f"{v:.0%}"
        return f"{text:>15}"

    print(f"{'model':{width}}" + "".join(f"{c[:14]:>15}" for c in COLUMNS))
    for k in shown:
        print(f"{k:{width}}" + "".join(cell(rows[k][c], c) for c in COLUMNS))
    print(f"\n{len(ranked)} of {len(names) - 1} runs reach {args.baseline}'s strictness at some threshold")


def main():
    p = parser(__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("score", help="save per-block scores of models on the panel and the extras")
    s.add_argument("models", nargs="+", type=Path)
    s.add_argument("--rescore", action="store_true", help="score again even if results exist")
    add_user_arg(s)
    r = sub.add_parser("rank", help="the matched-strictness table")
    r.add_argument("--baseline", default="shipped", help="the scored model each other is matched to, at 0.5")
    args = p.parse_args()
    {"score": score, "rank": rank}[args.command](args)


if __name__ == "__main__":
    main()
