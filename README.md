# TARS

[![CI](https://github.com/alon-pilosoph/tars/actions/workflows/ci.yml/badge.svg)](https://github.com/alon-pilosoph/tars/actions/workflows/ci.yml)

A household voice assistant you can take apart: say "hey TARS", ask something, and it answers in TARS's voice.
It runs on a Raspberry Pi 5 with a USB speakerphone, or on a Mac while you work on it.

**[See how it works, from wake word to answer →](https://tars.alonp.dev)**

## At a glance

| Part | Result |
|---|---|
| [Wake word](docs/wake-word.md) | Two stages, on the device: 97% of held-out voices in quiet, 97% of the owner's (never heard), 1 false answer in an hour of TV |
| [Response time](docs/latency.md) | 1.2-1.5 s from you stopping to the first sound, down from about 3.5 s |
| [Quick model](docs/models.md) | Qwen on Groq, then Cerebras, hedged at 0.5 s; 149/156 on the capability benchmark at low reasoning |
| [Whole conversations](docs/evals.md) | 47 scenarios played through the real brain, hand-offs followed through: 143/145 runs right on the 29 measured so far (from 76/87); run on request from a pull request comment |
| [Self-learning](docs/self-learning.md) | Retrains its wake models from the household's use, installs them only if better |
| [Runs on](docs/deployment.md) | Raspberry Pi 5 with a USB speakerphone (an Anker PowerConf here), or a Mac |

## Highlights

- **Nothing leaves the house before the wake word's double-check agrees** ([wake word](docs/wake-word.md)).
- **Answers are drafted before you finish**, and thrown away if you keep talking ([response time](docs/latency.md)).
- **A quick model that hands off**: web questions to OpenAI, hard ones to a thinking model ([models](docs/models.md)).
- **Evaluated as whole conversations**: hand-offs followed through, corrections, failures mid-conversation, every turn
  checked, with costs estimated and capped ([evals](docs/evals.md)).
- **Reminders, timers and messages** that repeat until someone says "got it" ([reminders](docs/reminders.md)).
- **Works the house** through Home Assistant: locks, switches, lights, covers, the AC, the TV. A change is "done"
  only when the device reports it, nothing is done by a draft, and the door unlocks only for a trusted voice that
  asked. An agent plays things on an Android TV, checking the screen step by step; anything else is a plugin
  ([the house](docs/home.md)).
- **Knows who's talking**, locally and in parallel, so it adds no latency ([self-learning](docs/self-learning.md)).
- **Every paid service is optional** ([what runs instead](docs/architecture.md#running-on-the-keys-there-are)).
- **A household web UI**: conversations, what TARS sent, wakes to review, reminders, voices, and a Status page:
  every part running, the services, the backup, the Pi ([web UI](docs/web-ui.md)).
- **On Telegram too**: write to it or send it a voice message away from home, and get its reminders and what it
  sends on your phone, with nothing at home opened to the internet ([Telegram](docs/telegram.md)).
- **Backed up to S3** every 15 minutes, with a snapshot of the database every six hours; old audio leaves the Pi for
  the bucket ([backup](docs/backup.md)).

How a request is answered:

```mermaid
flowchart LR
    mic[Mic] --> wake[Wake word] --> stt[Speech to text] --> brain[Quick brain] --> voice[Voice] --> spk[Speaker]
    brain -.-> handoff[Hand-offs]
```

The wake word runs on the device. Speech to text is Deepgram's Flux, the voice is Cartesia's Sonic, the quick brain is
Qwen on Groq or Cerebras, and hand-offs go to OpenAI for web search and hard questions.

How it learns:

```mermaid
flowchart LR
    wake[Wake word] --> kept[Wakes kept] --> labels[Labelled] --> train[New pair trained] --> test[Tested] -->|better| wake
```

![The web UI's Home page: what TARS sent, then the conversations](docs/screenshots/home-desktop.png)

## Quick start

Needs [uv](https://docs.astral.sh/uv/). On Linux (a Pi included), first `sudo apt install libportaudio2 libatomic1`.

```bash
git clone https://github.com/alon-pilosoph/tars.git && cd tars
uv sync --all-extras                   # every extra: echo cancellation, Claude, Android TV, the IR blaster, S3 backup
cp .env.example .env                   # then your API keys: OpenAI, Deepgram, Cartesia, Groq, Cerebras, or some
uv run voice-assistant --check         # keys, devices, models, services, voice and mic: what's wrong, what to do
uv run voice-assistant --mic-test      # loudness, speech and wake-word meters; no API key needed
uv run voice-assistant                 # say "hey TARS", then ask something
uv run voice-assistant --web           # in a second terminal: the web UI at http://127.0.0.1:8080
```

Also `--ptt` (press Enter instead of the wake word), `--text` (type, hear answers) and `--list-devices`. On a Pi,
`deploy/install-pi.sh` does it all ([at home](docs/deployment.md)). The web UI has no accounts: keep it on your network.

## Configure

Everything is in [`config.toml`](config.toml): audio devices by name, wake sensitivity, turn-taking, each stage's model.
What's this machine's own (the speakerphone's name, who's on Telegram) goes in `config.local.toml` next to it: never
committed, its settings replace `config.toml`'s, so `git pull` never meets a local edit.
Every key in `.env` is optional, as long as something can hear, think and speak; TARS says at startup what it does
without the missing ones ([running on the keys there are](docs/architecture.md#running-on-the-keys-there-are)).

The persona is TARS from *Interstellar*: for its speaker-in-a-metal-box sound, set `[tts] effect = "tars"`; for a plain
assistant, edit the system prompt.

## Docs

| Doc | What's in it |
|---|---|
| [Hearing "hey TARS"](docs/wake-word.md) | the two-stage wake word, its training and evaluation |
| [Training the models](training/README.md) | the training pipeline, from public data to the household's pair |
| [Response time](docs/latency.md) | where the time goes, and the steps from 3.5 s |
| [Choosing the quick model](docs/models.md) | the capability benchmark, every model tried, routers |
| [Evaluating whole conversations](docs/evals.md) | scenario evals: hand-offs followed through, corrections, failures, and what they found |
| [How TARS fits together](docs/architecture.md) | the pipeline, failures, missing keys, privacy, the code map |
| [Self-learning TARS](docs/self-learning.md) | what's kept, automatic labels, voices, retraining |
| [Reminders, timers and messages](docs/reminders.md) | setting, saying and acknowledging them |
| [The web UI](docs/web-ui.md) | pages, the Refresh rule, the API, checks |
| [TARS on Telegram](docs/telegram.md) | setting it up, what it does, where reminders go, what Telegram gets |
| [The house](docs/home.md) | Home Assistant's devices, an Android TV and its agent, unlocking, plugins, setting it up |
| [Running TARS at home](docs/deployment.md) | the Pi, services, enrolling voices, backup, a test run |
| [Backing up to S3](docs/backup.md) | what goes to the bucket, making room on the Pi, archiving, bringing it back |
| [What's next](docs/roadmap.md) | real use, interrupting, the hard-question hand-off, memory |

## Development

```bash
uv run pytest                                # offline tests: no devices or API keys
uv run ruff check src tests tools training evals examples && uv run ruff format src tests tools training evals examples
uv run python -m evals --estimate            # what the scenario evals would cost; without it they run (paid APIs)
cd webui && npm install
npm test && npm run lint && npm run format   # the web UI's unit tests, lint and formatting
npm run build                                # rebuilds the committed page in src/voice_assistant/webui_static/
npm run e2e && npm run visual                # against a real backend, and the screenshot baselines (macOS)
npm run site                                 # the How TARS works page, from site/ into site/dist/
```

CI runs the tests with every extra installed, lint and format checks, a check that the committed web build is
current, and an end-to-end run; the How TARS works site deploys only after CI passes on `main`.

## License

The code is MIT (`LICENSE`). The "hey TARS" models in `models/generic/` are CC BY-NC-SA 4.0 (`models/LICENSE.md`),
since some of their training data is non-commercial only; every source and its terms are in `ATTRIBUTION.md`.
