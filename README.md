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
```

On macOS, the first run will ask for microphone access for your terminal. If audio stays silent, check System Settings → Privacy & Security → Microphone.

## Configure

Everything lives in [`config.toml`](config.toml): audio devices (matched by name, so the same file works on the Mac and the Pi), wake word and sensitivity, silence timing, and the model and voice for each stage.

The default setup plays TARS from *Interstellar*: a deadpan persona in the system prompt, delivery `instructions` for the TTS voice, and `effect = "tars"`, which filters the audio so it sounds like a speaker in a metal box. To get a plain assistant back, set `effect = ""` and clear `instructions`.

## Layout

| File | Role |
| --- | --- |
| `audio.py` | Mic stream, device lookup by name, streamed playback, chime |
| `wake.py` | Wake-word and push-to-talk triggers |
| `recorder.py` | Records one utterance using voice activity detection |
| `stt.py` / `llm.py` / `tts.py` | Speech-to-text, conversation, text-to-speech |
| `speech.py` | Splits the streamed reply into sentences and pipelines them through text-to-speech |
| `effects.py` | Optional streaming audio effects on the synthesized voice (the TARS speaker box) |
| `assistant.py` | The main loop and per-stage timing |
