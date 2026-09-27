# Training the "hey TARS" models

These scripts made the wake models in `models/`:

| Setup | File | Made by |
|---|---|---|
| Generic (committed, the default) | `models/generic/hey_tars.tflite` | `stage1/train.py generic` |
| | `models/generic/hey_tars_check.json` | `stage2/train_check.py generic` |
| A household's pair (on its own machines) | `voice_data/events/wake_models/<setup>/vN/` | `household.py` |
| Personal (local only, gitignored) | `models/personal/hey_tars.tflite` | `stage1/train.py personal` |
| | `models/personal/hey_tars_check.json` | `stage2/train_check.py personal` |

[docs/wake-word.md](../docs/wake-word.md) explains how the two stages work, how well they do and what was tried.

## Training a household's own models

What most people want: a wake model and double-check tuned to the voices and rooms of one household, from the wakes
TARS logged there (every labeled wake and near-miss; see [self-learning](../docs/self-learning.md#learning-from-it)).
On a Mac or a Linux machine with about 80 GB free:

```bash
bash training/setup/prepare.sh                    # once: ~45 GB of downloads and 3-4 hours, unattended, resumable
uv run python -m training.household               # TARS runs on this machine
uv run python -m training.household --pi pi@tars.local   # TARS runs on a Pi (SSH access, repo in ~/voice-assistant)
```

`prepare.sh` sets up microWakeWord's environment and negative sets, downloads the noise, rooms and test audio from
their own sources, fetches the prepared clips from Hugging Face ([hey-tars-training](https://huggingface.co/datasets/alon-p/hey-tars-training),
CC BY 4.0, and [hey-tars-training-nc](https://huggingface.co/datasets/alon-p/hey-tars-training-nc), CC BY-NC-SA 4.0,
made by `training/hub.py`; the voices, never any household's recordings), and builds the wake model's features. `training.household` then copies the labeled wakes from TARS,
keeps every fifth aside, trains both stages (about 25 minutes), and tests the new pair against the one in use end to
end: the household's held-out wakes, held-out voices and lookalikes in 8 conditions, and false answers per hour on
TV and audiobooks. It prints the comparison and installs the pair only if it answers more of the household's real
wakes (or lets fewer through that weren't for TARS) and does no worse on anything else; otherwise it asks. The Pi
switches to it within seconds, and its Models page can switch back. Each run is kept in `DATA/household/<run>/`.

On the owner's 72 recordings (30 real, 42 lookalikes), a run took 19 minutes: other voices went from 94.4% to 97.8%
answered in quiet and 83.5% to 88.3% in noise, but lookalikes let through went from 3.0% to 4.8% and audiobooks
brought one false answer per hour, so it wasn't installed. That's the rule doing its job: more data per run, or a
review of what it got wrong, and the next run is compared the same way.

## Rebuilding everything from scratch

The rest of this page is how the generic and personal models were made, from nothing. Running `stage2/train_check.py generic` on the original data reproduces the
committed `hey_tars_check.json` exactly (same weights, same bias). Stage 1 training isn't bit-for-bit reproducible,
because microWakeWord's augmentation isn't seeded; a rerun gives a model of the same quality, not the same file.

## What you need

- macOS or Linux, [uv](https://docs.astral.sh/uv/), git, curl, and espeak-ng for Kokoro (`brew install espeak-ng`).
- About 130 GB of disk. The biggest parts are microWakeWord's negative sets (41 GB), stage 1 features (30 GB),
  MUSAN and the room responses (17 GB), and the clips (16 GB).
- An OpenAI API key for `openai_clips.py` (about $0.60).
- A day or two of mostly unattended CPU time. Everything here ran on a MacBook at 4 threads and `nice`.
- For the personal setup only: the owner's recordings (below). They never leave the machine.

Everything goes into one data folder: `--data DIR` on every script, or `TARS_TRAINING_DATA`, default
`~/tars-training`. The scripts run from the repo root as modules (`python -m training.stage1.train ...`), each in
one of four environments, because their dependencies don't mix:

| Environment | Made by | Used for |
|---|---|---|
| `DATA/tts/.venv` (Python 3.12) | `setup/tts_env.sh` | Piper, Kokoro, OpenAI clips, pitch/tempo |
| `DATA/mww/.venv` (Python 3.10) | `setup/mww_env.sh` | microWakeWord features and training, backgrounds, interference |
| `DATA/eval/.venv` (Python 3.12) | `setup/eval_env.sh` | stage 2, voice conversion, forced alignment, end-to-end tests |
| the repo's own (`uv run`) | `uv sync` | `data/clean.py` (uses the assistant's Vosk model) |

`data/piper_libritts.sh` makes a fifth, openWakeWord's, just to generate the Piper LibriTTS clips.

## Run order

Set `D=~/tars-training` (or your folder) and run from the repo root. Rough times are from the development Mac.

```bash
# 0. Environments (and microWakeWord's negative sets, ~20 GB)
bash training/setup/mww_env.sh; bash training/setup/tts_env.sh; bash training/setup/eval_env.sh
T=$D/tts/.venv/bin/python; M=$D/mww/.venv/bin/python; E=$D/eval/.venv/bin/python

# 1. Public datasets and background sound (downloads; an hour or two)
bash training/data/downloads.sh
$M -m training.data.backgrounds
$M -m training.data.make_interference                    # babble and TV, for training and for testing

# 2. Synthetic voices (hours; resumable, run two at a time at most)
bash training/data/piper_libritts.sh hey_tars            # 50k + 50k, the longest step
$T -m training.data.piper_voices_clips hey_tars          # ~1,100 more Piper voices
$T -m training.data.kokoro_clips hey_tars 20000 16000
OPENAI_API_KEY=... $T -m training.data.openai_clips      # also makes the held-out test voices
$T -m training.data.prosody_clips hey_tars 25000         # needs all of the above
$T -m training.data.accent_clips                         # non-English Piper voices (stage 2 only)

# 3. Real voices (about an hour each)
uv run python -m training.data.clean                     # which synthetic clips Vosk hears as labeled
$E -m training.data.real_lookalikes                      # "stars", "cars"... cut from LibriSpeech
$E -m training.data.vc_librispeech --per-speaker 80      # needs clean.py
$E -m training.data.prep_vctk && $E -m training.data.vc_vctk --per-speaker 60

# 4. Stage 1 (features ~2 h, training 20-45 min)
$M -m training.stage1.features synthetic
$M -m training.stage1.train generic
$M -m training.stage1.features user && $M -m training.stage1.train personal       # personal only

# 5. Stage 2 (a few minutes, plus its own held-out test)
$E -m training.stage2.train_check generic
$E -m training.stage2.train_check personal                                        # personal only

# 6. End to end, the way the assistant runs (30-60 min)
$E -m training.eval.pipeline $D/models/generic/hey_tars.tflite \
   --checks $D/models/generic/hey_tars_check.json plain --window 3.0 --all-user
$E -m training.eval.pipeline $D/models/personal/hey_tars.tflite \
   --checks $D/models/personal/hey_tars_check.json --window 2.5

# 7. Install: copy DATA/models/<setup>/* into the repo's models/<setup>/

# 8. (maintainer) the hosted clips, from this data folder (needs a Hugging Face write token to upload)
uv run --with huggingface_hub python -m training.hub export OUT && uv run --with huggingface_hub python -m training.hub upload OUT
```

Tip: for long steps, `nohup nice -n 19 ... > log 2>&1 &`, and keep it to two heavy jobs at once.

## The data folder

| Folder | What | Made by |
|---|---|---|
| `clips/<source>/hey_tars/{positive,near_miss}` | synthetic clips: `piper_libritts`, `piper_voices`, `kokoro`, `openai`, `prosody`, `accent` | step 2 |
| `bench/clips_heldout` | the 3 held-out OpenAI voices (90 wake phrases, 42 lookalikes): test only | `openai_clips.py` |
| `bench/interference/test`, `bench/LibriSpeech/test-clean` | test-only noise, rooms, TV hour, audiobooks | step 1 |
| `backgrounds`, `aug` | training noise, music, speech, rooms, babble, TV | step 1 |
| `LibriSpeech/train-clean-100`, `vctk` | real speakers, for voice conversion and lookalikes | step 1, `prep_vctk.py` |
| `clean` | lists of the synthetic clips that passed `clean.py` | `clean.py` |
| `vc`, `vc_vctk`, `real_lookalikes` | real voices: converted clips and cut words | step 3 |
| `mww`, `oww` | microWakeWord and openWakeWord checkouts, venvs, negative sets | setup |
| `features`, `trained_models` | stage 1 spectrograms and training runs | step 4 |
| `check` | stage 2 layers (`.pkl`) and their test rows | step 5 |
| `models/<setup>` | what to copy into the repo | steps 4, 5 |
| `results` | end-to-end results | step 6 |

The benchmark helpers the evaluation reuses (`tools/wakeword_bench.py`, `tools/verifier_bench.py`) default to
`~/wakeword_bench`; the scripts here point them at `DATA/bench` instead.

## The owner's recordings

`--user` (default `voice_data/alon/laptop`, gitignored) holds `hey_tars/`, `hey_tars_lookalikes/`, `tars_stop/`
and `speech/`, recorded with `voice-assistant --record-voice alon --mic laptop`: 30 takes of "hey TARS" (5 variations: normal, far,
quick, quiet, TV on; 6 takes each), 42 lookalikes, 30 "TARS stop" and 25 sentences. Files are numbered `000.wav`...
`training/common.py`'s `user_split` decides which half trains and which is held out: takes 0-2 of each variation
train, 3-5 test; for lookalikes the test take rotates; sentences alternate. The generic setup never uses any of
them, and the end-to-end test then uses all 30 (`--all-user`).

## What's left out, and why

- **The first rounds:** openWakeWord's own training (27% recall on the owner, replaced by microWakeWord), the first
  microWakeWord baselines (`train_mww.py`, `train_mww_v2.py`), and the "v3" training schedule (60k steps with a
  heavier false-alarm phase), which did worse everywhere. Its data and augmentation are what `features.py` builds.
- **Ablations A-C and E-H:** without background speech, with the owner's lookalike recordings, gentler positive
  augmentation, a bigger model, twice the steps. D (here: `personal`) won; the others are summarized in
  docs/wake-word.md.
- **Stage 1 variants trained on cleaned clips, on voice conversions, or on accented voices:** none beat the generic
  model here (cleaned and accented were clearly worse, voice conversions mixed); they are in the tried-and-dropped
  table.
- **Recognizer comparisons:** Whisper, Parakeet, Moonshine and sherpa-onnx as the check, and forced-choice scoring
  with Moonshine as extra features for the layer. `tools/verifier_bench.py` still compares recognizers.
- **Orchestration:** the shell scripts that chained steps overnight, progress monitors and smoke tests.
- **"TARS stop":** the clip generators still take `tars_stop`, but its models were parked.
