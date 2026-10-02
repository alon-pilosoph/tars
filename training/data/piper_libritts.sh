#!/bin/bash
# 50,000 "hey TARS" clips and 50,000 lookalikes from Piper's LibriTTS-R voice (~900 speakers), made with
# openWakeWord's clip generator.
#
#     bash training/data/piper_libritts.sh [PHRASE] [DATA]     (PHRASE = hey_tars | tars_stop, default hey_tars)
#
# "tarss" makes Piper say TARS with an S; the other phrase is added as a lookalike. Needs setup/mww_env.sh first
# (for piper-sample-generator). Output: DATA/clips/piper_libritts/PHRASE/{positive,near_miss} (links into DATA/oww).
# About a night per phrase on the development Mac.
set -euo pipefail
PHRASE=${1:-hey_tars}
DATA=${2:-${TARS_TRAINING_DATA:-$HOME/tars-training}}
W=$DATA/oww
OWW_COMMIT=368c03716d1e92591906a84949bc477f3a834455  # the openWakeWord the patches below were written for
export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 PYTORCH_ENABLE_MPS_FALLBACK=1 \
  OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
# torch 2.5 can't run Piper's TorchScript-fused op on the Apple GPU; plain eager mode is as fast.
export PYTORCH_JIT=0
log() { echo "[$(date +%T)] $*"; }
die() { log "FAILED: $*"; exit 1; }
[ -d "$DATA/mww/piper-sample-generator" ] || die "run training/setup/mww_env.sh first"
mkdir -p "$W" && cd "$W"

if [ ! -f .setup_done ]; then
  log "installing openWakeWord in $W/.venv"
  [ -d .venv ] || uv venv -q -p 3.10 .venv || die venv
  source .venv/bin/activate
  [ -d openwakeword ] || git clone -q https://github.com/dscripka/openwakeword || die clone
  git -C openwakeword checkout -q "$OWW_COMMIT" || die "openwakeword $OWW_COMMIT"
  for p in "torch==2.5.0 torchaudio==2.5.0" "piper-phonemize-cross==1.2.1" webrtcvad "-e ./openwakeword --no-deps" \
      mutagen==1.47.0 torchinfo==1.8.0 torchmetrics==1.2.0 speechbrain==0.5.14 audiomentations==0.33.0 \
      torch-audiomentations==0.11.0 acoustics==0.2.6 "onnxruntime onnx" pronouncing==0.2.0 datasets==2.14.6 \
      "numba>=0.62" "setuptools<81" "scipy soundfile librosa pyarrow<21 pyyaml tqdm requests"; do
    uv pip install -q $p || die "install $p"  # unquoted on purpose: some entries are several packages
  done
  M=openwakeword/openwakeword/resources/models; mkdir -p "$M"
  for f in embedding_model.onnx melspectrogram.onnx; do
    [ -f "$M/$f" ] || { curl -fsSL --retry 3 -o "$M/$f.part" \
      "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/$f" && mv "$M/$f.part" "$M/$f"; } \
      || die "model $f"
  done
  python - <<'PY' || die patch
def patch(path, old, new):
    s = open(path).read()
    assert old in s or new in s, f"{path} has changed: {old!r} not found"
    open(path, "w").write(s.replace(old, new))

# "tarss" isn't in the pronouncing dictionary: give it TARS with an S.
patch("openwakeword/openwakeword/data.py",
      "input_text_phones = [pronouncing.phones_for_word(i) for i in input_text.split()]",
      'input_text_phones = [pronouncing.phones_for_word(i) or {"tarss": ["T AA1 R S"]}.get(i, []) '
      'for i in input_text.split()]')
# Full batches for lookalikes too, and only 4 workers.
patch("openwakeword/openwakeword/train.py", 'config["tts_batch_size"]//7', 'config["tts_batch_size"]')
patch("openwakeword/openwakeword/train.py", "n_cpus = os.cpu_count()", "n_cpus = 8  # capped: 8 // 2 = 4 workers")
PY
  touch .setup_done
fi
source .venv/bin/activate

python - "$PHRASE" "$W" "$DATA" <<'PY' || die config
import sys, yaml
phrase, w, data = sys.argv[1:]
say, other = {"hey_tars": ("hey tarss", "tarss stop"), "tars_stop": ("tarss stop", "hey tarss")}[phrase]
c = yaml.safe_load(open(f"{w}/openwakeword/examples/custom_model.yml"))
c.update(target_phrase=[say], model_name=phrase, custom_negative_phrases=[other], n_samples=50000, n_samples_val=5000,
         output_dir=f"{w}/output", piper_sample_generator_path=f"{data}/mww/piper-sample-generator",
         rir_paths=[f"{data}/backgrounds/mit_rirs"], background_paths=[f"{data}/backgrounds/audioset_16k", f"{data}/backgrounds/fma"])
yaml.dump(c, open(f"{w}/{phrase}.yaml", "w"))
PY
log "$PHRASE: generating clips"
python openwakeword/openwakeword/train.py --training_config "$PHRASE.yaml" --generate_clips || die "$PHRASE generate"
OUT=$DATA/clips/piper_libritts/$PHRASE
mkdir -p "$OUT"
ln -sfn "$W/output/$PHRASE/positive_train" "$OUT/positive"
ln -sfn "$W/output/$PHRASE/negative_train" "$OUT/near_miss"
log "$PHRASE: done -> $OUT"
