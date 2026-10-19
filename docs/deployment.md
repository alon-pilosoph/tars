# Running TARS at home

How to run TARS on a Mac while working on it, and on a Raspberry Pi 5 as the household's assistant: install,
operation, storage and backup, and a first test run.

TARS is local-first: the Pi listens, records, serves the web UI and learns voiceprints. Nothing leaves the house
until a wake is confirmed, and then only the turn itself, and only to the services `.env` has keys for:

- **Deepgram,** on its EU servers: your request's audio, and a voice message sent on Telegram, for speech to text.
- **Cartesia:** TARS's reply text, a sentence at a time, for its voice.
- **Groq:** the conversation so far (requests tagged with who's speaking, and TARS's replies), for the answer.
- **Cerebras:** the same conversation when Groq is being skipped, fails, or hasn't started answering within 0.5 s.
- **OpenAI:** the same conversation for turns that need the web or the TARS page, or when neither Groq nor Cerebras
  can answer, and a request's recording (or a Telegram voice message) when Deepgram fails.
- **Telegram,** with `[telegram]` on: what's written and said in the household's chats with TARS, and what it sends
  there ([TARS on Telegram](telegram.md)).
- **Your own S3 bucket,** with `[backup]` on: a copy of everything TARS keeps, encrypted at rest, every 15 minutes
  ([backing up to S3](backup.md)).

