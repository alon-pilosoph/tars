#!/bin/bash
# The evaluation environment: Vosk and the learned layer (stage 2), kNN-VC voice conversion, MMS forced alignment,
# and the end-to-end benchmark (which runs the assistant's own wake and check code from src/).
#
#     bash training/setup/eval_env.sh [DATA]       (DATA defaults to $TARS_TRAINING_DATA or ~/tars-training)
#
# Creates DATA/eval/.venv (Python 3.12), with the versions the current models were made with.
set -euo pipefail
DATA=${1:-${TARS_TRAINING_DATA:-$HOME/tars-training}}
log() { echo "[$(date +%T)] $*"; }
die() { log "FAILED: $*"; exit 1; }
mkdir -p "$DATA/eval" && cd "$DATA/eval"
[ -d .venv ] || uv venv -q -p 3.12 .venv || die "venv"
source .venv/bin/activate
uv pip install -q "vosk==0.3.44" "scikit-learn==1.9.1" "torch==2.14.0" "torchaudio==2.11.0" "soundfile==0.14.0" \
  "scipy==1.18.1" "numpy==2.5.3" "pyarrow==25.0.1" "huggingface_hub==1.33.0" "pymicro-features==2.0.2" \
  "ai-edge-litert==2.2.0" "sounddevice==0.5.6" || die "install"
python -c "import vosk, sklearn, torch, torchaudio, pymicro_features, ai_edge_litert, sounddevice" || die "imports"
log "imports ok"
