"""A training run's snapshots (train.py --snapshots N) made into streaming models, to choose the run's best moment on
real recordings rather than microWakeWord's synthetic validation.

    DATA/mww/.venv/bin/python -m training.stage1.export_snapshots NAME OUT_DIR [--every 2]

NAME is the run's --name (its folder trained_models/hey_tars_NAME). Writes OUT_DIR/NAME-s<step>.tflite and
-s<step>-int8.tflite for every `--every`-th snapshot. --average K also makes the average of the last K snapshots'
weights (stochastic weight averaging: one flatter model instead of whichever moment training stopped at), as
NAME-savg<K>.tflite. Uses microWakeWord's own conversion (model_train_eval with
--train 0), one snapshot at a time.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import h5py
import numpy as np

from training.common import Layout, log, parser

SMALL = [
    "mixednet", "--pointwise_filters", "64,64,64,64", "--repeat_in_block", "1, 1, 1, 1", "--mixconv_kernel_sizes",
    "[5], [7,11], [9,15], [23]", "--residual_connection", "0,0,0,0", "--first_conv_filters", "32",
    "--first_conv_kernel_size", "5", "--stride", "3",
]  # fmt: skip


def average(snaps: list[Path], out: Path) -> Path:
    """The element-wise mean of several weight files of the same model (batch-norm statistics included)."""
    shutil.copy(snaps[0], out)
    with h5py.File(out, "r+") as dst:
        others = [h5py.File(s, "r") for s in snaps[1:]]

        def visit(name, obj):
            if isinstance(obj, h5py.Dataset) and obj.dtype.kind == "f":
                obj[...] = np.mean([obj[...], *(o[name][...] for o in others)], axis=0)

        dst.visititems(visit)
        for o in others:
            o.close()
    return out


def main():
    p = parser(__doc__)
    p.add_argument("name")
    p.add_argument("out", type=Path)
    p.add_argument("--every", type=int, default=1)
    p.add_argument("--average", type=int, default=0)
    p.add_argument("--phrase", default="hey_tars")
    args = p.parse_args()
    layout = Layout(args.data)
    train_dir = layout.trained_models / f"{args.phrase}_{args.name}"
    cfg = layout.trained_models / f"{args.phrase}_{args.name}.yaml"
    every = sorted((train_dir / "snapshots").glob("step_*.weights.h5"))
    snaps = [s for s in every if s.name.split("_")[1].split(".")[0].isdigit()][:: args.every]
    if args.average:
        snaps.append(average(snaps[-args.average :], train_dir / "snapshots" / f"step_avg{args.average}.weights.h5"))
    args.out.mkdir(parents=True, exist_ok=True)
    for snap in snaps:
        step = snap.name.split("_")[1].split(".")[0]
        out = args.out / f"{args.name}-s{step}.tflite"
        if out.exists():
            continue
        log(f"snapshot {step}")
        subprocess.run(
            [sys.executable, "-m", "microwakeword.model_train_eval", f"--training_config={cfg}", "--train", "0",
             "--test_tf_nonstreaming", "0", "--test_tflite_nonstreaming", "0",
             "--test_tflite_nonstreaming_quantized", "0", "--test_tflite_streaming", "1",
             "--test_tflite_streaming_quantized", "1", "--use_weights", f"snapshots/step_{step}", *SMALL],
            check=True, cwd=layout.mww, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        shutil.copy(train_dir / "tflite_stream_state_internal/stream_state_internal.tflite", out)
        quant = train_dir / "tflite_stream_state_internal_quant/stream_state_internal_quant.tflite"
        if quant.exists():
            shutil.copy(quant, out.with_name(f"{args.name}-s{step}-int8.tflite"))


if __name__ == "__main__":
    main()
