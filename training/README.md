# Training the "hey TARS" models

The scripts here make TARS's two wake models: the microWakeWord streaming model (stage 1) and the double-check's learned
layer (stage 2). There are two ways to use them. **Retraining for a household** takes one command and about 25 minutes
on a prepared machine, and installs the new pair only if it tests better than the one in use, unless you answer yes when
it asks or pass `--force`. **Rebuilding from scratch** makes every clip, the generic models and the test sets from
public data, in a day or two of mostly unattended CPU. Why the pipeline is built this way, and the evidence behind each
choice, is in [docs/wake-word.md](../docs/wake-word.md#how-the-models-were-built).

| Setup | File | Made by |
|---|---|---|
| Generic (committed, the default) | `models/generic/hey_tars.tflite` | `stage1/train.py generic` |
| | `models/generic/hey_tars_check.json` | `stage2/train_check.py generic` |
| A household's pair (on its own machines) | `voice_data/events/wake_models/<setup>/vN/` | `household.py` |
| Personal (local only, gitignored) | `models/personal/hey_tars.tflite` | `stage1/train.py personal` |
| | `models/personal/hey_tars_check.json` | `stage2/train_check.py personal` |

## Training a household's own models

What most people want: a wake model and double-check tuned to the voices and rooms of one household, from the wakes
TARS logged there (every labeled wake and near-miss; see [self-learning](../docs/self-learning.md#learning-from-it)).

**Prerequisites:** a Mac or a Linux machine with about 80 GB free, and TARS running on it or on a Pi reachable over
SSH (with the repo at `~/voice-assistant`, or `--remote` for another folder).

```bash
bash training/setup/prepare.sh                         # once: ~45 GB of downloads and 3-4 hours, resumable
uv run --group training python -m training.household   # TARS runs on this machine
uv run --group training python -m training.household --pi pi@tars.local   # TARS runs on a Pi
```

`prepare.sh` sets up microWakeWord's environment and negative sets, downloads the noise, rooms and test audio from
their own sources, fetches the prepared clips from Hugging Face
([hey-tars-training](https://huggingface.co/datasets/alon-p/hey-tars-training), CC BY 4.0, and
[hey-tars-training-nc](https://huggingface.co/datasets/alon-p/hey-tars-training-nc), CC BY-NC-SA 4.0, made by
`training/hub.py`; the voices, never any household's recordings), and builds the wake model's features.

`training.household` then:

1. checks that the data folder has everything it needs;
2. copies the labeled wakes and near-misses and the pair in use from TARS (nothing there is changed);
3. holds every fifth wake out for testing, with any wake seconds from it, so the same audio is never both trained
   and tested on;
4. trains one candidate pair, both stages, on the generic recipe plus the household's clips (about 25 minutes);
5. tests the candidate and the pair in use end to end: the household's held-out wakes, the held-out voices and
   lookalikes in 8 conditions, and false answers per hour on TV and audiobooks;
6. prints the comparison and installs the candidate only if it answers more of the household's real wakes (or lets
   fewer through that weren't for TARS) and does no worse on anything else.

If the candidate isn't better on everything, it asks; `--only-if-better` doesn't ask, nor does a run without a
terminal (as under `nohup`), and `--force` installs anyway after showing why. The Pi switches to an installed pair
within seconds, and its Models page can switch back. Each run is kept in `DATA/household/<run>/`.

Each run trains one candidate. Stage 1 training isn't deterministic: across 30 retrains on the same data, the share
of the owner's training-half takes answered ranged from 19% to 73% ([reproducibility](#reproducibility)), so one run
says little on its own. Running it again gives another candidate, compared against the pair in use the same way. An
example of the gate at work: on the owner's 72
recordings (30 real, 42 lookalikes), a run took 19 minutes. Other voices went from 94.4% to 97.8% answered in quiet
and 83.5% to 88.3% in noise, but lookalikes let through went from 3.0% to 4.8% and audiobooks brought one false
answer per hour, so it wasn't installed.

## Rebuilding everything from scratch

The rest of this page is how the generic and personal models and the test sets were made, from nothing.

### Prerequisites

- macOS or Linux, [uv](https://docs.astral.sh/uv/), git, curl, espeak-ng for Kokoro and FFmpeg for reading audio
  (`brew install espeak-ng ffmpeg`).
- About 130 GB of disk. The biggest parts are microWakeWord's negative sets (a 20 GB download, 41 GB unpacked),
  stage 1 features (30 GB), MUSAN and the room responses (17 GB), and the clips (16 GB).
- An OpenAI API key for `openai_clips.py` (about $0.60).
- A day or two of mostly unattended CPU time. Everything here ran on a MacBook at 4 threads and `nice`.
- For the cloned test voices only: a Google account with Colab (a T4 GPU is enough) and Drive, and a Mozilla Data
  Collective account ([below](#cloned-common-voice-speakers)).
- For the personal setup only: the owner's recordings ([below](#the-owners-recordings)). They never leave the
  machine.

### The data folder and environments

Everything goes into one data folder: `--data DIR` on every script, or `TARS_TRAINING_DATA`, default
`~/tars-training`. The scripts run from the repo root as modules (`python -m training.stage1.train ...`), each in
one of four environments, because their dependencies don't mix:

| Environment | Made by | Used for |
|---|---|---|
| `DATA/tts/.venv` (Python 3.14) | `setup/tts_env.sh` | Piper, Kokoro, OpenAI clips, pitch/tempo |
| `DATA/mww/.venv` (Python 3.13) | `setup/mww_env.sh` | microWakeWord features and training, backgrounds, interference |
| `DATA/eval/.venv` (Python 3.14) | `setup/eval_env.sh` | stage 2, voice conversion, forced alignment, end-to-end tests |
| the repo's own (`uv run --group training`) | `uv sync --group training` | `household.py`, `hub.py`, `data/clean.py` (uses the assistant's Vosk model) |

`data/piper_libritts.sh` makes a fifth, openWakeWord's, just to generate the Piper LibriTTS clips.

### Run order

Run from the repo root. Rough times are from the development Mac. Every script reads the data folder from
`TARS_TRAINING_DATA`, so set it once; `U` is the owner's recordings, for the personal setup.

```bash
export TARS_TRAINING_DATA=~/tars-training; D=$TARS_TRAINING_DATA; U=voice_data/<name>/laptop

# 0. Environments (and microWakeWord's negative sets, a 20 GB download)
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
uv run --group training python -m training.data.clean  # which synthetic clips Vosk hears as labeled
$E -m training.data.real_lookalikes                      # "stars", "cars"... cut from LibriSpeech
$E -m training.data.vc_librispeech --per-speaker 80      # needs clean.py
$E -m training.data.prep_vctk && $E -m training.data.vc_vctk --per-speaker 60

# 4. Stage 1 (features ~2 h, training 20-45 min)
$M -m training.stage1.features synthetic
$M -m training.stage1.train generic
$M -m training.stage1.features user --user $U && $M -m training.stage1.train personal   # personal only

# 5. Stage 2 (a few minutes, plus its own held-out test)
$E -m training.stage2.train_check generic --user $U         # --user: test on the owner's recordings too
$E -m training.stage2.train_check personal --user $U        # personal only

# 6. End to end, the way the assistant runs (30-60 min)
$E -m training.eval.pipeline $D/models/generic/hey_tars.tflite \
   --checks $D/models/generic/hey_tars_check.json plain --window 3.0 --user $U --all-user
$E -m training.eval.pipeline $D/models/personal/hey_tars.tflite \
   --checks $D/models/personal/hey_tars_check.json --window 2.5 --user $U

# 6b. Several stage-1 candidates, side by side: stage 1 on the speaker panel, the owner's halves, the held-out voices,
#     TV and audiobooks, then each at the strictness of the pair in use (the same lookalikes and fires an hour), ranked
#     by the share of panel speakers it answers at least 80% of the time
$E -m training.eval.candidates score $D/runs/*.tflite --user $U
$E -m training.eval.candidates rank --baseline shipped

# 7. Install: copy DATA/models/<setup>/* into the repo's models/<setup>/

# 7b. (optional) The large pair, stage 1 four times as wide with the cloned speakers added (RAW: below)
$E -m training.data.cloned_clips $RAW && $M -m training.stage1.features cloned
$M -m training.stage1.train generic --large --cloned     # DATA/models/generic-large/hey_tars.tflite, ~3 h

# 8. (maintainer) the hosted clips, from this data folder (needs a Hugging Face write token to upload)
uv run --group training python -m training.hub export OUT && uv run --group training python -m training.hub upload OUT
```

Tip: for long steps, `nohup nice -n 19 ... > log 2>&1 &`, and keep it to two heavy jobs at once.

**Training several stage 1 candidates.** Each run of step 4 overwrites `DATA/models/<setup>/hey_tars.tflite`, so copy
each candidate aside (to `DATA/runs/<name>.tflite`, next to the pair in use copied as `DATA/runs/shipped.tflite`),
rank them with step 6b, check the leaders end to end with step 6 and the other test sets below, and install the one
that does best across them. Step 5 is deterministic and only needs rerunning when its data changes.

### Test sets

Each set answers a different question about a candidate ([why](../docs/wake-word.md#how-it-was-measured)). All but
the cloned speakers come out of the run order above:

| Set | Built by | Where |
|---|---|---|
| Held-out synthetic voices: 3 OpenAI voices, 90 wake phrases, 42 lookalikes | step 2, `openai_clips.py` | `bench/clips_heldout` |
| Speaker panel: LibriSpeech and VCTK speakers through kNN-VC (score stage 1 alone on it: the check trains on these voices) | step 3, `vc_librispeech.py`, `vc_vctk.py` | `vc`, `vc_vctk` |
| Cloned Common Voice speakers | `data/clone_voices.ipynb`, below | a folder of raw clips, outside `clips/` |
| The owner's recordings | `voice-assistant --record-voice`, [below](#the-owners-recordings) | `voice_data/<name>/laptop` |
| Test-only noise and rooms, an hour of TV, audiobooks | step 1 | `bench/interference/test`, `bench/LibriSpeech/test-clean` |

#### Cloned Common Voice speakers

Real speakers from [Mozilla Common Voice](https://commonvoice.mozilla.org/) (CC0), cloned zero-shot with Chatterbox
Turbo (MIT) to say six "hey TARS" variants ("Hey Tarss.", "Hey Tarss!", "Hey TARS.", "Hey, TARS?", "Hey Tars!",
"hey tars") and six lookalikes ("Hey stars.", "Hey cars.", "Hey Tara.", "Hey bars.", "Hey guards.", "Hey tarot.").
Everything up to the clips runs in one Colab notebook, `training/data/clone_voices.ipynb`.

1. **Get access.** Create a Mozilla Data Collective account and an API key, and accept the terms on the page of
   each Common Voice dataset you'll use (the notebook's `SETS`). Downloads count against a daily quota.
2. **Open the notebook in Colab** on a GPU runtime (a T4 is enough, about a second a clip), and add the key to
   Colab Secrets as `MDC_API_KEY`. The notebook mounts Google Drive and writes to `MyDrive/tars-cloned`.
3. **Pick the subsets** in `USE`. The set used here is `irish`, `scottish`, `malaysian` and `us-south`: 486
   speakers, 2,916 wake phrases and 2,916 lookalikes, a few hours on a T4.
4. **Run the speakers cell.** Each subset is downloaded through the API (a `POST` to
   `https://mozilladatacollective.com/api/datasets/<id>/download` with the key as a Bearer token returns a
   presigned URL), its chosen speakers' clips are unpacked, and the archive is deleted. Per subset it takes up to 600
   speakers with at least 3 clips, as many women as the subset has up to half, with a fixed seed, and builds a 5-12 s
   reference of each speaker's own speech.
5. **Run the clone cell.** Two worker processes share the GPU, one process per batch of 50 voices, and each batch is
   written to Drive as its own archive. After a disconnect, rerun the cell: finished batches are skipped.
6. **Run the bundle cell.** It writes `MyDrive/tars-cloned/tars-cloned.tar` and releases the runtime. Download it
   and unpack it into a raw folder, which ends up with `positive/` and `near_miss/`:

   ```bash
   RAW=<a folder for the raw clips>; mkdir -p $RAW && tar -xf tars-cloned.tar -C $RAW
   for t in $RAW/worker*-*.tar; do tar -xf $t -C $RAW && rm $t; done
   ```

7. **Evaluate on the raw clips as they are**: one wake phrase and one lookalike per speaker, in all 8 test
   conditions (the large pair trains on these speakers, so they don't test it). Filtering test clips would flatter
   the check:

   ```bash
   $E -m training.eval.pipeline CANDIDATE.tflite --checks CHECK.json --window 3.0 --set cloned --cloned $RAW
   ```

Chatterbox's Perth watermark is kept. The terms forbid trying to identify speakers and rehosting the audio, so the
clips stay on the machines that made them and are never part of the hosted datasets.

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
| `check` | stage 2 layers (`.pkl`) and their training and test rows | step 5 |
| `models/<setup>` | what to copy into the repo | steps 4, 5 |
| `results` | end-to-end and benchmark results | step 6, `tools/*_bench.py` |
| `household/<run>` | a household run: its copy of the wakes, clips, the new pair and its results | `household.py` |
| `hub` | the hosted datasets, as downloaded | `hub.py download` |

Two benchmarks in `tools/` take the same `--data`: `wakeword_bench.py` (stage 1 alone, at several thresholds) and
`verifier_bench.py` (speech recognizers compared as the double-check; the ones other than Vosk need their models
in `DATA/asr_models`).

## The owner's recordings

`--user`, a folder like `voice_data/<name>/laptop` (gitignored), holds `hey_tars/`, `hey_tars_lookalikes/`,
`tars_stop/` and `speech/`, recorded with `voice-assistant --record-voice <name> --mic laptop`: 30 takes of "hey
TARS" (5 variations: normal, far, quick, quiet, TV on; 6 takes each), 42 lookalikes, 30 "TARS stop" and 25
sentences. Files are numbered `000.wav`...
`training/common.py`'s `user_split` decides which half trains and which is held out: takes 0-2 of each variation
train, 3-5 test; for lookalikes the test take rotates; sentences alternate. The generic setup never uses any of
them, and the end-to-end test then uses all 30 (`--all-user`).

## Reproducibility

- **Stage 2 is deterministic.** `stage2/train_check.py generic` on the same data, in the environment
  `setup/eval_env.sh` pins, gives the same weights and bias.
- **Stage 1 is not.** microWakeWord's augmentation is unseeded, and candidates trained on the same data and recipe
  differ substantially: across 30 retrains, both stages together answered between 19% and 73% of the owner's
  training-half takes, averaged over the 8 conditions (the shipped pair: 64%). So a rebuild gives a candidate, not
  the shipped file, and is judged on the test sets like any other; a difference between two single runs that is
  smaller than this spread isn't evidence.
- **Environments are pinned** to exact versions, kept at the newest that work: `setup/mww-constraints.txt` and
  `setup/oww-constraints.txt`, and the versions in `setup/tts_env.sh` and `setup/eval_env.sh`. Where the Python
  isn't the newest, something holds it back: microWakeWord's environment is on 3.13 until TensorFlow 2.22 is out
  (only its release candidate has 3.14 builds), and openWakeWord's clip generator on 3.12, the newest
  piper-phonemize-cross has builds for. The microWakeWord, piper-sample-generator and openWakeWord checkouts are
  pinned to commits, each its newest.
- **The shipped models predate the current pins.** The "hey TARS" models in `models/` were made with the previous
  pinned versions. On a data folder from before, each setup script replaces an environment on another Python, and
  `data/piper_libritts.sh` sets openWakeWord's up again.

## What isn't here

The alternatives evaluated on the way (openWakeWord, other microWakeWord schedules, stage 1 on filtered, converted or
accented clips, other recognizers as the check) are in
[docs/wake-word.md](../docs/wake-word.md#design-decisions), with their results; only the scripts behind the shipped
models and the test sets are here.

**"TARS stop"**, the interrupt phrase under development ([wake word](../docs/wake-word.md#tars-stop),
[roadmap](../docs/roadmap.md), step 2), is trained with `stage1/train.py generic --phrase tars_stop` and tested with
`eval/pipeline.py --phrase tars_stop` (with `--user`, on the owner's "TARS stop" takes too). A learned layer for it,
`stage2/train_check.py generic --phrase tars_stop`, did worse on the owner and isn't used. Its rejects include "stop"
on its own and in sentences, because a model trained without them learned the word "stop". The generators add to what's there, so on a data folder made before those rejects
existed, delete `DATA/clips/{kokoro,piper_voices}/tars_stop/near_miss` first, or the new rejects are never made.
