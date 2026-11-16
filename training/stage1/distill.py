"""microWakeWord's own training with two optional changes to each batch, run by train.py (--teacher, --snapshots);
not on its own.

    DATA/mww/.venv/bin/python -m training.stage1.distill [--teacher-dir DIR --teacher-args "..." --alpha A]
        [--snapshot-every N] -- MWW_ARGS

Distillation (--teacher-dir): every batch's targets are blended with a teacher model's scores on the same
spectrograms, so the small student learns how sure a bigger model is, not only yes or no. DIR is a trained model's
folder (trained_models/<name>, with best_weights.weights.h5) and the teacher's architecture is given as
microWakeWord's mixednet arguments. alpha is the teacher's share of each target: (1 - alpha) * the label + alpha * the
teacher's score. The class weights stay the labels' own.

Snapshots (--snapshot-every N): the weights are also saved every N steps (train_dir/snapshots/step_<n>.weights.h5),
so a run's best moment can be chosen later on real recordings instead of microWakeWord's synthetic validation
(export_snapshots.py makes each a streaming model).

Everything else (data, augmentation, its own checkpoint choice, the streaming and 8-bit export) is microWakeWord's,
run as its model_train_eval.
"""

import argparse
import os
import runpy
import sys


def main():
    argv = sys.argv[1:]
    split = argv.index("--") if "--" in argv else len(argv)
    p = argparse.ArgumentParser()
    p.add_argument("--teacher-dir")
    p.add_argument("--teacher-args")
    p.add_argument("--alpha", type=float, default=0.5)
    p.add_argument("--snapshot-every", type=int, default=0)
    own = p.parse_args(argv[:split])

    import microwakeword.train as mww_train
    import numpy as np
    from microwakeword import mixednet

    original_train = mww_train.train

    def wrapped_train(model, config, data_processor):
        teacher = None
        if own.teacher_dir:
            tp = argparse.ArgumentParser()
            mixednet.model_parameters(tp)
            teacher = mixednet.model(tp.parse_args(own.teacher_args.split()), config["training_input_shape"],
                                     config["batch_size"])  # fmt: skip
            teacher.load_weights(os.path.join(own.teacher_dir, "best_weights.weights.h5"))
            teacher.trainable = False
        snapshots = os.path.join(config["train_dir"], "snapshots")
        if own.snapshot_every:
            os.makedirs(snapshots, exist_ok=True)
        plain = model.train_on_batch
        step = [0]

        def train_on_batch(x, y, sample_weight=None, **kw):
            step[0] += 1
            if teacher is not None:
                soft = np.asarray(teacher(x, training=False)).reshape(-1, 1)
                y = (1 - own.alpha) * np.asarray(y, dtype=np.float32).reshape(-1, 1) + own.alpha * soft
            result = plain(x, y, sample_weight=sample_weight, **kw)
            if own.snapshot_every and step[0] % own.snapshot_every == 0:
                model.save_weights(os.path.join(snapshots, f"step_{step[0]:06d}.weights.h5"))
            return result

        model.train_on_batch = train_on_batch
        return original_train(model, config, data_processor)

    mww_train.train = wrapped_train
    sys.argv = ["model_train_eval", *argv[split + 1 :]]
    runpy.run_module("microwakeword.model_train_eval", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
