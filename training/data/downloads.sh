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
# Resumable: a finished download leaves <archive>.done, a finished unpack <archive>.unpacked, and the archive is
# deleted only then, so an interrupted unpack starts over from the archive.
set -u
DATA=${1:-${TARS_TRAINING_DATA:-$HOME/tars-training}}
PREPARED=${2:-}
fetch() {  # URL, archive, the command that unpacks it
  [ -f "$2.unpacked" ] && return 0
  [ -f "$2.done" ] || { curl -sL -C - -o "$2" "$1" && touch "$2.done" && echo "$2 downloaded"; } || return 1
  eval "$3" && touch "$2.unpacked" && rm -f "$2"
}

mkdir -p "$DATA/aug" && cd "$DATA/aug" || exit 1
fetch https://www.openslr.org/resources/17/musan.tar.gz musan.tar.gz "tar -xzf musan.tar.gz"
fetch https://www.openslr.org/resources/28/rirs_noises.zip rirs_noises.zip "unzip -qo rirs_noises.zip"

cd "$DATA" || exit 1
[ "$PREPARED" = "--prepared" ] || fetch https://www.openslr.org/resources/12/train-clean-100.tar.gz \
  train-clean-100.tar.gz "tar -xzf train-clean-100.tar.gz"

mkdir -p "$DATA/bench/interference" && cd "$DATA/bench/interference" || exit 1
fetch https://github.com/karoldvl/ESC-50/archive/master.zip esc50.zip "unzip -qo esc50.zip"
fetch https://huggingface.co/datasets/mchl914/fma_xsmall/resolve/main/fma_xs.zip fma_xs.zip "unzip -qo fma_xs.zip"
cd "$DATA/bench" || exit 1
fetch https://www.openslr.org/resources/12/test-clean.tar.gz test-clean.tar.gz "tar -xzf test-clean.tar.gz"
echo "ALL DONE"
