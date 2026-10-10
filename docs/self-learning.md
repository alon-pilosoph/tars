# Self-learning TARS

The generic wake models are trained without any household's voices, so they start out weakest on the voices they
will hear most: the owner's soft *t* is the main reason the generic setup misses a take. Self-learning closes that
gap from real use. TARS keeps every wake and near-miss with its audio, labels most of them automatically from what
happened next, and groups requests by voice for speaker ID. Both wake stages are then retrained together, as a pair,
on a bigger machine, and the new pair is installed only if it tests better end to end than the one in use, with
every version kept and one tap to switch back. Everything it learns from stays on the machine it runs on.

## The problem

- **The models need the household's own wakes**, especially the ones they missed, and those only exist once TARS is
  in use ([hearing "hey TARS"](wake-word.md)).
- **Labeling has to be nearly free.** Nobody reviews hundreds of wakes, so most labels must come from what happened
  next, and a person's answer is only asked for when nothing else explains a wake.
- **Logging must never cost a reply**, and a bad or half-copied model must never stop TARS listening.
- **The Pi doesn't train.** It runs the pairs; training happens elsewhere and comes back as a tested pair.

## What it keeps

Everything lives in `voice_data/events/` (gitignored): one SQLite database (`events.db`) and the audio and sent
files next to it. `[learning] log_events` in `config.toml` turns all of it on or off.

| Record | Written when | Holds |
|---|---|---|
| **Wake** | stage 1 fires | the 3 s the check heard, stage 1's score, what the check heard and how sure it was, the outcome (answer / ask / ignore), what followed |
| **Near-miss** | stage 1's score reaches 60% of its threshold, then falls away | the audio and the peak score |
| **Request** | the first thing said after a wake | its audio, transcript, speaker ID's guess and a voice embedding, on the wake |
| **Conversation** | someone says something after a wake | every turn: what each person said (with audio), what TARS said, turns that weren't meant for TARS |
| **Item** | TARS sends something | a link, note, list or text file, for a person or for the household |
| **Voice** | Regroup voices, or a person names, moves or merges one | a name, whether it's a person, and which requests belong to it |

A few rules keep the log honest:

- **Nothing is kept until stage 1 fires or comes close.** A "Yes, Alon?" or "Did you call me?" nobody answers, or
  a wake followed by noise, doesn't start a conversation; the conversation starts at the first thing someone says.
- **Writing never costs a reply.** Every write goes through `journal.py`, which catches its own failures (disk full,
  the database locked by the web UI), prints a warning and carries on. Near-misses are written off the mic loop.
- **The first request is stored once**, on its wake; the conversation's first turn points at the same audio.
- **Deleting** a conversation removes its turns, audio, items and the wake that started it. Deleting a wake alone
  keeps its conversation, without the wake or the request's audio. Ids are never reused, so a deleted row can't
  come back attached to something new.
