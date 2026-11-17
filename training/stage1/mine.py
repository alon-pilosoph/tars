"""Hard-negative mining: a stage 1 model run over training audio with no "hey TARS" in it, keeping the 3.2 s around
every moment it wakes. Those are its own mistakes, for the next model to train on as negatives
(features.py negatives NAME --folder OUT). Only training audio is mined, never a test set.

    uv run --group training python -m training.stage1.mine MODEL.tflite OUT [--threshold 0.1] [--most 12000]

Default audio: LibriSpeech train-clean-100, MUSAN speech and music, the training TV and babble, AudioSet noise, FMA
music; --source mines one folder instead. The shipped recipe mines a first model (train generic --negative-weight 40)
at 0.1 into DATA/aug/mined (see README.md).
"""

from pathlib import Path

import numpy as np
import soundfile as sf

from training.common import REPO, SR, Layout, log, parser

BLOCK, CUT = 1280, int(3.2 * SR)
AFTER = int(0.4 * SR)  # the cut ends a little after the wake: the model fires near the end of the phrase
REFRACTORY = int(2.0 * SR / BLOCK)


def sources(layout: Layout, only: Path | None = None) -> list[Path]:
    if only:
        files = sorted(only.rglob("*.wav")) + sorted(only.rglob("*.flac"))
    else:
        files = sorted(layout.librispeech.rglob("*.flac"))
        aug, bg = layout.aug, layout.backgrounds
        for d in [aug / "musan/speech", aug / "musan/music", aug / "train/tv", aug / "train/babble",
                  bg / "audioset_16k", bg / "fma"]:  # fmt: skip
            files += sorted(d.rglob("*.wav"))
    np.random.default_rng(17).shuffle(files)
    return files


def main():
    import sys

    sys.path.insert(0, str(REPO / "src"))
    from voice_assistant.wake import MicroWakeWordTrigger

    p = parser(__doc__)
    p.add_argument("model", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--threshold", type=float, default=0.1)
    p.add_argument("--most", type=int, default=12000)
    p.add_argument("--source", type=Path, help="mine only this folder of audio")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    trigger = MicroWakeWordTrigger(str(args.model), args.threshold)
    found, seconds = len(list(args.out.glob("*.wav"))), 0.0
    for path in sources(Layout(args.data), args.source):
        if found >= args.most:
            break
        try:
            audio, sr = sf.read(str(path), dtype="int16", always_2d=True)
        except (RuntimeError, sf.LibsndfileError):
            continue
        if sr != SR:
            continue
        audio = audio.mean(axis=1).astype(np.int16)
        seconds += len(audio) / SR
        trigger.reset()
        until = -1
        for i in range(0, len(audio) - BLOCK + 1, BLOCK):
            if trigger.score(audio[i : i + BLOCK]) >= args.threshold and i // BLOCK > until:
                end = min(len(audio), i + BLOCK + AFTER)
                cut = audio[max(0, end - CUT) : end]
                if len(cut) == CUT:
                    sf.write(str(args.out / f"{found:05d}.wav"), cut, SR, subtype="PCM_16")
                    found += 1
                until = i // BLOCK + REFRACTORY
    log(f"done: {found} mined from {seconds / 3600:.1f} h ({found / max(seconds / 3600, 1e-6):.0f} an hour)")


if __name__ == "__main__":
    main()
