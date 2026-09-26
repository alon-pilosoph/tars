# Self-learning TARS

TARS starts from the generic wake models (see [hearing "hey TARS"](wake-word.md)) and improves from the way the
household actually uses it. Everything it learns from stays on the machine it runs on.

## What it keeps

Everything lives in `voice_data/events/` (gitignored): one SQLite database (`events.db`) and the audio and sent
files next to it. `[learning] log_events` in `config.toml` turns all of it on or off.

| Record | Written when | Holds |
|---|---|---|
| **Wake** | stage 1 fires | the 3 s the check heard, stage 1's score, what the check heard and how sure it was, the outcome (answer / ask / ignore), what followed |
| **Near-miss** | stage 1 comes within 60% of its threshold, then falls away | the audio and the peak score |
| **Request** | the first thing said after a wake | its audio, transcript, speaker ID's guess and a voice embedding, on the wake |
| **Conversation** | someone says something after a wake | every turn: what each person said (with audio), what TARS said, turns that weren't meant for TARS |
| **Item** | TARS sends something | a link, note, list or text file, for a person or for the household |
| **Voice** | re-clustering, or a person names, moves or merges one | a name, whether it's a person, and which requests belong to it |

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
  wakes and near-misses that have no label at all, neither a person's nor an automatic one, is deleted; the row
  stays. Labeled audio is training data and is kept. This runs at startup and once a day.

The assistant writes and the web UI reads and edits the same database from two processes: it runs in WAL mode, and
every change of more than one row is one transaction. Older databases are upgraded automatically when either process
starts. Code: `store.py` (the database and files), `events.py` (wakes, near-misses, voices), `conversations.py`
(conversations, turns, items), `journal.py` (what gets written when).

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
new one, oldest requests first so voice numbers stay stable. Later turns keep their embeddings too, for clustering
them later. Whatever a person decided stays put when re-clustering: a request moved to a voice by hand, merged
voices, names, and voices marked "not a person".

Re-clustering also rebuilds the voiceprints speaker ID uses, from exactly the named people with 5 or more requests
(two voices with the same name count as one person). A name that's gone (renamed, merged away, marked not a person)
stops being recognized, and a voice named after someone enrolled with `--enroll` replaces that voiceprint; other
`--enroll` voiceprints stay. The file is replaced in one step, and the running assistant picks it up before its next
greeting or request, without a restart.

Correcting a conversation's voice ("that was Stacey, not Alon") applies to the request that came with the wake, to
the conversation's other turns speaker ID heard the same way, and to what TARS sent "for whoever asked". An
automatic re-cluster alone doesn't override speaker ID's name for a conversation: only a voice a person pinned,
named or marked counts.

Code: `clustering.py`, `speaker.py`.

## Learning from it

"Retrain now" on the Models page retrains **stage 2**, the double-check's learned layer, from these labels, on the
machine TARS runs on (`retrain.py`):

1. **Gather the labeled wakes.** A person's label, or the automatic one, except "the double-check heard something
   else": that one is only the check's own opinion, and learning from it would teach the check what it already
   thinks. Near-misses aren't used: the check never judges them.
2. **Keep every fifth aside** (by id), so the held-out wakes are the same ones every time and the test only grows.
3. **Run Vosk once** on each wake's saved audio. What it found is cached in `checks/<setup>/features.npz`.
4. **Fit a candidate from scratch**, the same way `training/stage2/train_check.py` fitted the installed layer, on
   that layer's own 13,000 training examples plus the household's wakes (each weighted 5, as the personal layer
   weighs the owner's recordings). The examples ship next to the layer, as `models/generic/hey_tars_check_data.npz`
   (58 KB, made by `training/stage2/export_data.py`), so nothing needs the training data folder.
5. **Compare** the candidate with the layer in use, end to end, on the household's held-out wakes and on a fixed test
   set that also ships in that file: 90 held-out voices saying "hey TARS" and 42 lookalikes, each in the 8 conditions
   of the end-to-end test, plus every window the wake model fired on in an hour of TV and an hour of audiobooks.
   A clip the wake model missed counts as missed, whatever the check would say.
6. **Swap it in only if it's better on the household's wakes and no worse on anything**: more held-out "hey TARS"
   answered, or fewer held-out wakes that weren't for TARS let through, and not one clip or false answer worse
   anywhere else. Otherwise the page shows the comparison and keeps the current layer.
7. **The running assistant switches** at its next wake, without a restart.

A retrain takes about a second, plus the recognizer's pass over wakes it hasn't seen yet. With too few wakes (none
held out yet), it says so and changes nothing.

**Versions.** Each version that was swapped in is kept in `<learning folder>/checks/<setup>/` (`v1.json`,
`v2.json`...), with `history.json` saying which one is in use. The Models page lists them, newest first, with the
installed layer (`config.toml`'s `check_model`) last, and "Use this" goes back to any of them. The assistant reads
`history.json` on every wake, and notices a rewritten file too, so a rollback also applies from the next wake. A
setup is named after the installed layer's folder, so switching `check_model` to `models/personal/` starts a
separate history. Code: `checks.py`.

Nothing about versions can stop TARS listening. Every file is written to a temporary file, flushed to disk and
swapped in, so a power cut can't leave half of one. If the history can't be read, a version file is missing or
damaged, or the installed layer has changed since the versions were made from it (an update replaced it), TARS uses
the installed layer and the Models page says why; the next retrain or "Use this" starts a new history and keeps the
old file aside. Version numbers are never reused. If someone picks another version while a retrain runs, the
candidate isn't swapped in (it was compared with the old one), and the page says so. A wake whose audio can't be
read is left out, and any other failure is shown on the Models page, keeping what the recognizer got through.

**Stage 1** (the wake model) is a different job: retraining it on the household's real "hey TARS" clips and
near-misses is what fixes wakes that stage 1 misses entirely, but it takes hours on a bigger machine (see
[`training/`](../training/README.md)). That's planned as an occasional job that pulls the labeled clips and installs
the new model through the same version history.