- **Old audio that teaches nothing is dropped.** After `[learning] keep_audio_days` (60 by default), the audio of
  wakes and near-misses training wouldn't learn from is deleted; the row stays. That's a lonely near-miss, a wake
  nobody spoke after, and a wake the double-check turned away that nobody answered in Review (the check's own
  verdict isn't training data). Audio with a person's answer, or with an automatic label training uses, is kept.
  This runs at startup and once a day.

The assistant and the web UI share the database ([how](architecture.md#the-web-ui)), and an older one is upgraded
automatically when either starts. Code: `store.py` (the database and files), `events.py` (wakes, near-misses, voices),
`conversations.py` (conversations, turns, items), `journal.py` (what gets written when).

## Labels, mostly automatic

Each wake needs one answer for training: was it really "hey TARS"? Most answer themselves, from what happened next.
The rules run when reading, so they can change without touching stored data:

| What happened | Automatic label |
|---|---|
| a request followed | real |
| "Did you call me?" and they answered | real |
| a near-miss, followed within 6 s by a real wake | real (a missed "hey TARS", said again louder) |
| the check heard something else | not real |
| the reply wasn't meant for TARS | not real |
| "Did you call me?" and nobody answered | not real |
| it woke and nobody spoke; a lone near-miss | no guess |

A person's answer always wins over the automatic one. The web UI's **Review** tab shows only the wakes no
conversation explained, so reviewing is a few taps a day. A wake that led to a conversation can still be marked
"Not meant for TARS" from the conversation's menu (a TV line TARS answered, for example).

## Voices

The voice embedding of each wake's first request (WeSpeaker ResNet34, the model speaker ID uses) goes into leader
clustering: each request joins the closest voice if it's similar enough (cosine similarity 0.5), or else starts a
new one, oldest requests first so voice numbers stay stable. Whatever a person decided stays put when the Voices
page's Regroup voices runs: a request moved to a voice by hand, merged voices, names, and voices marked "not a
person".

Regrouping also rebuilds the voiceprints speaker ID uses, from exactly the named people with 5 or more requests
(two voices with the same name count as one person). A name that's gone (renamed, merged away, marked not a person)
stops being recognized, and a voice named after someone enrolled with `--enroll` replaces that voiceprint; other
`--enroll` voiceprints stay. The file is replaced in one step, and the running assistant picks it up before its next
greeting or request, without a restart.

Correcting a conversation's voice ("that was Stacey, not Alon") applies to the request that came with the wake, to
the conversation's other turns speaker ID heard the same way, and to what TARS sent "for whoever asked". Regrouping
alone doesn't override speaker ID's name for a conversation: only a voice a person pinned, named or marked counts.

Code: `clustering.py`, `speaker.py`.

## Learning from it

Both stages are retrained together, as a pair, on a bigger machine than the Pi, with `uv run --group training python
-m training.household --pi <pi>`: the wake model's training takes tens of minutes and needs the training data
folder, which `training/setup/prepare.sh` fills
(see [`training/`](../training/README.md#training-a-households-own-models)).
Each run trains one candidate pair, tests it and the pair in use side by side on the household's held-out wakes and
the standard test sets, and installs it only if it answers more of the household's real wakes (or lets fewer
through that weren't for TARS) and is no worse on anything else
([how candidates are compared](wake-word.md#closing-the-loop-the-household-retrain)). The web UI doesn't train
anything. Its Models page shows the pair in use and how it tested, how much new labeled data is waiting, and the
version history, where "Use this" switches back.

**What training learns from** (`events.learning_label`): every labeled wake and near-miss, with a person's label
winning over the automatic one. Near-misses labeled real (a near-miss followed within seconds by a real wake) are the
most valuable: they're the "hey TARS" the wake model missed. One automatic label is left out: "the double-check
heard something else", because it's only the check's own opinion. When the check wrongly turns down a soft "t"
and nobody reviews it, learning from that would teach both stages to ignore that voice harder; answering it in
Review makes it count.

**Versions** (`versions.py`). A trained pair (the wake model, the check's learned layer, and the threshold and check
window they were tuned for, with the test results it was chosen on) is installed with `voice-assistant
--install-models FOLDER` into `<learning folder>/wake_models/<setup>/v1/`, `v2/`..., and `history.json` says which
one is in use. The installed pair is `config.toml`'s `[wake]` settings. `--based-on VERSION` refuses to install a
pair that was tested against a version that's no longer in use. The running assistant looks every few seconds and
switches the whole pair without a restart, as it does after "Use this". A setup is named after the installed
check's folder, so switching `config.toml` to `models/personal/` starts a separate history.

Nothing about versions can stop TARS listening. Every file is written to a temporary file, flushed to disk and
swapped in, and a pair is copied into place all at once, so a power cut can't leave half of one. A pair is checked
before it's installed (the wake model loads, the layer parses, the settings make sense). If the history can't be
read, a pair is missing or damaged, or the installed pair has changed since the versions were made from it (an update
replaced it), TARS uses the installed pair and the Models page says why; the next install or "Use this" starts a new
history and keeps the old file aside. Version numbers are never reused. If a new pair fails to load while TARS runs,
it keeps listening with the one it has.

## Limitations

- **Labels come from behavior, not from listening.** The automatic rules can be wrong (a request that followed a TV
  wake, for example), and a wake the double-check wrongly turned down teaches nothing until someone answers it in
  Review.
- **One candidate per run.** Stage 1 training varies from run to run, so a run that isn't installed says little on
  its own; running it again gives another candidate, compared the same way.
- **The gate is strict on purpose.** A candidate that does better on some tests but worse on any other, such as one
  more lookalike or false answer let through, isn't installed without a person's yes. On the owner's 72 recordings, a
  run that improved other voices from 94.4% to 97.8% in quiet but let more lookalikes through (3.0% to 4.8%) and gave
  one false answer an hour on audiobooks wasn't installed
  ([details](../training/README.md#training-a-households-own-models)).
- **Training needs a second machine** with the prepared data folder (about 80 GB); the Pi can't retrain itself.

## Next steps

The first real household data comes from a week of real use ([roadmap](roadmap.md), step 1). At the end of it, what's
left in Review is answered and the voices are named, and the Models page shows how many real, missed and not-real
wakes there are to train on, which is what the household retrain learns from.
