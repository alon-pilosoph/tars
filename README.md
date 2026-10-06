# TARS

[![CI](https://github.com/alon-pilosoph/tars/actions/workflows/ci.yml/badge.svg)](https://github.com/alon-pilosoph/tars/actions/workflows/ci.yml)

A voice assistant for the home that you can take apart: say "hey TARS", ask something, and it answers in about a
second and a half.

**[How TARS works](https://tars.alonp.dev)** follows one request second by second, from the wake word on the Pi to the
answer, and shows how it learns from the household.

![The web UI's Home page: what TARS sent, then the conversations](docs/screenshots/home-desktop-light.png)

Runs on a Raspberry Pi 5 with a USB speakerphone (an Anker PowerConf here), or on a Mac while you work on it.

- **The wake word is trained for it and runs locally.** A small [microWakeWord](https://github.com/kahrendt/microWakeWord)
  model hears "hey TARS", and a [Vosk](https://alphacephei.com/vosk/) double-check with a learned layer tells it from
  "hey cars". Nothing leaves the machine before that.
- **It starts answering 1.1 to 1.6 seconds after you stop talking.** Deepgram's Flux hears when you've finished from
  your words as well as the pause, Qwen on Cerebras starts the answer while Flux makes sure, and Deepgram's voice speaks
  it a sentence at a time ([response time](docs/latency.md)).
- **It sets timers, reminders and messages.** "Remind me to call the bank at nine", "tell Stacey dinner's ready
  when she's back": TARS says them when they're due and again until someone says "got it", and a timer rings until
  someone turns it off. The web UI shows each one, who acknowledged it, and sets them from your phone
  ([reminders](docs/reminders.md)).
- **It can look things up and send you things.** Questions that need the web go to an OpenAI model with web search,
  and links, notes, lists and files it sends land in the web UI.
- **It keeps the conversation going.** After answering it listens a few seconds for a follow-up without the wake
  word, and stays quiet when what it hears was meant for someone else.
- **It knows who's talking.** Each request gets a voiceprint, so TARS can greet people by name and keep what it sends
  for the right person.
- **It learns from the household.** Every wake is kept with its audio and labeled, mostly automatically. A bigger
  machine trains a new pair of wake models on those wakes, tests it end to end, and installs it only if it's better.

## Quick start

Needs [uv](https://docs.astral.sh/uv/). On Linux (a Pi included), first `sudo apt install libportaudio2 libatomic1`.

```bash
git clone https://github.com/alon-pilosoph/tars.git && cd tars
uv sync
cp .env.example .env                   # then add your OpenAI, Deepgram and Cerebras API keys
uv run voice-assistant --check         # keys, devices, models, services, the voice and the mic: what's wrong, and what to do
uv run voice-assistant --mic-test      # loudness, speech and wake-word meters; no API key needed
uv run voice-assistant                 # say "hey TARS", then ask something
uv run voice-assistant --web           # in a second terminal: the web UI at http://127.0.0.1:8080
```

The small local models (wake check, speech detector, end of turn, speaker ID, about 75 MB) download on first use. On
macOS the first run asks for microphone access for your terminal. Other ways in: `--ptt` (press Enter instead of the
wake word), `--text` (type, hear spoken answers), `--list-devices` (mic and speaker names for `config.toml`).

On a Pi, `deploy/install-pi.sh` does all of it and starts TARS and the web UI as services at boot
([running TARS at home](docs/deployment.md)).

## The web UI

Home shows the conversations by day, with what each person said and what TARS answered and sent. Sent lists the
links, notes, lists and files. Reminders shows every timer, reminder and message, and who said they got it. Review holds the wakes nothing explained, to answer "hey TARS" or "Not it". Voices
groups requests by voice; name one and press Regroup voices to build that person's voiceprint. Models shows the wake
models in use and can switch back to an earlier pair. It has no accounts, so keep it on the home network; away from
home, use [Tailscale](docs/deployment.md#things-to-know).

The page is a React app in [`webui/`](webui/), and its build is committed in `src/voice_assistant/webui_static/`, so
the Pi needs no Node. Add `?demo` to the URL for sample data without a server.

## Docs

- [How TARS fits together](docs/architecture.md): the pipeline, what each service receives, the web UI's safeguards,
  the code map
- [Hearing "hey TARS"](docs/wake-word.md): the two stages, how they were trained and measured, what was dropped
- [Response time](docs/latency.md): where the time goes, starting early and speaking late, how it got here
- [Self-learning TARS](docs/self-learning.md): what's kept, automatic labels, voices, training new wake models
- [The web UI](docs/web-ui.md): pages, the Refresh rule, the API, development and checks
- [Running TARS at home](docs/deployment.md): the Pi, services, storage, backup, a first test run
- [Training the models](training/README.md): preparing the data, training a household's pair, rebuilding everything
- [Reminders, timers and messages](docs/reminders.md): setting them, how they're said and acknowledged, the design
- [What's next](docs/roadmap.md): a week of real use, interrupting TARS, a larger model for hard questions, memory

## Configure

Everything is in [`config.toml`](config.toml): audio devices (matched by name, so one file works on the Mac and the
Pi), the wake models and their sensitivity, turn-taking, and the model and voice for each stage. Keys are read from
`.env` only. The OpenAI key is always needed; Deepgram's only while `[stt]` or `[tts] provider` uses it, and
Cerebras's only while `[llm] cerebras_model` is set.

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

CI (`.github/workflows/ci.yml`) runs the tests, lint and format checks on a clean Linux machine for every push, checks
that the committed web build is current, and runs the end-to-end check against a real backend. Pushing to `main`
publishes the How TARS works page (`.github/workflows/pages.yml`).

## License

The code is MIT (`LICENSE`). The trained "hey TARS" models in `models/generic/` are CC BY-NC-SA 4.0
(`models/LICENSE.md`), because some of what they were trained on is only licensed for non-commercial use. Every
source and its terms are in `ATTRIBUTION.md`.
