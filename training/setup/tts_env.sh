#!/bin/bash
# The clip-making environment: Piper, Kokoro and OpenAI TTS, and the pitch/tempo pass.
#
#     bash training/setup/tts_env.sh [DATA]        (DATA defaults to $TARS_TRAINING_DATA or ~/tars-training)
#
# Creates DATA/tts/.venv (Python 3.12). Kokoro also needs espeak-ng for its fallback G2P (brew install espeak-ng).
set -uo pipefail
DATA=${1:-${TARS_TRAINING_DATA:-$HOME/tars-training}}
mkdir -p "$DATA/tts" && cd "$DATA/tts" || exit 1
[ -d .venv ] || uv venv -q -p 3.12 .venv || exit 1
source .venv/bin/activate
uv pip install -q "kokoro==0.9.4" "misaki[en]==0.9.4" "piper-tts==1.8.0" onnxruntime soundfile scipy numpy librosa \
  openai || exit 1
python -c "import kokoro, piper, librosa, openai; print('imports ok')"
