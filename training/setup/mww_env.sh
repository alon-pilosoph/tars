#!/bin/bash
# The microWakeWord environment (stage 1 features and training), after kahrendt/microWakeWord's
# basic_training_notebook.ipynb, macOS variant. Also downloads its negative feature sets (~20 GB, 41 GB unpacked).
#
#     bash training/setup/mww_env.sh [DATA]        (DATA defaults to $TARS_TRAINING_DATA or ~/tars-training)
#
# Creates DATA/mww/.venv (Python 3.13), clones microWakeWord and piper-sample-generator (with the LibriTTS-R
# voice the openWakeWord clip generator uses), and fills DATA/mww/negative_datasets. Safe to rerun.
set -euo pipefail
DATA=${1:-${TARS_TRAINING_DATA:-$HOME/tars-training}}
CONSTRAINTS=$(cd "$(dirname "$0")" && pwd)/mww-constraints.txt
MWW_COMMIT=4665173cd35f1cff9a61e06fc427f124766c488e  # the microWakeWord the shipped models were trained with
PSG_COMMIT=b5b36e266576693ea47db129fbfe6bbc06164762  # on piper-sample-generator's mps-support branch
HF=https://huggingface.co/datasets/kahrendt/microwakeword/resolve/main
W=$DATA/mww
mkdir -p "$W" && cd "$W"
log() { echo "[$(date +%T)] $*"; }
die() { log "FAILED: $*"; exit 1; }
install() { uv pip install -q -c "$CONSTRAINTS" "$@"; }

log "python env in $W/.venv"
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))'; then
  log "replacing $W/.venv: it isn't Python 3.13"
  uv venv -q --clear -p 3.13 .venv || die "venv"
fi
[ -d .venv ] || uv venv -q -p 3.13 .venv || die "venv"
source .venv/bin/activate
install 'git+https://github.com/whatsnowplaying/audio-metadata@d4ebb238e6a401bb1a5aaaac60c9e2b3cb30929f' || die "audio-metadata"
[ -d microWakeWord ] || git clone -q https://github.com/kahrendt/microWakeWord || die "clone microWakeWord"
git -C microWakeWord checkout -q "$MWW_COMMIT" || die "microWakeWord $MWW_COMMIT"
install -e ./microWakeWord || die "install microWakeWord"
[ -d piper-sample-generator ] || git clone -q -b mps-support https://github.com/kahrendt/piper-sample-generator \
  || die "clone piper-sample-generator"
git -C piper-sample-generator checkout -q "$PSG_COMMIT" || die "piper-sample-generator $PSG_COMMIT"
VOICE=piper-sample-generator/models/en_US-libritts_r-medium.pt
if [ ! -f "$VOICE" ]; then
  curl -fsSL --retry 3 -o "$VOICE.part" \
    https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt \
    && mv "$VOICE.part" "$VOICE" || die "piper model"
fi
# torchcodec: newer datasets decode audio with it (needs FFmpeg). The phonemizer lives in the clip generator's own
# environment (piper_libritts.sh); this one only keeps piper-sample-generator's code and voice for it.
install torch torchaudio torchcodec soundfile librosa pyarrow scipy datasets pyyaml tensorboard || die "deps"
python -c "import microwakeword, torch, torchcodec, tensorflow as tf; print('imports ok, tf', tf.__version__)" \
  || die "imports"

log "negative feature sets"
mkdir -p negative_datasets && cd negative_datasets
for f in dinner_party dinner_party_eval no_speech speech speech_background no_speech_background dinner_party_background; do
  [ -d "$f" ] && continue  # moved into place only once fully unzipped
  curl -fsSL --retry 3 -C - -o "$f.zip" "$HF/$f.zip" || die "download $f"
  rm -rf "$f.tmp"
  unzip -q "$f.zip" -d "$f.tmp" || { rm -f "$f.zip"; die "unzip $f (deleted it: run this again to download it again)"; }
  mv "$f.tmp/$f" "$f" && rm -rf "$f.tmp" "$f.zip" || die "unpack $f"
  log "  $f ok"
done
log "done"