Without the backup, the event log, the audio kept for learning, voiceprints and the web UI never leave it
([what leaves the machine](architecture.md#what-leaves-the-machine)).

```
                 home network (Wi-Fi)                               internet
┌──────────────── Raspberry Pi 5 ────────────────┐
│ voice-assistant.service                        │── request audio / text ──► Deepgram, Groq, Cerebras, OpenAI, Cartesia
│   wake → double-check → answer                 │     (only after "hey TARS")
│   writes voice_data/events/ (SQLite + WAVs)    │
│ voice-assistant-web.service  :8080             │◄── phone / laptop browser (http://<pi>.local:8080)
│   the React page + API over the same log       │◄── away from home: Tailscale (optional)
│   regroup voices, switch wake models           │
│ voice-assistant-backup.timer (optional)        │── every 15 min, encrypted ──► an S3 bucket
│ voice-assistant-telegram.service (optional)    │── asks for new messages ──► Telegram ◄── the household's phones
└────────────────────────────────────────────────┘
```

## On the Mac, with a USB speakerphone

```bash
uv sync --all-extras && cp .env.example .env    # then your keys in .env: OpenAI, Deepgram, Groq, Cerebras, Cartesia
uv run voice-assistant --list-devices
```

Plug the speakerphone in and look for its name in the list. If it isn't marked as the default (`*`), put part of
its name in `config.local.toml` (this machine's own settings, never committed, over `config.toml`):

```toml
[audio]
input_device = "PowerConf"
output_device = "PowerConf"
```

Set
`[llm] home_city` and `home_country` to where the house is (Tel Aviv, IL in the repo), so "what's the weather?" is
about home. Then:

```bash
uv run voice-assistant --mic-test   # speak: the level, speech and wake-word meters should move; no API key needed
uv run voice-assistant              # say "hey TARS" and ask something
uv run voice-assistant --web        # in a second terminal: http://127.0.0.1:8080
```

The first start downloads the double-check's recognizer (40 MB), the speech detector (2 MB), the end-of-turn model
(8 MB) and the speaker model (25 MB) into `models/`. Expect about 1.2 to 1.5 seconds from when you stop talking to
TARS's first sound, a little more when you sounded mid-thought: Deepgram's Flux decides when you're done, and TARS
prepares the answer meanwhile ([response time](latency.md)). A web search adds a "Looking it up." first. macOS asks
for microphone access the first time; if TARS hears nothing, check System Settings → Privacy & Security →
Microphone for your terminal.

## Install on the Pi

On Raspberry Pi OS (64-bit, Bookworm or later), with the speakerphone plugged in:

```bash
git clone https://github.com/alon-pilosoph/tars.git ~/voice-assistant
~/voice-assistant/deploy/install-pi.sh
```

[`deploy/install-pi.sh`](../deploy/install-pi.sh) installs PortAudio and uv, the Python packages (every one has a
ready-made build for the Pi, so nothing compiles), downloads the models, lists the audio devices, and installs and
starts both systemd user services, at boot too, then runs `voice-assistant --check`. Every key is optional: it says what
TARS does without the ones `.env` doesn't have, and stops only when hearing, thinking or speaking has no service at all;
add keys and run it again ([running on the keys there are](architecture.md#running-on-the-keys-there-are)). It's safe to
rerun, and it restarts the services, so after a `git pull` it puts the new code in use. A mistake in `config.toml`, or a
stage left with no key, stops a service instead of restarting it every few seconds: `journalctl` says what's wrong. So
does a missing speakerphone, and plugging it in starts TARS again
([`deploy/99-tars-speakerphone.rules`](../deploy/99-tars-speakerphone.rules)). The
web UI's build is committed, so the Pi needs no Node. By hand, the steps are at the top of
[`deploy/voice-assistant.service`](../deploy/voice-assistant.service) and
[`deploy/voice-assistant-web.service`](../deploy/voice-assistant-web.service). With `[telegram]` on, it also installs
and starts [`deploy/voice-assistant-telegram.service`](../deploy/voice-assistant-telegram.service)
([setting it up](telegram.md#setting-it-up)).

## Recognizing people from the start

Voices are grouped in the web UI as people use TARS, and naming one makes a voiceprint. To recognize someone before
that, record and enroll them:

```bash
uv run voice-assistant --record-voice NAME   # ~10 min guided session: wake phrases and read-aloud sentences
uv run voice-assistant --enroll NAME         # NAME's voiceprint from those sentences
```

## Things to know

- **Reaching it.** Open `http://<pi-name>.local:8080` on the home network. It has no accounts, so don't forward the
  port to the internet; to use it away from home, put the Pi and the phone on
  [Tailscale](https://tailscale.com) and open `http://<pi-name>:8080`. It only answers to IP addresses, `localhost`,
  `*.local`, `*.ts.net` and single-label names; add any other name to `[web] allowed_hosts`. For a name of your
  own, such as `home.example.com`, point its DNS A record at the Pi's Tailscale address (`100.x.y.z`): it resolves
  anywhere, but only connects from your tailnet.
- **Storage.** A wake window is 3 s (96 KB), a request about 4 s (130 KB). At around 60 events a day that's about
  12 MB a day. Audio training wouldn't learn from is dropped after `[learning] keep_audio_days` (60); audio with
  an answer, or an automatic label training uses, is kept, since it's the training data. A 64 GB card lasts years,
  and with the backup on, a request's audio leaves the card after a month.
- **Backup.** SD cards die, and the labels are the valuable part. With `[backup]` on, everything TARS keeps goes to an
  S3 bucket every 15 minutes, with a daily snapshot of the database, voiceprints and wake models, and old audio moves
  to Glacier Deep Archive ([backing up to S3](backup.md)).
- **Separate processes, one log** ([how](architecture.md#the-web-ui)). Speaker ID picks up new voiceprints on its own.
- **Upgrades.** `git pull && deploy/install-pi.sh`. The database upgrades itself when either service starts.
- **When something's wrong**, `uv run voice-assistant --check` checks everything in one go: the API keys (a warning
  with what TARS does without any that are missing), both audio devices, the local models, echo cancellation (only
  when it's on; it warns when the devices' delay is over 0.5 s), the voiceprints, OpenAI, Groq, Cerebras, Deepgram,
  Cartesia and Telegram (each with how long it took, Deepgram on the servers `deepgram_host` names, Telegram's bot
  token when `[telegram]` is on, and any without a key or not in use skipped), the voice (it says "TARS is ready."),
  the mic (3 s of listening: muted, too quiet or fine), storage, the web UI, Home Assistant and each device in the
  house (with `[home]` devices), the backup (the bucket, and how the last pass went) and the systemd services. Each
  problem comes with what to do about it, and it exits 1 if anything is broken. It never hangs on a device: one that
  doesn't open in time is a failure. Without a terminal, the web UI's **Status** page shows much of the same, live:
  whether each process is running, the services with their own status pages, and the Pi's temperature, power and
  disk.

## A first test run

With `[learning] log_events = true` and speaker ID on, run the assistant and the web UI side by side
(`uv run voice-assistant`, and `uv run voice-assistant --web --host 0.0.0.0` in a second terminal), then spend 20
minutes on this:

| Do | Expect in the web UI (press Refresh) |
| --- | --- |
| 8× "hey TARS" plus a real request (the weather, a quick fact…) | conversations on Home, each with your voice |
| 2× "hey TARS", then say nothing | two wakes under Review: "Nobody spoke", no guess |
| "hey cars", "hey Mars", "hey stars" | "Did you call me?" or nothing; answer "no" and the wake is guessed not real |
| "hey TARS" quietly from across the room | maybe a near-miss; said again louder, it's guessed real |
| 10 minutes of TV in the background | ideally nothing; any false wake shows up in Review |
| "send me a lasagna recipe" | a link or a note under Sent, and "It's on the TARS page." |
| a second person, 5+ requests | a second voice in Voices |

Then answer the wakes in Review, and in Voices name your voice and press Regroup voices: the next bare "hey TARS"
should get "Yes, <name>?". Note down false wakes from the TV, missed wakes, wrong guesses, and anything that felt slow.

## Design decisions

### Where the learning happens

| What | Learns from | Where | How long |
| --- | --- | --- | --- |
| **Voiceprints** (who's talking) | requests in a named voice (5+) | the Pi, on Regroup voices | seconds |
| **The wake model and its double-check**, as a pair | synthetic voices plus the household's labeled wakes and near-misses | a bigger machine, now and then | tens of minutes ([`training/`](../training/README.md)) |

**Decision: the Pi runs the models; a bigger machine trains them.** Voiceprints rebuild in seconds, so the Pi does
that itself. Training a wake pair takes tens of minutes and needs the training data folder, so it runs elsewhere and
the Pi only runs the pairs; see [self-learning](self-learning.md#learning-from-it) for how they're installed and
switched. The experiments where more wake model training made it worse, and accented clips hurt it, were single
runs, and stage 1 varies widely from run to run
([training](../training/README.md#reproducibility)). So a new pair should be trained rarely, once
there's a good number of new labeled wakes, more than once per attempt, and always tested end to end before it's
installed.

### If this ever became a product

For many households the picture flips: devices upload events to cloud storage, the UI is hosted with accounts, and
training runs as cloud jobs, where the payoff is a generic model improving from every household's opted-in labels.
That brings authentication, consent and retention for recordings made inside homes, and hosting costs. For one
household, the Pi-only setup is simpler and private, which is why TARS is built that way.

## Limitations

- **No accounts.** The web UI is for the home network, or a tailnet away from home; never forward its port.
- **The backup is AWS's.** It needs an S3 bucket and an IAM key ([backing up to S3](backup.md)); without them,
  nothing leaves the Pi but the turns themselves.
- **Measured on the development Mac.** The wake stages' cost and the response times come from the Mac; the Pi should
  be comfortable (stage 1 is tiny), but running the wake model and the voice's playback at once on the Pi is still to
  be checked ([what's next](roadmap.md#2-tars-stop-interrupting-a-reply)).
