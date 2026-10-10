# Attribution

What TARS and its training data are built from, and under which terms. The code is MIT (`LICENSE`); the trained
wake models are CC BY-NC-SA 4.0 (`models/LICENSE.md`), because of the non-commercial sources below.

"How it's used" says whether a thing is **in this repo**, **hosted** in the training dataset on Hugging Face (the
clips and features `training/` makes, never anyone's own recordings), or only **downloaded** from its source by a
script.

## Models TARS runs

| Model | License | How it's used |
|---|---|---|
| [microWakeWord](https://github.com/kahrendt/microWakeWord) (the model architecture and training code) | Apache 2.0 | trains the wake model; the trained "hey TARS" model is in this repo |
| [Vosk small-en-us 0.15](https://alphacephei.com/vosk/models) | Apache 2.0 | the wake double-check; downloaded on first use |
| [Silero VAD](https://github.com/snakers4/silero-vad) | MIT | speech detection; downloaded |
| [Smart Turn](https://github.com/pipecat-ai/smart-turn) | BSD 2-Clause | end of turn; downloaded |
| [WeSpeaker ResNet34-LM](https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM) | CC BY 4.0 | speaker ID; downloaded |
| [openWakeWord](https://github.com/dscripka/openWakeWord) (library; its pretrained models are CC BY-NC-SA 4.0) | Apache 2.0 | only for pretrained names like "hey_jarvis" |

## Web UI

| Asset | License | How it's used |
|---|---|---|
| [Newsreader](https://github.com/productiontype/Newsreader) and [Instrument Sans](https://github.com/Instrument/instrument-sans) | SIL OFL 1.1 (`webui/src/fonts/OFL.txt`) | the web UI's and the How TARS works page's fonts, in this repo |

## Voices the training clips were synthesized with

| Source | License | How it's used |
|---|---|---|
| [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) | Apache 2.0 | clips hosted (permissive set) |
| OpenAI text-to-speech | output owned by the maker; AI-generated, and disclosed as such | training clips and the held-out test voices hosted (permissive set); `tools/demo_audio/` in this repo |
| [Piper](https://github.com/rhasspy/piper) voices ([model cards](https://huggingface.co/rhasspy/piper-voices)) | per voice | clips hosted, split by the voice's terms: |
| ... trained from scratch: libritts-high (CC BY 4.0), cori, kristin (public domain), john (from kristin) | as listed | permissive set |
| ... fine-tuned from lessac ([Blizzard 2013 license](https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html), research only), including libritts_r (the Piper LibriTTS clips), vctk, arctic, jenny_dioco and others; ryan (CC BY-NC-SA) and the voices built on it; l2arctic, semaine, hfc (NC datasets) | non-commercial | non-commercial set (CC BY-NC-SA 4.0) |
| ... alan (all rights reserved), and amy, danny, kusal (no license found) | none usable | not used: no clip from them, or derived from them, is in the models or the hosted sets |
| ... non-English voices, for the double-check's accented clips | per voice | used locally for stage 2, never hosted; left out for no usable license or a source built on one above: ar_JO-kareem, zh_CN-huayan, sv_SE-lisa, eu_ES-antton, eu_ES-maider (from amy), ka_GE-natia, ru_RU-irina, es_MX-claude |
| [piper-sample-generator](https://github.com/rhasspy/piper-sample-generator) | MIT | generates the Piper LibriTTS clips |
| [Chatterbox Turbo](https://github.com/resemble-ai/chatterbox) | MIT | clones Common Voice speakers: test clips, and the large pair's training clips; never hosted |

## Speech, noise and rooms

| Source | License | How it's used |
|---|---|---|
| [Mozilla Common Voice](https://commonvoice.mozilla.org/) (via the Mozilla Data Collective) | CC0 | downloaded; the speakers cloned for testing and for the large pair |
| [LibriSpeech](https://www.openslr.org/12/) | CC BY 4.0 (read from LibriVox, public domain) | downloaded; lookalike words cut from it are hosted |
| [LibriTTS-R](https://www.openslr.org/141/) | CC BY 4.0 | the base of Piper's libritts_r voice |
| [VCTK 0.92](https://datashare.ed.ac.uk/handle/10283/3443) | CC BY 4.0 | downloaded; voice conversions into its speakers are made locally and never hosted |
| [MUSAN](https://www.openslr.org/17/) | CC BY 4.0 overall; some music tracks CC BY-SA or CC BY-ND (see its LICENSE files) | downloaded; training background noise |
| [RIRS_NOISES](https://www.openslr.org/28/) | Apache 2.0 | downloaded; room echoes |
| [MIT IR Survey](https://mcdermottlab.mit.edu/Reverb/IR_Survey.html) | not stated | downloaded; room echoes |
| [AudioSet](https://research.google.com/audioset/) (the [agkphysics](https://huggingface.co/datasets/agkphysics/AudioSet) copy) | dataset CC BY 4.0; the audio is from YouTube | downloaded; background noise |
| [FMA](https://github.com/mdeff/fma) (fma_small, fma_xsmall) | per track, chosen by the artist, many NC or ND | downloaded; background music |
| [ESC-50](https://github.com/karolpiczak/ESC-50) | CC BY-NC 3.0 | downloaded; test noise |
| [microWakeWord's negative features](https://huggingface.co/datasets/kahrendt/microwakeword) | CC BY-NC 4.0 | downloaded; the wake model's negatives |
| [kNN-VC](https://github.com/bshall/knn-vc) | MIT | voice conversion, run locally |
| [MMS forced aligner](https://docs.pytorch.org/audio/main/generated/torchaudio.pipelines.MMS_FA.html) | CC BY-NC 4.0 (weights) | finds the lookalike words in LibriSpeech |

Nothing a household records (its wakes, requests and voiceprints) is ever committed or hosted.
