# TARS

[![CI](https://github.com/alon-pilosoph/tars/actions/workflows/ci.yml/badge.svg)](https://github.com/alon-pilosoph/tars/actions/workflows/ci.yml)

A household voice assistant you can take apart. Say "hey TARS" and ask something: a wake word trained for it hears
you on the device, and TARS starts answering 1.1 to 1.6 seconds after you stop talking. It sets reminders and passes
messages between people, knows who's speaking, and retrains its own wake word from the household's use. It runs on a
Raspberry Pi 5 with a USB speakerphone (an Anker PowerConf here), or on a Mac while you work on it. The main choices
were measured against alternatives, and the docs show the numbers.

**[How TARS works](https://tars.alonp.dev)** follows one request second by second, from the wake word on the Pi to the
answer, and shows how it learns from the household.

## Highlights

- **A custom wake word, trained for TARS, that keeps everything local until it's sure.** A 69 KB
  [microWakeWord](https://github.com/kahrendt/microWakeWord) model scores every 80 ms of audio, and a
  [Vosk](https://alphacephei.com/vosk/) double-check with a learned layer tells "hey TARS" from "hey cars". Nothing
  leaves the machine before both agree. The pair was trained on synthetic and voice-converted clips across thousands of
  voices (about 2,000 Piper voices alone), and evaluated end to end on held-out voices in 8 conditions, plus an hour
  each of TV and audiobooks for false answers: 97% recall on held-out voices in quiet, 93% on the owner's voice it never
  heard, and one false answer in the TV hour (none in the audiobooks). A self-learning retrain installs a new pair only
  if it beats the current one ([wake word](docs/wake-word.md), [training pipeline](training/README.md)).
- **1.1-1.6 s from you stopping to TARS's first sound, down from about 3.5 s.** Deepgram's Flux decides you've finished
  from your words as well as the pause; the answer is drafted while it makes sure, and thrown away if you keep talking;
  Qwen on Cerebras writes the first sentence in about 0.4 s; Deepgram's voice speaks a sentence at a time. The measured
  steps that got it there, and why each helped, are in [response time](docs/latency.md).
- **A quick brain that knows when to hand off.** Qwen on Cerebras answers first, with its own tools for reminders and
  sending. Web questions go to an OpenAI model with web search, and hard ones to a thinking model. It was chosen on a
  26-request capability benchmark against gpt-oss-120b, Claude Haiku 5.5 and OpenAI's Luna: 149 of 156 right at low
  reasoning, first words in about 0.4 s, and of the two fast candidates, the one that sounds like TARS ([choosing the
  quick model](docs/models.md)). After answering it listens a few seconds for a follow-up without the wake word, and
  stays quiet when what it heard was meant for someone else.
- **It learns from the household.** Every wake is kept with its audio and labeled, mostly automatically from what
  happened next. A bigger machine retrains the wake pair and tests it end to end against the pair in use; it's installed
  only if it's better on the household's own wakes and no worse on anything else. Installs are atomic, and the web UI
  switches back to any earlier pair ([self-learning](docs/self-learning.md)).
- **Reminders, timers and messages that wait for "got it".** "Remind me to call the bank at nine", "tell Stacey dinner's
  ready when she's back". TARS says them again every 5 minutes, up to 4 times, until someone acknowledges them, and
  never over a conversation; a timer rings until someone turns it off. The model judges the "got it", and TARS, not the
  model, works out the time ([reminders](docs/reminders.md)).
- **It knows who's talking.** Speaker ID runs locally while you talk, so it adds no latency. Voices are grouped
  automatically, and naming one with 5 or more requests and pressing Regroup voices makes a voiceprint, so TARS greets
  people by name and keeps what it sends for the right person.
- **A web UI for the household.** Conversations by day, what TARS sent (links, notes, lists, files), wakes to review,
  reminders, voices, and the wake models in use. React over a small FastAPI API, and nothing on the page moves until you
  press Refresh ([web UI](docs/web-ui.md)).
- **Checked on every push.** CI runs the offline pytest suite (no devices or API keys), ruff lint and format checks,
  oxlint, Prettier, the web UI's unit tests, a check that the committed web build matches its source, and a Playwright
  end-to-end run against a real backend. Screenshot tests hold the page's look locally, and `tools/` holds the
  benchmarks the docs' numbers come from.

![The web UI's Home page: what TARS sent, then the conversations](docs/screenshots/home-desktop-light.png)

## Quick start

Needs [uv](https://docs.astral.sh/uv/). On Linux (a Pi included), first `sudo apt install libportaudio2 libatomic1`.

```bash
git clone https://github.com/alon-pilosoph/tars.git && cd tars
uv sync --all-extras                   # echo cancellation and Claude are extras
cp .env.example .env                   # then add your API keys: OpenAI, Deepgram, Groq, Cerebras, or some of them
uv run voice-assistant --check         # keys, devices, models, services, the voice and the mic: what's wrong, and what to do
uv run voice-assistant --mic-test      # loudness, speech and wake-word meters; no API key needed
uv run voice-assistant                 # say "hey TARS", then ask something
uv run voice-assistant --web           # in a second terminal: the web UI at http://127.0.0.1:8080
```

The small local models (wake check, speech detector, end of turn, speaker ID, about 75 MB) download on first use. On
macOS the first run asks for microphone access for your terminal. Other ways in: `--ptt` (press Enter instead of the
wake word), `--text` (type, hear spoken answers), `--list-devices` (mic and speaker names for `config.toml`).

On a Pi, `deploy/install-pi.sh` does all of it and starts TARS and the web UI as services at boot
([running TARS at home](docs/deployment.md)). The web UI has no accounts, so keep it on the home network; away from
home, use [Tailscale](docs/deployment.md#things-to-know).

## Configure

Everything is in [`config.toml`](config.toml): audio devices (matched by name, so one file works on the Mac and the
Pi), the wake models and their sensitivity, turn-taking, and the model and voice for each stage. Keys are read from
`.env` only, and each is optional, as long as something can hear, think and speak: without Groq's or Cerebras's, Qwen
runs on the other (or OpenAI answers everything); without Deepgram's, OpenAI hears and speaks; without OpenAI's,
Deepgram does, and Qwen answers everything itself, with no web search or thinking model. TARS says at startup what it
does without the missing ones.

The default persona is TARS from *Interstellar*: deadpan, with a humor setting, and a filter that makes the voice
sound like a speaker in a metal box. For a plain assistant, set `[tts] effect = ""` and edit the system prompt.

To recognize people without waiting for the web UI to group their voices, record and enroll them:

```bash
uv run voice-assistant --record-voice NAME   # ~10 min guided session: wake phrases and read-aloud sentences
uv run voice-assistant --enroll NAME         # NAME's voiceprint from those sentences
```

## Development

```bash
uv run pytest                                # offline tests: no devices or API keys
uv run ruff check src tests tools training && uv run ruff format src tests tools training
cd webui && npm install
npm test && npm run lint && npm run format   # the web UI's unit tests, lint and formatting
npm run build                                # rebuilds the committed page in src/voice_assistant/webui_static/
npm run e2e && npm run visual                # against a real backend, and the screenshot baselines (macOS)
npm run site                                 # the How TARS works page, from site/ into site/dist/
```

The web UI is a React app in [`webui/`](webui/), and its build is committed in `src/voice_assistant/webui_static/`, so
the Pi needs no Node. Add `?demo` to the URL for sample data without a server.

CI (`.github/workflows/ci.yml`) runs the tests, lint and format checks on a clean Linux machine for every push, checks
that the committed web build is current, and runs the end-to-end check against a real backend. Pushing to `main`
publishes the How TARS works page (`.github/workflows/pages.yml`).

## Docs

- [Hearing "hey TARS"](docs/wake-word.md): the two-stage wake word, how it was trained on thousands of synthetic and
  voice-converted voices, how it was measured end to end, and the alternatives that lost
- [Training the models](training/README.md): the training pipeline, from public datasets to a household's own pair,
  and rebuilding everything from scratch
- [Response time](docs/latency.md): where the time goes, starting early and speaking late, the steps from 3.5 s to
  1.1-1.6 s
- [Choosing the quick model](docs/models.md): the capability benchmark, every model tried, routers and relays
- [How TARS fits together](docs/architecture.md): the pipeline, failure handling, what each service receives, the web
  UI's safeguards, the code map
- [Self-learning TARS](docs/self-learning.md): what's kept, automatic labels, voices, training and installing new
  wake models
- [Reminders, timers and messages](docs/reminders.md): setting them, how they're said and acknowledged, the design
- [The web UI](docs/web-ui.md): pages, the Refresh rule, the API, development and checks
- [Running TARS at home](docs/deployment.md): the Pi, services, storage, backup, a first test run
- [What's next](docs/roadmap.md): a week of real use, interrupting TARS, measuring the hard-question hand-off, memory

## License

The code is MIT (`LICENSE`). The trained "hey TARS" models in `models/generic/` are CC BY-NC-SA 4.0
(`models/LICENSE.md`), because some of what they were trained on is only licensed for non-commercial use. Every
source and its terms are in `ATTRIBUTION.md`.
