# voice-assistant

A hackable voice assistant: say a wake word, ask a question, hear the answer. Prototyped on a Mac, deployed to a Raspberry Pi 5 with a USB speakerphone.

```
mic ──► wake word ──► record until you stop ──► speech-to-text ──► LLM ──► text-to-speech ──► speaker
        (local)       (local: Silero VAD +       (Deepgram,          (OpenAI)  (OpenAI, streamed)
                       an end-of-turn model)      streamed)
```

- **One continuous mic stream.** Every stage reads 80 ms blocks from the same queue, so nothing fights over the audio device.
- **Wake word and speech detection run locally** ([microWakeWord](https://github.com/kahrendt/microWakeWord), a [Vosk](https://alphacephei.com/vosk/) double-check, [Silero VAD](https://github.com/snakers4/silero-vad)). Nothing leaves the machine until you've said "hey TARS".
- **Recording ends when you stop talking**, not after a fixed number of seconds. After 0.8 s of silence a small local model ([Smart Turn](https://github.com/pipecat-ai/smart-turn)) listens to how you sounded, and if you were mid-thought, TARS keeps waiting.
- **Your words are transcribed while you say them** (Deepgram, streamed), so the text is ready about 0.2 s after you stop. While TARS waits to be sure you're done, it already prepares the answer, and only plays it once you are: about 2.5 s from when you stop talking to its first word ([docs/latency.md](docs/latency.md)).
- **It can look things up and send you things.** With web search it answers current questions and finds real links; with the send tool it puts links, notes, lists and text files in the web UI instead of reading them out.
- **The reply is spoken while it's still being written.** The LLM response is streamed, each sentence goes to text-to-speech the moment it's complete, and later sentences synthesize while earlier ones play. Playback starts on the first audio chunk.
- **Every stage sits behind a small interface** (`Trigger`, `Transcriber`, `Brain`, `Voice`), so swapping in a local model is one new class and a config change.
- **Latency is printed for every turn**, broken down by stage.
- **Conversations, not commands.** After answering, it listens a few more seconds for a follow-up without the wake word. If what it hears isn't meant for it (people talking to each other), the LLM answers `<skip>` and it stays quiet and forgets it. The whole conversation is sent to the LLM until it's been quiet for 10 minutes.
- **It learns from your household** (optional). Every wake and near-miss is kept with its audio and labeled, mostly automatically, and the web UI shows what's waiting to be learned from. New wake models are trained on a bigger machine, tested end to end, and installed on the Pi without a restart; "Use this" switches back ([docs/self-learning.md](docs/self-learning.md)).
- **It knows who's talking** (optional). Each request gets a voiceprint, compared to enrolled people while speech-to-text runs, so it adds no latency.
- **Failures are spoken, not fatal.** A dropped connection or a crashed request gets a line in TARS's voice ("I lost that one somewhere between here and the server. Ask me again."), made ahead of time so it plays even when the voice service is the problem, and the assistant keeps listening. The voice gives up after 4 s of silence, other OpenAI requests after 15 s. A microphone that stops delivering audio exits with an error so a supervisor can restart it.

## Docs

- [How TARS fits together](docs/architecture.md): the pipeline, the LLM's tools, what leaves the machine, the web UI's safeguards
- [Hearing "hey TARS"](docs/wake-word.md): the two stages, training, performance, what was tried and dropped
- [Response time](docs/latency.md): where the ~2.5 s goes, starting early and speaking late, what was tried
- [Self-learning TARS](docs/self-learning.md): what's kept, automatic labels, voices, training new wake models
- [The web UI](docs/web-ui.md): pages, the Refresh rule, the API, development and checks
- [Running TARS at home](docs/deployment.md): the Pi, services, storage, backup, a first test run
- [What's next](docs/roadmap.md): a week of real use, interrupting TARS, a larger model for hard questions, memory

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # then add your OpenAI and Deepgram API keys
```

Keys are read from `.env` only; keys in your shell are ignored. Deepgram (streaming speech to text) is only needed
with `[stt] provider = "deepgram"`, the default in `config.toml`; with `"openai"`, the OpenAI key is enough.

On Linux (a Raspberry Pi included), install PortAudio and libatomic first: `sudo apt install libportaudio2 libatomic1`.

## Run

```bash
uv run voice-assistant --mic-test      # live loudness / speech / wake-word meter, no API key needed
uv run voice-assistant                 # say "hey TARS", then ask something
uv run voice-assistant --ptt           # press Enter instead of saying the wake word
uv run voice-assistant --text          # type questions, hear spoken answers
uv run voice-assistant --list-devices  # find your mic / speaker names for config.toml
uv run pytest                          # offline tests, no devices or API key needed
uv run ruff check src tests tools training
```

The same checks, plus the web UI's unit tests, its build and its end-to-end check, run on a clean Linux machine for
every push (`.github/workflows/ci.yml`), so the repo keeps working somewhere other than where it was written.

Sounds: a chime means "listening", a soft blip after an answer means "still listening for a follow-up", and two falling notes mean something went wrong.

### The web UI

```bash
uv run voice-assistant --web                 # http://127.0.0.1:8080, this machine only
uv run voice-assistant --web --host 0.0.0.0  # from a phone on the home network
```

While the assistant runs, every conversation is saved to `voice_data/events/`: what each person said (with their audio), what TARS answered, and what woke it (a few seconds of audio, what the double-check heard). TARS can also send things there: ask it to "send me a recipe" or "make a packing list" and it answers "It's on the TARS page". It finds real links with web search (`[llm] web_search` and `send` in `config.toml`). The web UI is where you read the conversations and what was sent, check the unclear wakes (was it really "hey TARS"?), and name the voices it has grouped (TARS then greets them by name). It runs as its own process next to the assistant and has no accounts, so keep it on the home network.

![The web UI's Home page: what TARS sent, then the conversations](docs/screenshots/home-desktop-light.png)

The page is a React app in [`webui/`](webui/). Its build is committed in `src/voice_assistant/webui_static/`, so the Pi needs no Node. After changing it:

```bash
cd webui && npm install && npm run build     # rebuilds src/voice_assistant/webui_static/
npm run dev                                  # hot reload on :5173, API proxied to :8080
npm test && npm run e2e && npm run visual    # unit tests, functional checks, screenshot tests
```

Add `?demo` to the URL for built-in sample data with no server. [docs/web-ui.md](docs/web-ui.md) covers the pages, the API and the checks.

### On the Pi, at boot

`deploy/install-pi.sh` sets everything up on a Pi in one go (see [docs/deployment.md](docs/deployment.md)). [`deploy/voice-assistant.service`](deploy/voice-assistant.service) is a systemd user service that starts the assistant at boot and restarts it if it exits (for example after the speakerphone is unplugged and plugged back in). [`deploy/voice-assistant-web.service`](deploy/voice-assistant-web.service) does the same for the web UI. Install steps are at the top of each file.

On macOS, the first run will ask for microphone access for your terminal. If audio stays silent, check System Settings → Privacy & Security → Microphone.

## Configure

Everything lives in [`config.toml`](config.toml): audio devices (matched by name, so the same file works on the Mac and the Pi), wake word and sensitivity, silence timing, and the model and voice for each stage. `[llm]` also turns web search and sending on or off, `[learning]` sets what's kept for the web UI and for how long, and `[web]` lists any extra names the web UI may be reached by.

The default setup plays TARS from *Interstellar*: a deadpan persona in the system prompt, delivery `instructions` for the TTS voice, and `effect = "tars"`, which filters the audio so it sounds like a speaker in a metal box. To get a plain assistant back, set `effect = ""` and clear `instructions`.

### The wake word

"hey TARS" is two stages: a small always-on [microWakeWord](https://github.com/kahrendt/microWakeWord) model, then a double-check with an offline recognizer that tells "hey TARS" from "hey cars". The committed models in `models/generic/` were never trained on anyone's recordings, so they work out of the box. [docs/wake-word.md](docs/wake-word.md) explains how they work, how well they do, and what was tried; [`training/`](training/README.md) has the scripts that made them. `[wake] model` also takes an openWakeWord name (`hey_jarvis`) or a path to your own `.onnx` or `.tflite`.

### Recognizing people

```bash
uv run voice-assistant --record-voice NAME   # ~10 min guided session: wake phrases + read-aloud sentences
uv run voice-assistant --enroll NAME         # build NAME's voiceprint from those sentences
```

Then set `enabled = true` under `[speaker]`. There's also a way without a recording session: in the web UI's Voices page, name a voice TARS has grouped; once it has 5 or more requests, re-clustering builds that person's voiceprint from them (and replaces one made with `--enroll` under the same name). Requests reach the LLM tagged `[Speaker: NAME]` (or `unknown` below `threshold`). Recordings and voiceprints stay in `voice_data/`, which is gitignored. The speaker model, [WeSpeaker ResNet34-LM](https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM) (CC-BY-4.0, the one pyannote.audio uses), downloads to `models/` on first use.

## Layout

| File | Role |
| --- | --- |
| `audio.py` | Mic stream, device lookup by name, streamed playback, chimes and error tone |
| `wake.py` | Wake-word and push-to-talk triggers |
| `recorder.py` | Records one utterance: from when you start talking until your turn is over |
| `vad.py` | Is someone talking? Silero VAD, a small local speech detector |
| `turn.py` | Finished, or only paused? Smart Turn, a small local model that extends the wait when you sound mid-thought |
| `draft.py` | Start early, speak late: the answer prepared during a pause, used or thrown away |
| `models.py` | Downloads the small local models on first use |
| `stt.py` / `llm.py` / `tts.py` | Speech-to-text (Deepgram streamed, OpenAI as backup), conversation (OpenAI's Responses API, with web search and the send tool), text-to-speech |
| `speech.py` | Splits the streamed reply into sentences and pipelines them through text-to-speech |
| `effects.py` | Optional streaming audio effects on the synthesized voice (the TARS speaker box) |
| `assistant.py` | The main loop: wake, listen, answer, follow-ups, error handling, per-stage timing |
| `verify.py` | The double-check on every wake: answer, ask "Did you call me?", or ignore |
| `speaker.py` | Speaker identification from voiceprints |
| `store.py` | The database and audio folder the logs share (SQLite in WAL mode, schema and upgrades) |
| `events.py` | The event log: every wake and near-miss, with audio, labels and voice clusters |
| `conversations.py` | Every conversation, turn by turn, and what TARS sent (links, notes, lists, files) |
| `journal.py` | What the assistant writes to those logs as it works; a failed write never costs a reply |
| `clustering.py` | Groups requests by voice; builds voiceprints for named people |
| `webui.py` | The web UI's server and API (the page itself is in `webui/`) |
| `versions.py` | The wake model and check pairs: the installed one, trained ones, which is in use, switching live |
| `enroll.py` | Guided recording of a person's voice for speaker ID and the wake-word verifier |
