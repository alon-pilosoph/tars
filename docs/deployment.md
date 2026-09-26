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

## On the Mac, with a USB speakerphone

```bash
uv sync && cp .env.example .env    # then put your OpenAI and Deepgram API keys in .env
uv run voice-assistant --list-devices
```

Plug the speakerphone in and look for its name in the list. If it isn't marked as the default (`*`), put part of
its name in `config.toml`, for example `input_device = "PowerConf"` and `output_device = "PowerConf"`. Then:

```bash
uv run voice-assistant --mic-test   # speak: the level, speech and wake-word meters should move; no API key needed
uv run voice-assistant              # say "hey TARS", wait for the chime, ask something
uv run voice-assistant --web        # in a second terminal: http://127.0.0.1:8080
```

The first start downloads the double-check's recognizer (40 MB), the speech detector (2 MB), the end-of-turn model
(8 MB) and the speaker model (25 MB) into `models/`. Expect about 2.5 seconds from when you stop talking to TARS's
first word, a little more when you sounded mid-thought: TARS waits 0.8 s to be sure you're done, and prepares the
answer meanwhile ([response time](latency.md)). A web search adds a "Looking it up." first. macOS asks for microphone access the first time; if TARS hears nothing, check System
Settings → Privacy & Security → Microphone for your terminal.

## Install on the Pi

On Raspberry Pi OS (64-bit, Bookworm or later), with the speakerphone plugged in:

```bash
git clone <this repo> ~/voice-assistant
~/voice-assistant/deploy/install-pi.sh
```

[`deploy/install-pi.sh`](../deploy/install-pi.sh) installs PortAudio and uv, the Python packages (every one has a
ready-made build for the Pi, so nothing compiles), downloads the models, lists the audio devices, and installs and
starts both systemd user services, at boot too. The first run stops to ask for the keys in `.env` (OpenAI, and
Deepgram unless `[stt] provider = "openai"`); run it again after. It's safe to rerun. The two services are separate processes on purpose: a crash in one never takes the other
down. The web UI's build is committed, so the Pi needs no Node. By hand, the steps are at the top of
[`deploy/voice-assistant.service`](../deploy/voice-assistant.service) and
[`deploy/voice-assistant-web.service`](../deploy/voice-assistant-web.service).

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
- **Upgrades.** `git pull && uv sync --frozen && systemctl --user restart voice-assistant voice-assistant-web`. The
  database upgrades itself when either starts.

## Where the learning happens

| What | Learns from | Where | How long |
| --- | --- | --- | --- |
| **Voiceprints** (who's talking) | requests in a named voice (5+) | the Pi, on Re-cluster | seconds (built) |
| **The double-check's layer** (answer / ask / ignore) | labeled wakes | the Pi, on "Retrain now" | seconds (built) |
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
