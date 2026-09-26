"""kNN-VC into VCTK's 110 real speakers (many English accents), for stage 2's generic layer.

    DATA/eval/.venv/bin/python -m training.data.vc_vctk [--per-speaker 60]

Needs prep_vctk.py first. Sources are NOT cleaned (cleaning taught a crisp "TARS"): accented Piper clips, Kokoro and
Piper voices. Resumable. Output: DATA/vc_vctk/{positive,near_miss}/<speaker>_<nnn>.wav
"""

import random
import time

from training.common import Layout, parser
from training.data.vc_librispeech import MATCH_SECONDS, convert, load_knn_vc


def main():
    p = parser(__doc__)
    p.add_argument("--per-speaker", type=int, default=60)
    p.add_argument("--threads", type=int, default=4)
    args = p.parse_args()
    import soundfile as sf

    layout = Layout(args.data)
    sources = {
        kind: [
            layout.clips / "accent/hey_tars" / kind,
            layout.clip_dir("kokoro", "hey_tars", kind),
            layout.clip_dir("piper_voices", "hey_tars", kind),
        ]
        for kind in ["positive", "near_miss"]
    }
    knn_vc = load_knn_vc(args.threads)
    pools = {k: [sorted(str(p) for p in d.glob("*.wav")) for d in dirs] for k, dirs in sources.items()}
    for kind in pools:
        (layout.vc_vctk / kind).mkdir(parents=True, exist_ok=True)
    speakers = sorted(p for p in layout.vctk.iterdir() if p.is_dir())
    t0, done = time.time(), 0
    for si, spk in enumerate(speakers):
        rng = random.Random(f"vctk-{spk.name}")
        jobs = []
        for kind, lists in pools.items():
            picks = [rng.choice(lst) for lst in lists if lst for _ in range(args.per_speaker // len(lists) + 1)]
            rng.shuffle(picks)
            jobs += [
                (kind, p, layout.vc_vctk / kind / f"{spk.name}_{i:03d}.wav")
                for i, p in enumerate(picks[: args.per_speaker])
            ]
        todo = [j for j in jobs if not j[2].exists()]
        if not todo:
            continue
        refs, total = [], 0.0
        for f in sorted(spk.glob("*.wav")):
            refs.append(str(f))
            total += sf.info(f).duration
            if total >= MATCH_SECONDS:
                break
        done += convert(knn_vc, refs, todo, spk.name)
        print(
            f"[{time.strftime('%H:%M:%S')}] speaker {si + 1}/{len(speakers)} ({spk.name}) done; "
            f"{done / (time.time() - t0):.1f} clips/s",
            flush=True,
        )


if __name__ == "__main__":
    main()
