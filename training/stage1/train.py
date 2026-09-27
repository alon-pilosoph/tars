"""Train stage 1, the microWakeWord "hey TARS" model, with the recipe the experiments settled on ("D").

    DATA/mww/.venv/bin/python -m training.stage1.train generic       # never hears the owner: models/generic
    DATA/mww/.venv/bin/python -m training.stage1.train personal      # adds the owner's training half: models/personal
    DATA/mww/.venv/bin/python -m training.stage1.train household --run RUN   # adds a household's wakes

Recipe D: 20k steps, learning rate 0.001, negative class weight 20, SpecAugment (2 time masks of up to 5 frames,
2 frequency masks of up to 3 bins), batch 128, 1.5 s clips. The checkpoint kept is the one with the best average
recall among those under 0.5 false accepts per hour of ambient audio. The model is a MixedNet (4 blocks of 64
filters, kernels [5], [7,11], [9,15], [23], stride 3). Both setups use the uncleaned synthetic data.
  generic:  + real LibriSpeech lookalikes (weight 2)
  personal: + the owner's training half (positives weight 1.5, sentences weight 2)
  household: generic + a household's real wakes and missed "hey TARS" (weight 1.5) and what wasn't for TARS
             (weight 2), as training.household sets them out; the same weights as the owner's in personal
20 to 45 minutes on the development Mac at 4 threads. Needs features.py synthetic (and user, for personal).
Output: DATA/models/<setup>/hey_tars.tflite (household: DATA/household/<run>/pair/), the full-precision streaming
model (float32 in and out, 69 KB).
"""

import os
import shutil
import subprocess
import sys

import yaml

from training.common import Layout, log, parser

POSITIVE_WEIGHTS = {"piper_libritts": 1.0, "piper_voices": 1.5, "kokoro": 1.5, "openai": 0.75, "prosody": 1.5}
NEAR_MISS_WEIGHTS = {"piper_libritts": 3.0, "piper_voices": 3.0, "kokoro": 3.0, "openai": 1.0}
NEGATIVES = {
    "speech": 10.0,
    "dinner_party": 10.0,
    "no_speech": 5.0,  # microWakeWord's own sets
    "speech_background": 10.0,
    "no_speech_background": 5.0,
    "dinner_party_background": 5.0,
}


def feature_set(path, sampling, truth, truncation="truncate_start"):
    return {
        "features_dir": str(path),
        "sampling_weight": sampling,
        "penalty_weight": 1.0,
        "truth": truth,
        "truncation_strategy": truncation,
        "type": "mmap",
    }


def features(layout: Layout, setup: str, run: str | None = None) -> list[dict]:
    root, neg = layout.features / "hey_tars", layout.mww / "negative_datasets"
    fs = [feature_set(root / "positive" / n, w, True) for n, w in POSITIVE_WEIGHTS.items()]
    fs += [feature_set(root / "near_miss" / n, w, False) for n, w in NEAR_MISS_WEIGHTS.items()]
    if setup in ("generic", "household"):
        fs.append(feature_set(root / "near_miss" / "real", 2.0, False))
    fs += [feature_set(neg / n, w, False, "random") for n, w in NEGATIVES.items()]
    fs.append(feature_set(neg / "dinner_party_eval", 0.0, False, "split"))  # validation and testing only
    if setup == "personal":
        user = layout.features / "user" / "hey_tars"
        fs += [feature_set(user / "positive", 1.5, True), feature_set(user / "negative", 2.0, False, "random")]
    if setup == "household":
        house = layout.features / "household" / run
        fs.append(feature_set(house / "positive", 1.5, True))
        if (house / "negative").is_dir():  # a household may have nothing labeled "not for TARS" yet
            fs.append(feature_set(house / "negative", 2.0, False, "random"))
    missing = [f["features_dir"] for f in fs if not os.path.isdir(f["features_dir"])]
    if missing:
        raise SystemExit("Missing features (run training.stage1.features first):\n  " + "\n  ".join(missing))
    return fs


def main():
    p = parser(__doc__)
    p.add_argument("setup", choices=["generic", "personal", "household"])
    p.add_argument("--run", help="household: the training run's name")
    args = p.parse_args()
    if (args.setup == "household") != bool(args.run):
        p.error("--run goes with the household setup, and only with it")
    for k, v in {
        "OMP_NUM_THREADS": "4",
        "TF_NUM_INTRAOP_THREADS": "4",
        "TF_NUM_INTEROP_THREADS": "2",
        "TF_CPP_MIN_LOG_LEVEL": "2",
    }.items():
        os.environ.setdefault(k, v)
    layout = Layout(args.data)
    name = f"hey_tars_household_{args.run}" if args.run else f"hey_tars_{args.setup}"
    train_dir = layout.root / "trained_models" / name
    config = {
        "window_step_ms": 10,
        "train_dir": str(train_dir),
        "features": features(layout, args.setup, args.run),
        "training_steps": [20000],
        "positive_class_weight": [1],
        "negative_class_weight": [20],
        "learning_rates": [0.001],
        "batch_size": 128,
        "time_mask_max_size": [5],
        "time_mask_count": [2],
        "freq_mask_max_size": [3],
        "freq_mask_count": [2],
        "eval_step_interval": 500,
        "clip_duration_ms": 1500,
        "minimization_metric": "ambient_false_positives_per_hour",
        "target_minimization": 0.5,
        "maximization_metric": "average_viable_recall",
    }
    train_dir.parent.mkdir(parents=True, exist_ok=True)
    cfg = train_dir.parent / f"{name}.yaml"
    cfg.write_text(yaml.dump(config))
    log(f"training {args.setup}")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "microwakeword.model_train_eval",
            f"--training_config={cfg}",
            "--train",
            "1",
            "--restore_checkpoint",
            "1",
            "--test_tf_nonstreaming",
            "0",
            "--test_tflite_nonstreaming",
            "0",
            "--test_tflite_nonstreaming_quantized",
            "0",
            "--test_tflite_streaming",
            "1",
            "--test_tflite_streaming_quantized",
            "0",
            "--use_weights",
            "best_weights",
            "mixednet",
            "--pointwise_filters",
            "64,64,64,64",
            "--repeat_in_block",
            "1, 1, 1, 1",
            "--mixconv_kernel_sizes",
            "[5], [7,11], [9,15], [23]",
            "--residual_connection",
            "0,0,0,0",
            "--first_conv_filters",
            "32",
            "--first_conv_kernel_size",
            "5",
            "--stride",
            "3",
        ],
        check=True,
        cwd=layout.mww,
    )
    out = (
        layout.root / "household" / args.run / "pair" if args.run else layout.models / args.setup
    ) / "hey_tars.tflite"
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(train_dir / "tflite_stream_state_internal/stream_state_internal.tflite", out)
    log(f"DONE: {out}" + ("" if args.run else f" (copy it to the repo's models/{args.setup}/)"))


if __name__ == "__main__":
    main()
