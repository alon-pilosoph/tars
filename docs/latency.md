# Response time

How long TARS takes to answer, from the moment you stop talking to its first sound: where that time goes, how the
pipeline is built to overlap it, and the measured steps that took it from about 3.5 s to 1.1-1.6 s.

The constraints shape every choice below. A voice assistant feels slow well before it feels wrong, so every stage is
streamed and started as early as possible. But cutting someone off mid-sentence is worse than a slow answer, so
nothing that speeds up the answer may change what TARS heard. And when speed and the sound of the voice conflict,
TARS picks speed.

## Where the time goes

Median over 8 spoken questions, three runs, on the development Mac ([method](#how-its-measured)):

| Stage | Time | What happens |
|---|---|---|
| Waiting to be sure you're done | ~0.6 s (0.2-1.7 s) | Deepgram's Flux decides, from your words as well as the pause |
| Speech to text | ~0 s | Flux sends the words with its decision |
| The brain's first sentence | ~0.4 s | Qwen on Cerebras at low reasoning, streamed (OpenAI's Luna: ~0.7 s; it still answers anything that needs the web) |
| The voice's first audio | ~0.4 s | Deepgram's Aura-2 Zeus, a sentence at a time (OpenAI's Onyx: ~1.1 s) |
| **From you stopping to TARS's first sound** | **~1.1-1.6 s** | the answer is prepared during the wait, so these overlap |

A turn that needs the web adds Qwen's hand-off (about 0.4 s) and a web search, and TARS says "Looking it up." as it
starts.

## How the stages overlap

```mermaid
sequenceDiagram
    participant You
    participant Rec as Recorder (local)
    participant DG as Deepgram Flux
    participant Draft as Answer draft
    participant Out as Speaker
    You->>Rec: "What's the capital of Australia?"
    Rec-->>DG: audio, while you talk
    DG->>Rec: maybe done
    Rec->>Draft: start early (the words so far, speaker ID)
    Draft->>Draft: Qwen writes, Zeus speaks the first sentence
    DG->>Rec: done
    Rec->>Out: confirmed: play the draft
```

### Start early, speak late

When Flux thinks you may be done, TARS prepares the answer in the background while the recording goes on
(`draft.py`). If you carry on talking, the draft is thrown away and the brain forgets it, without waiting for it, so
a draft stuck in a web search can't hold up the next one. A draft is only played, logged, and allowed to send
anything to the web page once your turn is confirmed over. The waiting rule therefore decides only *when TARS
speaks*, never what it heard: answering early can't cause a cut-off.

### The end of your turn

[Flux](https://deepgram.com/learn/introducing-flux-conversational-speech-recognition) (`stt.FluxSession`,
`recorder.py`) hears your words as well as the pause, so "and the..." isn't taken for the end of a sentence. Silero
VAD (local) still hears when you start talking. If Flux fails, local rules take over for that turn: after 0.8 s of
silence, [Smart Turn](https://github.com/pipecat-ai/smart-turn) (a small local model that listens to how your last
words sounded) may extend the wait to 1.6 s, and a draft starts a quarter second into a pause. `[stt] provider =
"deepgram"` makes the local rules the only ones.

### The voice

A warm connection to Deepgram starts speaking in about 0.27 s, and opening one takes about 0.7 s, so
`tts.DeepgramSpeech` keeps two open from the wake word. The speaker (`audio.Speaker`) skips the silence a voice
starts a reply on (0.2-0.5 s for Zeus) and hands the mic back at the last sound, so a short "Yes?" frees the mic
0.9 s after it starts, of which 0.3 s is a margin for the room's echo.

### The greeting and the open mic

With `[recorder] greet = "always"`, TARS says "Yes, <name>?" the moment the wake word passes its double-check, so
you always know it's listening. The double-check (21 ms on the development Mac) and speaker ID for the name (43 ms)
are all it waits for, and the line is made ahead. Each turn records how long after the wake word the greeting
started (`greet` in its timings).

With `[audio] echo_cancel`, the greeting is said with the mic open. WebRTC's echo canceller (`echo.py`, AEC3, through
LiveKit's SDK) is given everything the speaker plays and takes it back out of what the mic hears, so you can carry
straight on from "hey TARS" without your first words being lost. If a trace of the greeting gets through, its words
are taken off the start of what was heard (`echo.without_echo`); if that's all there was, TARS keeps listening. On
recorded speech through a simulated room, it removed about 30 dB once it had learned the room (a second or two), and
speech recognition heard nothing of TARS's own voice.

Both are off by default until measured in a real room (see [limitations](#limitations)): `greet = "pause"` says
"Yes, <name>?" only once nothing has followed the wake word for `greet_after_s`. If the canceller can't start or
fails, TARS carries on with the mic closed while it speaks; `--check` says so, and warns when the devices' delay is
more than the 0.5 s the canceller can take.

### When a service fails

The voice gives up after 4 s without audio instead of the client's 15 s, and TARS says so in its own voice, from
lines synthesized at startup ("I lost that one somewhere between here and the server. Ask me again."; plainer below
50% humor). If Deepgram fails, OpenAI transcribes the same recording. If Cerebras fails, Luna answers, and keeps
answering on its own for the next two minutes. A failed answer is kept in its conversation with where it failed
(speech to text, the answer, or the voice) and why, and Home shows it.

### Timings on every answer

Each of TARS's turns keeps the same stages as the log line (`timings` in the `turns` table, in seconds) and which
model wrote it (`answered_by`: `quick` for Qwen, `look_up` when Qwen handed the turn over, `ponder` when it went to
the thinking model, `fallback` when Cerebras failed or is resting, `openai` with no Cerebras set up). Home shows the
time to first sound and the model under each answer; hovering shows each stage. For a week's numbers:
`SELECT answered_by, avg(json_extract(timings, '$.total')) FROM turns WHERE role = 'tars' GROUP BY 1`.

## How it's measured

```bash
uv run python tools/latency_bench.py
uv run python tools/latency_bench.py --set recorder.end_silence_s=0.6 --set stt.provider=deepgram
uv run python tools/latency_bench.py --set tts.provider=openai --set tts.model=gpt-4o-mini-tts
uv run python tools/turn_bench.py      # cut-offs: the local end of turn against Flux, on your own sentences
```

`tools/latency_bench.py` speaks 8 questions once (Deepgram's Aura voice, cached in `voice_data/bench/`), plays them
to the assistant at real-time pace over real room tone taken from your recordings, and measures from where the voice
ends in the audio to when the first reply sound would play (its speaker skips a voice's leading silence too). It uses
the real services, so a run costs a few cents, and the numbers move by a few tenths between runs with the network and
the model, so compare medians over several runs. `--set` overrides any `config.toml` setting for
one run. Every turn also prints its own stage timings in the assistant's log.

`tools/turn_bench.py` measures cut-offs: it plays the owner's 25 recorded sentences whole, and again with a pause
spliced in after an unfinished word ("and", "the", "to"...), 0.5, 0.8 or 1.2 s long.

## How it got here

From the first setup to the current one, each measured with `latency_bench.py`; medians from you stopping to TARS's
first sound.

| Step | Time | Why it helped |
|---|---|---|
| OpenAI throughout: the recording uploaded and transcribed after a 0.5 s silence | ~3.5 s | the starting point: nothing is transcribed until the recording is finished |
| Deepgram Nova-3 streamed, a local end of turn (Silero VAD and Smart Turn), Onyx's voice, Luna | 2.6-3.0 s | the words are transcribed while you talk, so the text is ready a moment after you stop |
| Flux decides the end of the turn | 2.0-2.6 s | Flux sends the words with its decision, so speech to text costs nothing after the wait |
| Deepgram's Aura-2 Zeus voice instead of Onyx | 2.1-2.5 s | first audio in about 0.4 s instead of 1.1 s; from this step on the total is counted to the first *audible* sound, which includes the silence Zeus starts on, so the total barely moves |
| Qwen on Cerebras answers first, Luna takes what needs the web | 1.8-2.0 s | the first sentence comes in about 0.3 s (Qwen without reasoning) instead of Luna's 0.7 s |
| The speaker skips the silence a voice starts on | 1.1-1.6 s | Zeus opens a reply with 0.2-0.5 s of silence, which no longer plays |

## Design decisions

Each decision answers one question, and the main choices were measured against alternatives.

### Who decides that you've finished?

**Constraint:** a cut-off is the worst error a voice assistant makes, worse than a slow answer.

| Option | Evidence | Outcome |
|---|---|---|
| Silence alone, with webrtcvad and rules for its false alarms (tonal hums, lone noise blocks) | brittle: every rule fixed one case and broke another | replaced by Silero VAD |
| Silero VAD and Smart Turn, Smart Turn allowed to end the turn after 0.24 s when it says "finished" | 1.6 s total, but it cut sentences in half | Smart Turn may only extend the wait |
| Silero VAD and Smart Turn, Smart Turn only extending the wait | turn over 0.88 s after a whole sentence; cut off **18 of 18** sentences in a 1.2 s pause | kept as the fallback |
| Flux | turn over 1.05 s after a whole sentence (90% within 1.29 s); cut off 1 of 18 in a 1.2 s pause | **chosen** |

The cut-off test in full (`turn_bench.py`):

| | Silero VAD and Smart Turn | Flux |
|---|---|---|
| Turn over after a whole sentence | 0.88 s | 1.05 s median, 90% within 1.29 s |
| Cut-offs in a 0.5 / 0.8 / 1.2 s pause | 0 / 0 / **18 of 18** | 0 / 1 / 1 of 18 |

**Decision: Flux, with the local rules as the fallback.** Smart Turn, checked after 0.8 s of silence, scored 17 of
the 18 unfinished sentences as done, so it almost never extended the wait.
**Trade-off:** Flux is about 0.2 s slower to call a whole sentence finished, and the turn's audio goes to Deepgram
(it already did, for speech to text).

### Which model writes the first sentence?

**Constraint:** the quick model runs on every turn, so its first sentence is on the critical path, and its rate
limit has to cover a household's use.

First sentence with TARS's prompt, without reasoning or tools of its own, over 24 questions:

| Model | First sentence | Free-key limit |
|---|---|---|
| Qwen on Cerebras | 0.27 s (slowest 0.41 s) | 450 requests a minute |
| Qwen on Groq | 0.16 s | 8,000 tokens a minute (about 10 of TARS's requests) |
| gpt-oss-20b on Groq | 0.35 s | 8,000 tokens a minute |
| gpt-oss-120b on Groq | 0.38 s | 8,000 tokens a minute |
| gpt-oss-120b on Cerebras | 0.27 s | 5 requests a minute |

**Decision: Qwen on Cerebras, with OpenAI's Luna (~0.7 s) behind it.** Qwen sounded the most like TARS; gpt-oss was
correct but plain. Groq's Qwen was faster, but its free allowance covers about ten requests a minute.
**Trade-off:** a second model in the loop, which must know what to hand over. In this test, with no tools of its own,
Qwen replied `<look-up>` for the web or the TARS page and the turn went to Luna. Asked 12 questions that need that
and 12 that don't, six times each, it handed off 70 of the 72 it should have and answered all 72 others itself. As
TARS is set up, Qwen has its own tools for reminders and sending (`quick_tools`) and runs at low reasoning, with first
words in about 0.4 s; [choosing the quick model](models.md) benchmarks those across models and reasoning efforts.

### Which voice?

**Constraint:** the voice's first audio is the last stage before TARS is heard.

| Option | First audio | Outcome |
|---|---|---|
| OpenAI's Onyx | 0.97-1.19 s | sounds best and takes delivery instructions; too slow |
| Onyx at 1.2x speed (`speed`, pitch kept) | about 20% more words a second | sounded bad |
| Deepgram's Flux Cliff | 0.39-0.44 s | now and then opens a sentence with about a second of silence, which undoes the gain |
| ElevenLabs Flash, Cartesia Sonic | 0.2-0.45 s | no faster than Deepgram's, which also does speech to text, so not chosen |
| Deepgram's Aura-2 Zeus | 0.34-0.48 s | **chosen** |

**Decision: Aura-2 Zeus**, with Deepgram already used for speech to text. **Trade-off:** Onyx sounds better and takes
delivery instructions; TARS picks latency over voice. Onyx stays a setting (`[tts] provider = "openai"`).

### Can a reply start sooner?

| Option | Evidence | Outcome |
|---|---|---|
| Skip the silence a voice starts a reply on | 1.8-2.0 s → 1.1-1.6 s | kept |
| Speak the reply's first clause before its sentence ends | no measurable gain: most replies are one sentence | dropped |
| OpenAI's Realtime API (speech in, speech out) | reported around 1 s | not tried: it ties the brain to OpenAI's realtime models |

### What happens when the voice stalls?

A backup voice worked, but the voice changed mid-reply. **Decision:** a 4 s timeout and a spoken error line in
TARS's own voice ([when a service fails](#when-a-service-fails)). **Trade-off:** that turn is lost, and TARS says so.

## Limitations

- **Measured on the development Mac**; the numbers move by a few tenths between runs.
- **Spliced pauses aren't real hesitations** (no "um", no stretched last word), which may flatter Flux in the
  cut-off test.
- **Starting early costs brain calls.** Flux's early "maybe done", where a draft starts, came 0.65 s after the end of
  a sentence and was taken back in 20 of the 54 paused ones: about one extra brain call in three.
- **Web turns are slower**: Qwen's hand-off, then a web search. [Choosing the quick model](models.md#web-questions)
  follows one through.
- **The open-mic greeting is unmeasured in a real room.** Its numbers come from a simulated room, and when you talk
  over TARS, some of the overlapping words can be lost, as with any echo canceller. Measure it with
  `uv run python tools/echo_bench.py` (`--talk` to talk over it) before turning `echo_cancel` and `greet = "always"`
  on.

## Settings

| Setting | In `config.toml` | What it does |
|---|---|---|
| `[stt] provider` | `"flux"` | `flux`: Deepgram's Flux transcribes while you talk and decides when you're done. `deepgram`: Nova-3 transcribes while you talk, and the local rules below decide. `openai`: the finished recording is sent after the local rules decide (slowest, one key fewer) |
| `[llm] cerebras_model` | `"qwen-3.8-27b"` | the quick brain; empty: OpenAI's `model` answers everything (~0.4 s slower) |
| `[tts] provider`, `model` | `"deepgram"`, `"aura-2-zeus-en"` | the voice; `openai` with `gpt-4o-mini-tts` takes delivery instructions but starts ~0.6 s later |
| `[recorder] end_silence_s` | `0.8` | without Flux: silence that ends your turn |
| `[recorder] max_pause_s` | `1.6` | without Flux: how long a pause may be when you sounded mid-thought |
| `[recorder] end_of_turn` | `"smart"` | without Flux: `silence` turns Smart Turn off |
| `[recorder] greet` | `"pause"` | when to say "Yes, <name>?" after the wake word: `always` at once, `pause` only after `greet_after_s` of nothing, `never` |
| `[audio] echo_cancel` | `false` | take TARS's own sound out of what the mic hears, so the mic stays open while it greets you; off until checked in the room with `tools/echo_bench.py` |
| `[recorder] answer_early_s` | `0.25` | without Flux: when the answer starts being prepared; `0` waits for the end |
| `[recorder] vad_threshold` | `0.5` | how sure the speech detector must be |
| `[llm] humor` | `75` | the persona's humor setting, and which error lines TARS uses |

## Next steps

Measure the echo canceller in the room, then keep the mic open while TARS answers too, so "hey TARS" can interrupt
a reply ([what's next](roadmap.md#2-tars-stop-interrupting-a-reply)).
