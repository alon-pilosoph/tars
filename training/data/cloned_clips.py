"""Cloned Common Voice speakers as stage 1 training clips, for the large pair only.

    DATA/eval/.venv/bin/python -m training.data.cloned_clips RAW

RAW is the unpacked bundle from `clone_voices.ipynb` (positive/ and near_miss/). A wake phrase is kept only if the
plain Vosk check hears it as "hey TARS", and a lookalike only if it doesn't, so the clips the cloning got wrong don't
teach stage 1 the wrong thing. Training on these speakers uses them up as a test set for that model.
Output: DATA/clips/cloned/hey_tars/{positive,near_miss}/
"""

import shutil
from pathlib import Path

import numpy as np

from training.common import REPO, SR, Layout, log, parser


def main():
    p = parser(__doc__)
    p.add_argument("raw", type=Path, help="the unpacked bundle, with positive/ and near_miss/")
    args = p.parse_args()
    import soundfile as sf

    from voice_assistant.verify import PhraseVerifier

    layout = Layout(args.data)
    check = PhraseVerifier("hey tars", REPO / "models")
    pad = np.zeros(SR // 2, np.int16)
    for kind, keep_if in (("positive", True), ("near_miss", False)):
        out = layout.clip_dir("cloned", "hey_tars", kind)
        out.mkdir(parents=True, exist_ok=True)
        files = sorted((args.raw / kind).glob("*.wav"))
        kept = 0
        for f in files:
            accepted, _ = check.check(np.concatenate([pad, sf.read(f, dtype="int16")[0], pad]))
            if accepted == keep_if:
                shutil.copy(f, out / f.name)
                kept += 1
        log(f"{kind}: kept {kept} of {len(files)}")


if __name__ == "__main__":
    main()
