#!/bin/bash
# Public datasets. Training and test sources are kept apart, so the benchmark measures sounds no model heard.
#
#     bash training/data/downloads.sh [DATA]       (DATA defaults to $TARS_TRAINING_DATA or ~/tars-training)
#
# Training:  MUSAN (CC BY 4.0) and RIRS_NOISES (Apache 2.0)                -> DATA/aug
#            LibriSpeech train-clean-100 (CC BY 4.0), for voice conversion
#            and real lookalike words                                      -> DATA/LibriSpeech/train-clean-100
# Test only: ESC-50 (CC BY-NC), FMA-xsmall, LibriSpeech test-clean         -> DATA/bench
# Resumable: each finished download leaves a .done marker.
set -u
DATA=${1:-${TARS_TRAINING_DATA:-$HOME/tars-training}}
get() { [ -f "$2.done" ] && return 0; curl -sL -C - -o "$2" "$1" && echo "$2 downloaded" && touch "$2.done"; }

mkdir -p "$DATA/aug" && cd "$DATA/aug" || exit 1
get https://www.openslr.org/resources/17/musan.tar.gz musan.tar.gz && { [ -d musan ] || tar -xzf musan.tar.gz; }
get https://www.openslr.org/resources/28/rirs_noises.zip rirs_noises.zip && { [ -d RIRS_NOISES ] || unzip -q rirs_noises.zip; }

cd "$DATA" || exit 1
get https://www.openslr.org/resources/12/train-clean-100.tar.gz train-clean-100.tar.gz \
  && { [ -d LibriSpeech/train-clean-100 ] || tar -xzf train-clean-100.tar.gz; }

mkdir -p "$DATA/bench/interference" && cd "$DATA/bench/interference" || exit 1
get https://github.com/karoldvl/ESC-50/archive/master.zip esc50.zip && { [ -d ESC-50-master ] || unzip -q esc50.zip; }
get https://huggingface.co/datasets/mchl914/fma_xsmall/resolve/main/fma_xs.zip fma_xs.zip && { [ -d fma_small ] || unzip -q fma_xs.zip; }
cd "$DATA/bench" || exit 1
get https://www.openslr.org/resources/12/test-clean.tar.gz test-clean.tar.gz && { [ -d LibriSpeech/test-clean ] || tar -xzf test-clean.tar.gz; }
echo "ALL DONE"
