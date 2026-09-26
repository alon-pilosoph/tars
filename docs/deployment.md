# Running TARS at home

TARS is local-first: the Raspberry Pi 5 records, serves the web UI and does the learning. Only what already goes to
OpenAI after a wake leaves the house.

```
                 home network (Wi-Fi)                               internet
┌──────────────── Raspberry Pi 5 ────────────────┐
│ voice-assistant.service                        │── request audio / text ──► OpenAI (STT, LLM, TTS)
│   wake → double-check → answer                 │     (only after "hey TARS")
│   writes voice_data/events/ (SQLite + WAVs)    │
│ voice-assistant-web.service  :8080             │◄── phone / laptop browser (http://<pi>.local:8080)
│   the React page + API over the same log       │◄── away from home: Tailscale (optional)
│   re-cluster, voiceprints, retrain the check   │── nightly encrypted backup ──► S3 / B2 (optional)
└────────────────────────────────────────────────┘
```

## Install on the Pi

```bash
sudo apt install libportaudio2
git clone <this repo> ~/voice-assistant && cd ~/voice-assistant
uv sync
cp .env.example .env               # add the OpenAI API key
uv run voice-assistant --mic-test  # check the speakerphone is found (names go in config.toml [audio])
```

Then install both systemd user services; the steps are at the top of
[`deploy/voice-assistant.service`](../deploy/voice-assistant.service) and
[`deploy/voice-assistant-web.service`](../deploy/voice-assistant-web.service). They're separate processes on
purpose: a crash in one never takes the other down. The web UI's build is committed, so the Pi needs no Node.

## Things to know

- **Reaching it.** Open `http://<pi-name>.local:8080` on the home network. It has no accounts, so don't forward the
  port to the internet; to use it away from home, put the Pi and the phone on
  [Tailscale](https://tailscale.com) and open `http://<pi-name>:8080`. It only answers to IP addresses, `localhost`,
  `*.local`, `*.ts.net` and single-label names; add any other name to `[web] allowed_hosts`.
- **Storage.** A wake window is 3 s (96 KB), a request about 4 s (130 KB). At around 60 events a day that's about
  12 MB a day. Audio that teaches nothing (no label, not even an automatic one) is dropped after
  `[learning] keep_audio_days` (60); labeled audio is kept, since it's the training data. A 64 GB card lasts years.
- **Backup.** SD cards die, and the labels are the valuable part. A nightly `restic` or `rclone` job sending an
  encrypted copy of `voice_data/` and `models/personal/` to S3 or Backblaze B2 costs cents a month.
- **Two processes, one log.** The assistant writes and the web UI reads and edits the same SQLite file (WAL mode,
  so neither blocks the other; a failed write never costs a reply). Speaker ID picks up new voiceprints on its own.
- **Upgrades.** `git pull && uv sync`, then restart both services. The database upgrades itself when either starts.

## Where the learning happens

| What | Learns from | Where | How long |
| --- | --- | --- | --- |
| **Voiceprints** (who's talking) | requests in a named voice (5+) | the Pi, on Re-cluster | seconds (built) |
| **The double-check's layer** (answer / ask / ignore) | labeled wakes and near-misses | the Pi, on "Retrain now" | seconds (planned) |
| **Stage 1, the wake model** | synthetic voices plus the household's labeled clips | a bigger machine, rarely | hours ([`training/`](../training/README.md)) |

"Retrain now" is described in [self-learning](self-learning.md#learning-from-it). Given what the experiments showed
(more stage 1 training made it worse, and accented clips hurt it), stage 1 retraining should be rare; most of the
gains should come from the double-check and the wake threshold.

## A first test run

With `[learning] log_events = true` and speaker ID on, run the assistant and the web UI side by side
(`uv run voice-assistant`, and `uv run voice-assistant --web --host 0.0.0.0` in a second terminal), then spend 20
minutes on this:

| Do | Expect in the web UI (press Refresh) |
| --- | --- |
| 8× "hey TARS" plus a real request (weather, a timer…) | conversations on Home, each with your voice |
| 2× "hey TARS", then say nothing | two wakes under Review: "Nobody spoke", no guess |
| "hey cars", "hey Mars", "hey stars" | "Did you call me?" or nothing; answer "no" and the wake is guessed not real |
| "hey TARS" quietly from across the room | maybe a near-miss; said again louder, it's guessed real |
| 10 minutes of TV in the background | ideally nothing; any false wake shows up in Review |
| "send me a lasagna recipe" | a link or a note under Sent, and "It's on the TARS page." |
| a second person, 5+ requests | a second voice in Voices |

Then answer the wakes in Review, and in Voices name your voice and re-cluster: the next bare "hey TARS" should get
"Yes, <name>?". Note down false wakes from the TV, missed wakes, wrong guesses, and anything that felt slow.

## If this ever became a product

For many households the picture flips: devices upload events to cloud storage, the UI is hosted with accounts, and
training runs as cloud jobs, where the payoff is a generic model improving from every household's opted-in labels.
That brings authentication, consent and retention for recordings made inside homes, and hosting costs. For one
household, the Pi-only setup is simpler and private.
