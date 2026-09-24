# voice-assistant

A hackable voice assistant: say a wake word, ask a question, hear the answer. Prototyped on a Mac, deployed to a Raspberry Pi 5 with a USB speakerphone.

```
mic ──► wake word ──► record until you stop ──► speech-to-text ──► LLM ──► text-to-speech ──► speaker
        (local)       (local VAD)               (OpenAI)            (OpenAI)  (OpenAI, streamed)
```

- **One continuous mic stream.** Every stage reads 80 ms blocks from the same queue, so nothing fights over the audio device.
- **Wake word and speech detection run locally** ([openWakeWord](https://github.com/dscripka/openWakeWord), [WebRTC VAD](https://github.com/wiseman/py-webrtcvad)). Nothing leaves the machine until you've said the wake word and finished talking.
- **Recording ends when you stop talking**, not after a fixed number of seconds.
- **The reply is spoken while it's still being written.** The LLM response is streamed, each sentence goes to text-to-speech the moment it's complete, and later sentences synthesize while earlier ones play. Playback starts on the first audio chunk.
- **Every stage sits behind a small interface** (`Trigger`, `Transcriber`, `Brain`, `Voice`), so swapping in a local model is one new class and a config change.
- **Latency is printed for every turn**, broken down by stage.
- **Conversations, not commands.** After answering, it listens a few more seconds for a follow-up without the wake word. If what it hears isn't meant for it (people talking to each other), the LLM answers `<skip>` and it stays quiet and forgets it. The whole conversation is sent to the LLM until it's been quiet for 10 minutes.
- **It knows who's talking** (optional). Each request gets a voiceprint, compared to enrolled people while speech-to-text runs, so it adds no latency.
- **Failures are audible, not fatal.** A dropped connection or a crashed request plays a two-note error tone and the assistant keeps listening; OpenAI requests give up after 15 s. A microphone that stops delivering audio exits with an error so a supervisor can restart it.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # then add your OpenAI API key
```

The key is read from `.env` only. An `OPENAI_API_KEY` in your shell is ignored.

On a Raspberry Pi, install PortAudio first: `sudo apt install libportaudio2`.

## Run

```bash
uv run voice-assistant --mic-test      # live loudness / speech / wake-word meter, no API key needed
uv run voice-assistant                 # say "hey jarvis", then ask something
uv run voice-assistant --ptt           # press Enter instead of saying the wake word
uv run voice-assistant --text          # type questions, hear spoken answers
uv run voice-assistant --list-devices  # find your mic / speaker names for config.toml
uv run pytest                          # offline tests, no devices or API key needed
```

Sounds: a chime means "listening", a soft blip after an answer means "still listening for a follow-up", and two falling notes mean something went wrong.

### On the Pi, at boot

[`deploy/voice-assistant.service`](deploy/voice-assistant.service) is a systemd user service that starts the assistant at boot and restarts it if it exits (for example after the speakerphone is unplugged and plugged back in). Install steps are at the top of the file.

On macOS, the first run will ask for microphone access for your terminal. If audio stays silent, check System Settings → Privacy & Security → Microphone.

## Configure

Everything lives in [`config.toml`](config.toml): audio devices (matched by name, so the same file works on the Mac and the Pi), wake word and sensitivity, silence timing, and the model and voice for each stage.

The default setup plays TARS from *Interstellar*: a deadpan persona in the system prompt, delivery `instructions` for the TTS voice, and `effect = "tars"`, which filters the audio so it sounds like a speaker in a metal box. To get a plain assistant back, set `effect = ""` and clear `instructions`.

### Custom wake word

`[wake] model` takes a built-in openWakeWord name (`hey_jarvis`) or a path to your own `.onnx`, e.g. `models/hey_tars.onnx`. Custom models are trained with openWakeWord's training notebook on Colab; the phrase shown on screen comes from the file name.

### Recognizing people

```bash
uv run voice-assistant --record-voice NAME   # ~10 min guided session: wake phrases + read-aloud sentences
uv run voice-assistant --enroll NAME         # build NAME's voiceprint from those sentences
```

Then set `enabled = true` under `[speaker]`. Requests reach the LLM tagged `[Speaker: NAME]` (or `unknown` below `threshold`). Recordings and voiceprints stay in `voice_data/`, which is gitignored. The speaker model, [WeSpeaker ResNet34-LM](https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM) (CC-BY-4.0, the one pyannote.audio uses), downloads to `models/` on first use.

## Layout

| File | Role |
| --- | --- |
| `audio.py` | Mic stream, device lookup by name, streamed playback, chimes and error tone |
| `wake.py` | Wake-word and push-to-talk triggers |
| `recorder.py` | Records one utterance using voice activity detection, with optional padding |
| `stt.py` / `llm.py` / `tts.py` | Speech-to-text, conversation, text-to-speech |
| `speech.py` | Splits the streamed reply into sentences and pipelines them through text-to-speech |
| `effects.py` | Optional streaming audio effects on the synthesized voice (the TARS speaker box) |
| `assistant.py` | The main loop: wake, listen, answer, follow-ups, error handling, per-stage timing |
| `speaker.py` | Speaker identification from voiceprints |
| `enroll.py` | Guided recording of a person's voice for speaker ID and the wake-word verifier |
