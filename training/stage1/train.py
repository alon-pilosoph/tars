"""Train stage 1, the microWakeWord "hey TARS" model, with the recipe the experiments settled on.

    DATA/mww/.venv/bin/python -m training.stage1.train generic       # never hears the owner: models/generic
    DATA/mww/.venv/bin/python -m training.stage1.train personal      # adds the owner's training half: models/personal
    DATA/mww/.venv/bin/python -m training.stage1.train household --run RUN   # adds a household's wakes
    DATA/mww/.venv/bin/python -m training.stage1.train generic --phrase tars_stop    # "TARS stop", the interrupt
    DATA/mww/.venv/bin/python -m training.stage1.train generic --large --cloned      # the large pair
    DATA/mww/.venv/bin/python -m training.stage1.train generic --device   # on features.py synthetic --device
    DATA/mww/.venv/bin/python -m training.stage1.train generic --cut      # on features.py synthetic --cut

The recipe: 20k steps, learning rate 0.001, negative class weight 20, SpecAugment (2 time masks of up to 5 frames,
2 frequency masks of up to 3 bins), batch 128, 1.5 s clips. The checkpoint kept is the one with the best average
recall among those under 0.5 false accepts per hour of ambient audio. The model is a MixedNet (4 blocks of 64
filters, kernels [5], [7,11], [9,15], [23], stride 3). All setups use the uncleaned synthetic data.
  generic:  + real LibriSpeech lookalikes (weight 2)
  personal: + the owner's training half (positives weight 1.5, sentences weight 2)
  household: generic + a household's real wakes and missed "hey TARS" (weight 1.5) and what wasn't for TARS
             (weight 2), as training.household sets them out; the same weights as the owner's in personal
  --large:  4 blocks of 256 filters and a first layer of 96 (372 KB); --cloned adds the cloned Common Voice
            speakers (features.py cloned; positives weight 1.5, lookalikes 3)
20 to 45 minutes on the development Mac at 4 threads. Needs features.py synthetic (and user, for personal).
Output: DATA/models/<setup>/<phrase>.tflite (household: DATA/household/<run>/pair/), the full-precision streaming
model (float32 in and out, 69 KB). "TARS stop" has no real lookalikes (they're cut for "hey TARS" only); its own
near-misses include "stop" on its own and in sentences, since its first try turned into a detector for "stop".
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from training.common import PHRASES, Layout, cap_threads, log, parser

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
VALIDATION_NEGATIVES = "dinner_party_eval"


def feature_set(path, sampling, truth, truncation="truncate_start"):
    return {
        "features_dir": str(path),
        "sampling_weight": sampling,
        "penalty_weight": 1.0,
        "truth": truth,
        "truncation_strategy": truncation,
        "type": "mmap",
    }


DEVICE_NEGATIVE_WEIGHT = 5.0  # the backgrounds through made-up microphones, as microWakeWord's own no-speech sets


def features(
    layout: Layout,
    setup: str,
    run: str | None = None,
    phrase: str = "hey_tars",
    cloned: bool = False,
    device: bool = False,
    cut: bool = False,
    tv_negatives: float = 0,
    extra: list[str] = (),
    vc: float = 0,
    loud: bool = False,
    lead: bool = False,
) -> list[dict]:
    variant = "device" if device else "cut" if cut else "loud" if loud else "lead" if lead else None
    root = layout.features / (f"{phrase}-{variant}" if variant else phrase)
    neg = layout.mww / "negative_datasets"
    positives, near_misses = POSITIVE_WEIGHTS, NEAR_MISS_WEIGHTS
    if cloned:
        positives, near_misses = {**positives, "cloned": 1.5}, {**near_misses, "cloned": 3.0}
    fs = [feature_set(root / "positive" / n, w, True) for n, w in positives.items()]
    fs += [feature_set(root / "near_miss" / n, w, False) for n, w in near_misses.items()]
    if setup in ("generic", "household") and phrase == "hey_tars":
        fs.append(feature_set(root / "near_miss" / "real", 2.0, False))
    fs += [feature_set(neg / n, w, False, "random") for n, w in NEGATIVES.items()]
    if device:
        fs.append(feature_set(root / "negative" / "device", DEVICE_NEGATIVE_WEIGHT, False, "random"))
    if vc:
        base = layout.features / phrase
        fs += [feature_set(base / "positive" / "vc", vc, True), feature_set(base / "near_miss" / "vc", 2 * vc, False)]
    for spec in extra:
        set_name, weight = spec.rsplit(":", 1)
        fs.append(feature_set(layout.features / phrase / "negative" / set_name, float(weight), False, "random"))
    if tv_negatives:
        fs.append(feature_set(layout.features / phrase / "negative" / "tv", tv_negatives, False, "random"))
    fs.append(feature_set(neg / VALIDATION_NEGATIVES, 0.0, False, "split"))  # validation and testing only
    if setup == "personal":
        user = layout.features / "user" / phrase
        fs += [feature_set(user / "positive", 1.5, True), feature_set(user / "negative", 2.0, False, "random")]
    if setup == "household":
        house = layout.features / "household" / run
        fs.append(feature_set(house / "positive", 1.5, True))
        if (house / "negative").is_dir():
            fs.append(feature_set(house / "negative", 2.0, False, "random"))
    missing = [f["features_dir"] for f in fs if not os.path.isdir(f["features_dir"])]
    if missing:
        raise SystemExit("Missing features (run training.stage1.features first):\n  " + "\n  ".join(missing))
    return fs


def main():
    p = parser(__doc__)
    p.add_argument("setup", choices=["generic", "personal", "household"])
    p.add_argument("--run", help="household: the training run's name")
    p.add_argument("--phrase", default="hey_tars", choices=PHRASES)
    p.add_argument("--large", action="store_true", help="generic: the large model, four times as wide")
    p.add_argument("--cloned", action="store_true", help="generic: add the cloned Common Voice speakers")
    p.add_argument("--device", action="store_true", help="generic: the features made through made-up microphones")
    p.add_argument("--cut", action="store_true", help="generic: the features with some endings cut")
    p.add_argument("--loud", action="store_true", help="generic: the features made under loud talk and TV")
    p.add_argument("--lead", action="store_true", help="generic: the features with conversation before the phrase")
    p.add_argument(
        "--extra",
        action="append",
        default=[],
        metavar="NAME:WEIGHT",
        help="also features.py's negatives set NAME, at WEIGHT (again for more)",
    )
    p.add_argument("--vc", type=float, default=0, help="weight of the voice-converted speakers (features.py vc)")
    p.add_argument("--tv-negatives", type=float, default=0, help="weight of features.py tv-negatives (0: none)")
    p.add_argument("--negative-weight", type=float, default=20, help="how much a negative counts (the recipe's: 20)")
    p.add_argument("--name", help="the model's folder under DATA/models (default: its setup's)")
    p.add_argument(
        "--teacher",
        help="distil from this trained model (trained_models/hey_tars_NAME), large; or a "
        "committee: NAME[:WEIGHTS],... (WEIGHTS a file in its folder, e.g. snapshots/step_avg5.weights.h5)",
    )
    p.add_argument("--teacher-small", action="store_true", help="the teachers are small models (a committee)")
    p.add_argument("--alpha", type=float, default=0.5, help="with --teacher: the teacher's share of each target")
    p.add_argument("--snapshots", type=int, default=0, help="also save the weights every N steps (to choose later)")
    p.add_argument(
        "--average",
        type=int,
        default=0,
        help="with --snapshots: the model is the average of the last K snapshots (stochastic weight averaging), not "
        "microWakeWord's own checkpoint choice, which is a lottery on real recordings (the shipped recipe: 5)",
    )
    p.add_argument("--steps", type=int, default=20000, help="training steps (the recipe's: 20000)")
    p.add_argument("--lr", type=float, default=0.001, help="learning rate (the recipe's: 0.001)")
    p.add_argument("--target-fph", type=float, default=0.5, help="the checkpoint's false wakes an hour (recipe: 0.5)")
    p.add_argument(
        "--resume",
        action="store_true",
        help="carry on from the last checkpoint of an interrupted run (otherwise it starts over)",
    )
    args = p.parse_args()
    if args.average and not args.snapshots:
        p.error("--average needs --snapshots")
    if (args.setup == "household") != bool(args.run):
        p.error("--run goes with the household setup, and only with it")
    if args.setup == "household" and args.phrase != "hey_tars":
        p.error("a household's wakes are for hey_tars")
    variant = args.large or args.cloned or args.device or args.cut or args.loud or args.lead
    if variant and (args.setup != "generic" or args.phrase != "hey_tars"):
        p.error("--large, --cloned, --device and --cut are for the generic hey_tars model")
    import yaml

    cap_threads()
    layout = Layout(args.data)
    setup = args.name or next(
        (f"generic-{v}" for v in ("large", "device", "cut", "loud", "lead") if getattr(args, v)), args.setup
    )
    name = f"{args.phrase}_household_{args.run}" if args.run else f"{args.phrase}_{setup}"
    train_dir = layout.trained_models / name
    if not args.resume:
        shutil.rmtree(train_dir, ignore_errors=True)  # a finished run's checkpoint would be "restored" as done
    config = {
        "window_step_ms": 10,
        "train_dir": str(train_dir),
        "features": features(
            layout,
            args.setup,
            args.run,
            args.phrase,
            args.cloned,
            args.device,
            args.cut,
            args.tv_negatives,
            args.extra,
            args.vc,
            args.loud,
            args.lead,
        ),
        "training_steps": [args.steps],
        "positive_class_weight": [1],
        "negative_class_weight": [args.negative_weight],
        "learning_rates": [args.lr],
        "batch_size": 128,
        "time_mask_max_size": [5],
        "time_mask_count": [2],
        "freq_mask_max_size": [3],
        "freq_mask_count": [2],
        "eval_step_interval": 500,
        "clip_duration_ms": 1500,
        "minimization_metric": "ambient_false_positives_per_hour",
        "target_minimization": args.target_fph,
        "maximization_metric": "average_viable_recall",
    }
    train_dir.parent.mkdir(parents=True, exist_ok=True)
    cfg = train_dir.parent / f"{name}.yaml"
    cfg.write_text(yaml.dump(config))
    log(f"training {args.setup}")
    entry = ["-m", "microwakeword.model_train_eval"]
    env = None
    if args.teacher or args.snapshots:
        wrapped = ["-m", "training.stage1.distill", "--snapshot-every", str(args.snapshots)]
        if args.teacher:
            arch = (
                "--pointwise_filters {f},{f},{f},{f} --repeat_in_block 1,1,1,1"
                " --mixconv_kernel_sizes [5],[7,11],[9,15],[23]"
                " --residual_connection 0,0,0,0 --first_conv_filters {c} --first_conv_kernel_size 5 --stride 3"
            ).format(**({"f": 64, "c": 32} if args.teacher_small else {"f": 256, "c": 96}))
            dirs = ",".join(str(layout.trained_models / f"{args.phrase}_{t}") for t in args.teacher.split(","))
            wrapped += ["--teacher-dir", dirs, "--teacher-args", arch, "--alpha", str(args.alpha)]
        entry = [*wrapped, "--"]
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    command = [
            sys.executable,
            *entry,
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
            "1",
            "--use_weights",
            "best_weights",
            "mixednet",
            "--pointwise_filters",
            "256,256,256,256" if args.large else "64,64,64,64",
            "--repeat_in_block",
            "1, 1, 1, 1",
            "--mixconv_kernel_sizes",
            "[5], [7,11], [9,15], [23]",
            "--residual_connection",
            "0,0,0,0",
            "--first_conv_filters",
            "96" if args.large else "32",
            "--first_conv_kernel_size",
            "5",
            "--stride",
            "3",
    ]  # fmt: skip
    subprocess.run(command, check=True, cwd=layout.mww, env=env)
    if args.average:
        from training.stage1.export_snapshots import average

        snaps = [p for p in sorted((train_dir / "snapshots").glob("step_*.weights.h5")) if p.stem[5:11].isdigit()]
        if len(snaps) < args.average:
            raise SystemExit(f"--average {args.average}: only {len(snaps)} snapshots (snapshot more often)")
        avg = average(snaps[-args.average :], train_dir / "snapshots" / f"step_avg{args.average}.weights.h5")
        log(f"the model: the average of the last {args.average} snapshots, not the checkpoint microWakeWord chose")
        export = [sys.executable, "-m", "microwakeword.model_train_eval", *command[len(entry) + 1 :]]
        export[export.index("--train") + 1] = "0"
        export[export.index("--use_weights") + 1] = f"snapshots/{avg.name.removesuffix('.weights.h5')}"
        subprocess.run(export, check=True, cwd=layout.mww)
    folder = layout.household / args.run / "pair" if args.run else layout.models / setup
    out = folder / f"{args.phrase}.tflite"
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(train_dir / "tflite_stream_state_internal/stream_state_internal.tflite", out)
    quantized = train_dir / "tflite_stream_state_internal_quant/stream_state_internal_quant.tflite"
    if quantized.exists():  # the 8-bit model an ESP32 runs (ESPHome's micro_wake_word), next to the full one
        shutil.copy(quantized, out.with_name(f"{args.phrase}_int8.tflite"))
    log(f"DONE: {out}" + ("" if args.run else f" (copy it to the repo's models/{setup}/)"))


if __name__ == "__main__":
    main()
