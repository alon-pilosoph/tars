#!/bin/sh
# The open stage 1 (models/generic/hey_tars.onnx), from a data folder prepared for stage 1 (README.md, run order;
# aug/mined is stage1/recipe.sh's mining). About 5 hours on the development Mac.
#
#   sh training/open/recipe.sh            (from the repo root; DATA = $TARS_TRAINING_DATA or ~/tars-training)
#
# 1. A first head, and its mistakes on 12 h of training-only audio as more negatives; a second, and its mistakes on
#    24 more hours (hard-negative mining, two rounds; a third round squeezed real wakes and was dropped).
# 2. Two heads on those rows: a plain one and LiveKit's conv-attention one, lookalikes counting 3x, negatives 10x.
# 3. More "hey TARS" and lookalikes at +5 to -10 dB only, and both heads again with those rows too.
# 4. The four averaged into one model: they err in different places, and the average beats each of them.
set -e
D=${TARS_TRAINING_DATA:-$HOME/tars-training}
P="$D/oww/.venv/bin/python -m training.open.train"

$P embed
$P train --out first
$P mine --model first --out hard --hours 12 --keep-over 0.3
$P train --out second --near 3
$P mine --model second --out hard2 --hours 24 --keep-over 0.5 --offset 3000
$P train --out plain --near 3 --neg 10
$P train --out plain-ca --head convattn --near 3 --neg 10
$P embed --loud
$P train --out loud --near 3 --neg 10
$P train --out loud-ca --head convattn --near 3 --neg 10
$P export --heads plain,plain-ca,loud,loud-ca --out "$D/open_model/hey_tars.onnx"
echo "open stage 1: $D/open_model/hey_tars.onnx (threshold 0.59, [wake] sure 0.88)"
