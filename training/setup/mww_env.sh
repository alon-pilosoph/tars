#!/bin/bash
# The microWakeWord environment (stage 1 features and training), after kahrendt/microWakeWord's
# basic_training_notebook.ipynb, macOS variant. Also downloads its negative feature sets (~20 GB).
#
#     bash training/setup/mww_env.sh [DATA]        (DATA defaults to $TARS_TRAINING_DATA or ~/tars-training)
#
# Creates DATA/mww/.venv (Python 3.10), clones microWakeWord and piper-sample-generator (with the LibriTTS-R
# voice the openWakeWord clip generator uses), and fills DATA/mww/negative_datasets. Safe to rerun.
set -uo pipefail
DATA=${1:-${TARS_TRAINING_DATA:-$HOME/tars-training}}
CONSTRAINTS=$(cd "$(dirname "$0")" && pwd)/mww-constraints.txt
MWW_COMMIT=4665173cd35f1cff9a61e06fc427f124766c488e  # the microWakeWord the shipped models were trained with
W=$DATA/mww
mkdir -p "$W" && cd "$W" || exit 1
log() { echo "[$(date +%T)] $*"; }
die() { log "FAILED: $*"; exit 1; }

log "python env in $W/.venv"
[ -d .venv ] || uv venv -q -p 3.10 .venv || die "venv"
source .venv/bin/activate
PIP="uv pip install -q -c $CONSTRAINTS"
$PIP 'git+https://github.com/whatsnowplaying/audio-metadata@d4ebb238e6a401bb1a5aaaac60c9e2b3cb30929f' || die "audio-metadata"
[ -d microWakeWord ] || git clone -q https://github.com/kahrendt/microWakeWord || die "clone microWakeWord"
git -C microWakeWord checkout -q $MWW_COMMIT || die "microWakeWord $MWW_COMMIT"
$PIP -e ./microWakeWord || die "install microWakeWord"
[ -d piper-sample-generator ] || git clone -q -b mps-support https://github.com/kahrendt/piper-sample-generator || die "clone piper-sample-generator"
[ -f piper-sample-generator/models/en_US-libritts_r-medium.pt ] || curl -sL -o piper-sample-generator/models/en_US-libritts_r-medium.pt \
  https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt || die "piper model"
$PIP torch torchaudio piper-phonemize-cross==1.2.1 soundfile librosa pyarrow scipy datasets pyyaml tensorboard || die "deps"
python -c "import microwakeword, torch, piper_phonemize, tensorflow as tf; print('imports ok, tf', tf.__version__)" || die "imports"

log "negative feature sets"
mkdir -p negative_datasets
for f in dinner_party dinner_party_eval no_speech speech speech_background no_speech_background dinner_party_background; do
  [ -d negative_datasets/$f ] && continue
  curl -sL -C - -o negative_datasets/$f.zip https://huggingface.co/datasets/kahrendt/microwakeword/resolve/main/$f.zip || die "download $f"
  unzip -q negative_datasets/$f.zip -d negative_datasets && rm negative_datasets/$f.zip || die "unzip $f"
  log "  $f ok"
done
log "done"
