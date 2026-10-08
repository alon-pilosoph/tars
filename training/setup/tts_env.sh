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
[ -d .venv ] || uv venv -q -p 3.14 .venv || die "venv"
source .venv/bin/activate
uv pip install -q "kokoro==0.9.4" "misaki[en]==0.9.4" "piper-tts==1.8.0" "onnxruntime==1.30.0" "soundfile==0.14.0" \
  "scipy==1.18.1" "numpy==2.5.3" librosa openai "transformers==5.19.0" || die "install"
# transformers: pinned too; left loose, the resolver once picked 4.12, whose tokenizers needs Rust to build
# Kokoro's English G2P loads spaCy's small English model; spaCy's own downloader needs pip, which uv's venvs don't have.
SPACY=$(python -c "import spacy; a, b, *_ = spacy.__version__.split('.'); print(f'{a}.{b}.0')")
python -c "import en_core_web_sm" 2>/dev/null || uv pip install -q \
  "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-$SPACY/en_core_web_sm-$SPACY-py3-none-any.whl" \
  || die "spaCy model"
python -c "import kokoro, piper, librosa, openai, en_core_web_sm" || die "imports"
log "imports ok"
