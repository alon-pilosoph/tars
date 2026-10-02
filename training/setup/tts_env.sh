#!/bin/bash
# The clip-making environment: Piper, Kokoro and OpenAI TTS, and the pitch/tempo pass.
#
#     bash training/setup/tts_env.sh [DATA]        (DATA defaults to $TARS_TRAINING_DATA or ~/tars-training)
#
# Creates DATA/tts/.venv (Python 3.12). Kokoro also needs espeak-ng for its fallback G2P (brew install espeak-ng).
set -euo pipefail
DATA=${1:-${TARS_TRAINING_DATA:-$HOME/tars-training}}
log() { echo "[$(date +%T)] $*"; }
die() { log "FAILED: $*"; exit 1; }
mkdir -p "$DATA/tts" && cd "$DATA/tts"
[ -d .venv ] || uv venv -q -p 3.12 .venv || die "venv"
source .venv/bin/activate
uv pip install -q "kokoro==0.9.4" "misaki[en]==0.9.4" "piper-tts==1.8.0" "onnxruntime==1.30.0" "soundfile==0.14.0" \
  "scipy==1.18.1" "numpy==2.5.3" librosa openai || die "install"
python -c "import kokoro, piper, librosa, openai" || die "imports"
log "imports ok"
