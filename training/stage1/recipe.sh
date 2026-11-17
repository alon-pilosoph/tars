#!/bin/sh
# The shipped generic stage 1 (models/generic/hey_tars.tflite), from a data folder that has had steps 0-3 and
# features synthetic (README.md, run order). About 9 hours of training on the development Mac; two jobs at once.
#
#   sh training/stage1/recipe.sh            (from the repo root; DATA = $TARS_TRAINING_DATA or ~/tars-training)
#
# 1. More negatives: real read speech, and a first model's own mistakes on the training audio (hard-negative mining).
# 2. Four teachers, each the average of its last five snapshots (stochastic weight averaging): microWakeWord's own
#    checkpoint choice is a lottery, the average isn't. Three of the plain recipe, one with loud talk and TV.
# 3. The student: the same recipe with loud talk and TV, its targets half the labels and half the teachers' averaged
#    scores (distillation from a committee), again averaged over its last five snapshots. Same size as any stage 1.
set -e
D=${TARS_TRAINING_DATA:-$HOME/tars-training}
M=$D/mww/.venv/bin/python
RECIPE="--negative-weight 40 --extra mined:10 --extra speech:5"
W=snapshots/step_avg5.weights.h5

$M -m training.stage1.features synthetic --loud
$M -m training.stage1.features speech-negatives
$M -m training.stage1.train generic --negative-weight 40 --name recipe-base
uv run --group training python -m training.stage1.mine "$D/models/recipe-base/hey_tars.tflite" "$D/aug/mined"
$M -m training.stage1.features negatives mined --folder "$D/aug/mined"

teacher() {  # NAME ARGS...
  name=$1; shift
  # shellcheck disable=SC2086
  $M -m training.stage1.train generic "$@" $RECIPE --snapshots 2000 --name "$name"
  $M -m training.stage1.export_snapshots "$name" "$D/runs/recipe" --average 5
}
teacher recipe-t1 & teacher recipe-t2; wait
teacher recipe-t3 & teacher recipe-t4 --loud; wait

# shellcheck disable=SC2086
$M -m training.stage1.train generic --loud $RECIPE --snapshots 2000 --name recipe-student --teacher-small --alpha 0.5 \
  --teacher "recipe-t1:$W,recipe-t2:$W,recipe-t3:$W,recipe-t4:$W"
$M -m training.stage1.export_snapshots recipe-student "$D/runs/recipe" --average 5
echo "stage 1: $D/runs/recipe/recipe-student-savg5.tflite (8-bit, for an ESP32: -savg5-int8.tflite)"
