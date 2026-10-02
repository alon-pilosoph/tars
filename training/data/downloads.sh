#!/bin/bash
# Public datasets. Training and test sources are kept apart, so the benchmark measures sounds no model heard.
#
#     bash training/data/downloads.sh [DATA] [--prepared]   (DATA: $TARS_TRAINING_DATA or ~/tars-training)
#
# --prepared: for a data folder filled from the hosted clips (training/hub.py), which already has the real lookalike
# words: skips LibriSpeech train-clean-100 (6 GB), which only voice conversion and cutting those words need.
#
# Training:  MUSAN (CC BY 4.0) and RIRS_NOISES (Apache 2.0)                -> DATA/aug
#            LibriSpeech train-clean-100 (CC BY 4.0), for voice conversion
#            and real lookalike words                                      -> DATA/LibriSpeech/train-clean-100
# Test only: ESC-50 (CC BY-NC), FMA-xsmall, LibriSpeech test-clean         -> DATA/bench
# Resumable: a finished download leaves <archive>.done and a finished unpack <archive>.unpacked; the archive is
# deleted only after unpacking, so an interrupted unpack restarts from it. An archive that won't unpack is deleted so
# a rerun downloads it again.
set -euo pipefail
DATA=${1:-${TARS_TRAINING_DATA:-$HOME/tars-training}}
PREPARED=${2:-}
mkdir -p "$DATA" && DATA=$(cd "$DATA" && pwd)
log() { echo "[$(date +%T)] $*"; }
die() { log "FAILED: $*"; exit 1; }
fetch() {  # URL, archive, the command that unpacks it
  [ -f "$2.unpacked" ] && return 0
  if [ ! -f "$2.done" ]; then
    curl -fsSL --retry 3 -C - -o "$2" "$1" || die "download $1"
    touch "$2.done"
    log "$2 downloaded"
  fi
  eval "$3" || { rm -f "$2" "$2.done"; die "unpack $2 (deleted it: run this again to download it again)"; }
  touch "$2.unpacked"
  rm -f "$2"
}

mkdir -p "$DATA/aug" && cd "$DATA/aug"
fetch https://www.openslr.org/resources/17/musan.tar.gz musan.tar.gz "tar -xzf musan.tar.gz"
fetch https://www.openslr.org/resources/28/rirs_noises.zip rirs_noises.zip "unzip -qo rirs_noises.zip"

cd "$DATA"
[ "$PREPARED" = "--prepared" ] || fetch https://www.openslr.org/resources/12/train-clean-100.tar.gz \
  train-clean-100.tar.gz "tar -xzf train-clean-100.tar.gz"

mkdir -p "$DATA/bench/interference" && cd "$DATA/bench/interference"
fetch https://github.com/karoldvl/ESC-50/archive/master.zip esc50.zip "unzip -qo esc50.zip"
fetch https://huggingface.co/datasets/mchl914/fma_xsmall/resolve/main/fma_xs.zip fma_xs.zip "unzip -qo fma_xs.zip"
cd "$DATA/bench"
fetch https://www.openslr.org/resources/12/test-clean.tar.gz test-clean.tar.gz "tar -xzf test-clean.tar.gz"
log "all downloaded"
