#!/bin/bash
# The evaluation environment: Vosk and the learned layer (stage 2), kNN-VC voice conversion, MMS forced alignment,
# and the end-to-end benchmark (which runs the assistant's own wake and check code from src/).
#
#     bash training/setup/eval_env.sh [DATA]       (DATA defaults to $TARS_TRAINING_DATA or ~/tars-training)
#
# Creates DATA/eval/.venv (Python 3.12).
set -uo pipefail
DATA=${1:-${TARS_TRAINING_DATA:-$HOME/tars-training}}
mkdir -p "$DATA/eval" && cd "$DATA/eval" || exit 1
[ -d .venv ] || uv venv -q -p 3.12 .venv || exit 1
source .venv/bin/activate
uv pip install -q "vosk==0.3.44" scikit-learn torch torchaudio soundfile scipy numpy pyarrow huggingface_hub \
  "pymicro-features>=2" "ai-edge-litert>=2.2.0" || exit 1
python -c "import vosk, sklearn, torch, torchaudio, pymicro_features, ai_edge_litert; print('imports ok')"
