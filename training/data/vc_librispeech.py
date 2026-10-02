"""Convert clean synthetic clips into real LibriSpeech speakers' voices with kNN-VC.

    DATA/eval/.venv/bin/python -m training.data.vc_librispeech [--per-speaker 80]

For each of the 251 train-clean-100 speakers: a matching set from ~2.5 min of their real speech, then N wake-phrase
clips and N lookalikes, sampled from the cleaned lists (clean.py), converted to that voice. About an hour on the
development Mac at 4 threads (~11 clips/s). Resumable. Output: DATA/vc/{positive,near_miss}/<speaker>_<nnn>.wav
"""

import random
import time

from training.common import Layout, log, parser, write_wav

MATCH_SECONDS = 150
POSITIVE_MIX = {"piper_libritts": 0.40, "piper_voices": 0.20, "kokoro": 0.25, "openai": 0.05, "prosody": 0.10}
NEAR_MIX = {"piper_libritts": 0.40, "piper_voices": 0.25, "kokoro": 0.30, "openai": 0.05}


def load_knn_vc(threads: int):
    """kNN-VC from torch hub, with torchaudio.load swapped for soundfile (newer torchaudio needs torchcodec)."""
    import soundfile as sf
    import torch
    import torchaudio

    def load(path, normalize=True, **kw):
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        return torch.from_numpy(data.T.copy()), sr

    torchaudio.load = load
    torch.set_num_threads(threads)
    return torch.hub.load("bshall/knn-vc", "knn_vc", prematched=True, trust_repo=True, pretrained=True, device="cpu")


def convert(knn_vc, refs: list[str], todo: list[tuple], speaker: str) -> int:
    """`todo`: (kind, source, destination) tuples. Returns how many were written."""
    import numpy as np
    import soundfile as sf

    try:
        matching = knn_vc.get_matching_set(refs)
    except Exception as e:  # noqa: BLE001 - too little reference audio for this speaker
        log(f"  skipped speaker {speaker}: {e}")
        return 0
    done = 0
    for _kind, src, dst in todo:
        if sf.info(src).duration < 0.5:  # kNN-VC's loudness matching needs at least 0.4 s of audio
            continue
        try:
            wav = knn_vc.match(knn_vc.get_features(src), matching, topk=4).numpy()
        except Exception as e:  # noqa: BLE001 - an empty or silent source clip
            log(f"  skipped {src}: {e}")
            continue
        write_wav(dst, (np.clip(wav, -1, 1) * 32767).astype(np.int16))  # kNN-VC's output is 16 kHz
        done += 1
    return done


def pool(clean, kind: str, mix: dict) -> list[tuple[list[str], float]]:
    out = []
    for source, share in mix.items():
        lst = clean / f"{source}_{kind}.txt"
        paths = [p for p in lst.read_text().split("\n") if p] if lst.exists() else []
        out.append((paths, share))
    return out


def sample(pools, n, rng):
    picks = []
    for paths, share in pools:
        if paths:
            picks += rng.sample(paths, min(len(paths), max(1, round(n * share))))
    rng.shuffle(picks)
    return picks[:n]


def main():
    p = parser(__doc__)
    p.add_argument("--per-speaker", type=int, default=80)
    p.add_argument("--threads", type=int, default=4)
    args = p.parse_args()
    import soundfile as sf

    layout = Layout(args.data)
    knn_vc = load_knn_vc(args.threads)
    pos_pools, near_pools = pool(layout.clean, "positive", POSITIVE_MIX), pool(layout.clean, "near_miss", NEAR_MIX)
    for kind in ["positive", "near_miss"]:
        (layout.vc / kind).mkdir(parents=True, exist_ok=True)
    speakers = sorted(p for p in layout.librispeech.iterdir() if p.is_dir())
    t_start, done = time.time(), 0
    for si, spk in enumerate(speakers):
        rng = random.Random(f"vc-{spk.name}")
        jobs = [("positive", p) for p in sample(pos_pools, args.per_speaker, rng)] + [
            ("near_miss", p) for p in sample(near_pools, args.per_speaker, rng)
        ]
        jobs = [(k, p, layout.vc / k / f"{spk.name}_{i:03d}.wav") for i, (k, p) in enumerate(jobs)]
        todo = [j for j in jobs if not j[2].exists()]
        if not todo:
            continue
        refs, total = [], 0.0
        for f in sorted(spk.glob("*/*.flac")):
            refs.append(str(f))
            total += sf.info(str(f)).duration
            if total >= MATCH_SECONDS:
                break
        done += convert(knn_vc, refs, todo, spk.name)
        rate = done / (time.time() - t_start)
        left = (len(speakers) - si - 1) * 2 * args.per_speaker / max(rate, 1e-6)
        log(f"speaker {si + 1}/{len(speakers)} ({spk.name}) done; {rate:.1f} clips/s, ~{left / 3600:.1f} h left")


if __name__ == "__main__":
    main()
