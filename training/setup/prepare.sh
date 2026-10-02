#!/bin/bash
# Everything a machine needs to train "hey TARS" models, from the hosted clips (training/hub.py) instead of making
# them: about 45 GB of downloads and 3-4 hours, unattended and resumable (rerun it after an interruption).
#
#     bash training/setup/prepare.sh [DATA]        (DATA defaults to $TARS_TRAINING_DATA or ~/tars-training)
#
# 1. microWakeWord's environment and its negative sets (~20 GB)       training/setup/mww_env.sh
# 2. noise, rooms and test audio from their own sources (~15 GB)     training/data/downloads.sh --prepared
# 3. background sounds, and the babble and TV mixes                   backgrounds.py, make_interference.py
# 4. the hosted clips and the double-check's training rows (~4 GB)    training.hub download
# 5. the wake model's training features (~2 hours)                    training.stage1.features synthetic
# Then `uv run --group training python -m training.household` trains a household's pair (see training/README.md).
set -euo pipefail
DATA=${1:-${TARS_TRAINING_DATA:-$HOME/tars-training}}
mkdir -p "$DATA" && DATA=$(cd "$DATA" && pwd)
REPO=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO"
log() { echo; echo "[$(date +%T)] $*"; }

log "1/5 microWakeWord environment"; bash training/setup/mww_env.sh "$DATA"
M=$DATA/mww/.venv/bin/python
log "2/5 downloads"; bash training/data/downloads.sh "$DATA" --prepared
log "3/5 backgrounds and interference"
"$M" -m training.data.backgrounds --data "$DATA"
"$M" -m training.data.make_interference --data "$DATA"
log "4/5 hosted clips"  # HUB_FROM=<folder>: a local copy of the datasets (training.hub export) instead of the hub
uv run --group training python -m training.hub download --data "$DATA" ${HUB_FROM:+--from "$HUB_FROM"}
log "5/5 features"; nice -n 19 "$M" -m training.stage1.features synthetic --data "$DATA"
log "ready: $DATA"
