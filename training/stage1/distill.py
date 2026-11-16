"""Stage 1 trained by distillation: microWakeWord's own training, except that every batch's targets are blended with
a teacher model's scores on the same spectrograms, so the small student learns how sure a bigger model is, not only
yes or no. Run by train.py --teacher; not on its own.

    DATA/mww/.venv/bin/python -m training.stage1.distill --teacher-dir DIR --teacher-args "..." --alpha A -- MWW_ARGS

DIR is a trained model's folder (trained_models/<name>, with best_weights.weights.h5) and the teacher's architecture is
given as microWakeWord's mixednet arguments. alpha is the teacher's share of each target: (1 - alpha) * the label +
alpha * the teacher's score. The class weights stay the labels' own. Everything else (data, augmentation, checkpoint
choice, the streaming and 8-bit export) is microWakeWord's, run as its model_train_eval.
"""

import argparse
import os
import runpy
import sys


def main():
    argv = sys.argv[1:]
    split = argv.index("--") if "--" in argv else len(argv)
    p = argparse.ArgumentParser()
    p.add_argument("--teacher-dir", required=True)
    p.add_argument("--teacher-args", required=True)
    p.add_argument("--alpha", type=float, default=0.5)
    own = p.parse_args(argv[:split])

    import microwakeword.train as mww_train
    import numpy as np
    from microwakeword import mixednet

    original_train = mww_train.train

    def distilled_train(model, config, data_processor):
        tp = argparse.ArgumentParser()
        mixednet.model_parameters(tp)
        teacher_flags = tp.parse_args(own.teacher_args.split())
        teacher = mixednet.model(teacher_flags, config["training_input_shape"], config["batch_size"])
        teacher.load_weights(os.path.join(own.teacher_dir, "best_weights.weights.h5"))
        teacher.trainable = False
        plain = model.train_on_batch

        def train_on_batch(x, y, sample_weight=None, **kw):
            soft = np.asarray(teacher(x, training=False)).reshape(-1, 1)
            target = (1 - own.alpha) * np.asarray(y, dtype=np.float32).reshape(-1, 1) + own.alpha * soft
            return plain(x, target, sample_weight=sample_weight, **kw)

        model.train_on_batch = train_on_batch
        return original_train(model, config, data_processor)

    mww_train.train = distilled_train
    sys.argv = ["model_train_eval", *argv[split + 1 :]]
    runpy.run_module("microwakeword.model_train_eval", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
