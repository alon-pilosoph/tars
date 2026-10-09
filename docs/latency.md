# Response time

How long TARS takes to answer, from the moment you stop talking to its first sound, where that time goes, and what
was tried to shorten it. Measured with `tools/latency_bench.py` (below).

## Where the time goes

Median over 8 spoken questions, three runs, on the development Mac (2026-10-09, while it was also training):

| Stage | Time | What happens |
|---|---|---|
| Waiting to be sure you're done | ~0.5 s (0-1.6 s) | Deepgram's Flux decides, from your words as well as the pause |
| Speech to text | ~0 s | Flux sends the words with its decision |
| The brain's first sentence | ~0.4 s | Qwen on Groq, streamed (on Cerebras: ~0.6 s; OpenAI's Luna: ~0.7 s, and it still answers anything that needs the web) |
| The voice's first audio | ~0.4 s | Deepgram's Aura-2 Zeus from its EU servers, a sentence at a time (from the US: ~0.6 s; OpenAI's Onyx: ~1.1 s) |
| **From you stopping to TARS's first sound** | **~1.3-1.4 s** | the answer is prepared during the wait, so these overlap |

A turn that needs the web adds Qwen's ~0.4 s and a web search, and TARS says "Looking it up." as it starts.

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

**Start early, speak late.** When Flux thinks you may be done, TARS prepares the answer in the background while
the recording goes on (`draft.py`). If you carry on talking, the draft is thrown away and the brain forgets it
(without waiting for it: a draft stuck in a web search can't hold up the next one). It's only played, logged, and
allowed to send anything to the web page once your turn is confirmed over. So the waiting rule decides only *when
TARS speaks*, never what it heard: a cut-off can't come from answering early.

**The end of your turn** (`stt.FluxSession`, `recorder.py`):
[Flux](https://deepgram.com/learn/introducing-flux-conversational-speech-recognition) hears your words as well as
the pause, so "and the..." isn't taken for the end of a sentence. Silero VAD (local) still hears when you start
talking. If Flux fails, the local rules take over for that turn: after 0.8 s of silence,
[Smart Turn](https://github.com/pipecat-ai/smart-turn) (a small local model that listens to how your last words
sounded) may extend the wait to 1.6 s, and a draft starts a quarter second into a pause. `[stt] provider =
"deepgram"` makes the local rules the only ones.

**The voice.** A warm connection to Deepgram starts speaking in about 0.27 s, and opening one takes about 0.7 s, so
`tts.DeepgramSpeech` keeps two open from the wake word. The speaker (`audio.Speaker`) skips the silence a voice
starts a reply on (0.2-0.5 s for Zeus) and hands the mic back at the last sound, so a short "Yes?" frees the mic
0.9 s after it starts, of which 0.3 s is a margin for the room's echo.

<a id="the-greeting-and-the-open-mic"></a>**The greeting and the open mic.** With `[recorder] greet = "always"`, TARS says "Yes, <name>?" the moment the
wake word passes its double-check, so you always know it's listening: the double-check (21 ms on the development
Mac) and speaker ID for the name (43 ms) are all it waits for, and the line is made ahead. Each turn keeps how long
after the wake word the greeting started (`greet` in its timings). With `[audio] echo_cancel`, it's said with the mic
open: WebRTC's echo canceller (`echo.py`, AEC3, through LiveKit's SDK) is given everything the speaker plays and
takes it back out of what the mic hears, so you can carry straight on from "hey TARS" without your first words
being lost. If a trace of the greeting gets through, its words are taken off the start of what was heard
(`echo.without_echo`); if that's all there was, TARS keeps listening. On recorded speech through a simulated room it
removed about 30 dB once it had learned the room (a second or two), and speech recognition heard nothing of TARS's
own voice; when you talk over it, some of the overlapping words can be lost, as with any echo canceller. Measure it
in the room with `uv run python tools/echo_bench.py` (`--talk` to talk over it); until it has been, both are off, and
`greet = "pause"`, the default, says "Yes, <name>?" only once nothing has followed the wake word for `greet_after_s`.
If the canceller can't start or fails, TARS carries on with the mic closed while it speaks, as before; `--check`
says so, and warns when the devices' delay is more than the 0.5 s it can take.

**Failures.** The voice gives up after 4 s without audio instead of the client's 15 s, and TARS says so in its own
voice, from lines synthesized at startup ("I lost that one somewhere between here and the server. Ask me again.";
plainer below 50% humor). If Deepgram fails, OpenAI transcribes the same recording. Qwen runs on Groq first, then
Cerebras: if Groq hasn't started answering within 0.5 s, Cerebras is asked too, the first to say anything is kept, and
the other is closed before any of its tools run. A service that fails is skipped for the next two minutes, or after
"too many requests" for as long as it asks (a minute if it doesn't say), and with both out, Luna answers. A reply
stopped on purpose (you carried on talking) isn't a failure. A reply that runs into Qwen's cap of 500 tokens is
carried on by Luna (Groq refuses a request without a cap). A failed answer is kept in its conversation with where it
failed (speech to text, the answer, or the voice) and why, and Home shows it.

**Measured on every answer.** The same stages as the log line are kept with each of TARS's turns (`timings` in the
`turns` table, in seconds), with which model wrote it (`answered_by`: `quick` for Qwen, `look_up` when Qwen handed the
turn over, `ponder` when it went to the thinking model, `fallback` when Groq and Cerebras both failed or are resting,
`openai` with neither set up) and where Qwen ran (`quick_service`: `Groq` or `Cerebras`, hand-offs included). Home
shows the time to first sound and the model under each answer; hovering shows each stage. For a week's numbers:
`SELECT answered_by, quick_service, avg(json_extract(timings, '$.total')) FROM turns WHERE role = 'tars' GROUP BY
answered_by, quick_service`.

## Settings

| Setting | In `config.toml` | What it does |
|---|---|---|
| `[stt] provider` | `"flux"` | `flux`: Deepgram's Flux transcribes while you talk and decides when you're done. `deepgram`: Nova-3 transcribes while you talk, and the local rules below decide. `openai`: the finished recording is sent after the local rules decide (slowest, one key fewer) |
| `[llm] groq_model` | `"qwen/qwen3.8-27b"` | the quick brain, on Groq, asked first; empty, or no `GROQ_API_KEY`: not used |
| `[llm] cerebras_model` | `"qwen-3.8-27b"` | the same Qwen on Cerebras, when Groq is busy, failing or hasn't started answering within 0.5 s; empty, or no `CEREBRAS_API_KEY`: not used. Neither: OpenAI's `model` answers everything (~0.3 s slower) |
| `[tts] provider`, `model` | `"deepgram"`, `"aura-2-zeus-en"` | the voice; `openai` with `gpt-4o-mini-tts` takes delivery instructions but starts ~0.6 s later |
| `[stt] deepgram_host`, `[tts] deepgram_host` | `"api.eu.deepgram.com"` | Deepgram's servers: the EU's, or `api.deepgram.com` for the US (from Israel, the voice's first audio ~0.2 s later) |
| `[recorder] end_silence_s` | `0.8` | without Flux: silence that ends your turn |
| `[recorder] max_pause_s` | `1.6` | without Flux: how long a pause may be when you sounded mid-thought |
| `[recorder] end_of_turn` | `"smart"` | without Flux: `silence` turns Smart Turn off |
| `[recorder] greet` | `"pause"` | when to say "Yes, <name>?" after the wake word: `always` at once, `pause` only after `greet_after_s` of nothing, `never` |
| `[audio] echo_cancel` | `false` | take TARS's own sound out of what the mic hears, so the mic stays open while it greets you; off until checked in the room with `tools/echo_bench.py` |
| `[recorder] answer_early_s` | `0.25` | without Flux: when the answer starts being prepared; `0` waits for the end |
| `[recorder] vad_threshold` | `0.5` | how sure the speech detector must be |
| `[llm] humor` | `75` | the persona's humor setting, and which error lines TARS uses |

## Measuring

```bash
uv run python tools/latency_bench.py
uv run python tools/latency_bench.py --set recorder.end_silence_s=0.6 --set stt.provider=deepgram
uv run python tools/latency_bench.py --set tts.provider=openai --set tts.model=gpt-4o-mini-tts
uv run python tools/turn_bench.py      # cut-offs: the local end of turn against Flux, on your own sentences
```

The benchmark speaks 8 questions once (Deepgram's Aura voice, cached in `voice_data/bench/`), plays them to the
assistant at real-time pace over real room tone taken from your recordings, and measures from where the voice ends in
the audio to when the first reply sound would play (its speaker skips a voice's leading silence too). It uses the
real services, so it costs a few cents a run, and the numbers move by a few tenths between runs with the network and
the model; compare medians over several runs. Every turn also prints its own stage timings in the assistant's log.

## How it got here

Each step measured with the benchmark above, medians from you stopping to TARS's first sound:

| Setup | Time |
|---|---|
| OpenAI throughout: the recording uploaded and transcribed after a 0.5 s silence | ~3.5 s |
| Deepgram Nova-3 streamed, a local end of turn (Silero VAD and Smart Turn), Onyx's voice, Luna | 2.6-3.0 s |
| Flux decides the end of the turn | 2.0-2.6 s |
| Deepgram's Aura-2 Zeus voice instead of Onyx, counted to the first audible sound from here on | 2.1-2.5 s |
| Qwen on Cerebras answers first, Luna takes what needs the web | 1.8-2.0 s |
| The speaker skips the silence a voice starts on | 1.1-1.6 s |
| Deepgram's EU servers for Flux and the voice | 1.6-2.6 s |
| Qwen on Groq first, Cerebras when Groq is busy or slow | 1.3-1.4 s |

The last two were measured on 2026-10-09, alternating with the setup before them, which took 1.7-2.5 s that day:
compare each with that, not with 1.1-1.6 s. Every run kept had no failure, refusal, fallback or timeout, and the Mac
was training at the same time.

**Why Groq first.** Over the same runs, Qwen's first sentence took 0.38 s on Groq and 0.61 s on Cerebras (medians of
every turn), and Groq's runs moved less: 1.59-1.65 s with Deepgram's US servers, against 1.67-2.48 s for Cerebras.
Deepgram's EU servers took the voice's first audio from 0.58 to 0.39 s. Groq answered every turn; the half-second
hedge never brought Cerebras in. Groq's free key is the catch: 8,000 tokens a minute, 1,000 of them written, and a
daily allowance. When it says "too many requests", Cerebras answers until Groq is ready again, so a busy minute costs
about 0.2 s a turn rather than the answer. `quick_service` on each turn shows how often that happens in real use.

**Why Flux ends the turn** (`tools/turn_bench.py`): the owner's 25 recorded sentences, whole, and with a pause
spliced in after an unfinished word ("and", "the", "to"...), 0.5, 0.8 or 1.2 s long.

| | Silero VAD and Smart Turn | Flux |
|---|---|---|
| Turn over after a whole sentence | 0.88 s | 1.05 s median, 90% within 1.29 s |
| Cut-offs in a 0.5 / 0.8 / 1.2 s pause | 0 / 0 / **18 of 18** | 0 / 1 / 1 of 18 |

Smart Turn, checked after 0.8 s of silence, scored 17 of these 18 unfinished sentences as done, so it almost never
extended the wait. Spliced pauses aren't real hesitations (no "um", no stretched last word), which may flatter Flux.
Flux's early "maybe done", where a draft starts, came 0.65 s after the end of a sentence and was taken back in 20 of
the 54 paused ones: about one extra brain call in three.

**Choosing the brain.** First sentence with TARS's prompt over 24 questions: Cerebras Qwen 0.27 s (slowest 0.41
s), Groq Qwen 0.16 s, Groq gpt-oss-20b 0.35 s, Groq gpt-oss-120b 0.38 s, Cerebras gpt-oss-120b 0.27 s. Qwen
sounded the most like TARS; gpt-oss was correct but plain. On a free key Groq allows 8,000 tokens a minute (about 10
of TARS's requests) and Cerebras gpt-oss-120b 5 requests a minute; Cerebras Qwen allows 450. Qwen had no tools then: for
the web or the TARS page it replies `<look-up>` and the turn goes to Luna. Asked 12 questions that need them and 12
that don't, six times each, it handed off 70 of the 72 it should have and answered all 72 others itself. With
`quick_tools` it has its own for reminders and sending; `tools/capability_bench.py` checks them.

**Choosing the voice.** Time to first audio: Onyx 0.97-1.19 s, Aura-2 Zeus 0.34-0.48 s, Flux Cliff 0.39-0.44 s.
Flux Cliff now and then opens a sentence with about a second of silence, which undoes the gain. Onyx sounds best and
takes delivery instructions, but TARS picks latency over voice.

## Tried and dropped

| Tried | Result | Kept? |
|---|---|---|
| ElevenLabs Flash, Cartesia Sonic voices | 0.2-0.45 s to first audio, like Deepgram's | no: Deepgram's Aura-2 Zeus instead |
| Onyx at 1.2x speed (`speed`, pitch kept) | about 20% more words a second | no: it sounded bad |
| A backup voice when the voice stalls | worked, but the voice changes mid-reply | no: a 4 s timeout and a spoken error line instead |
| Ending the turn after 0.24 s when Smart Turn says "finished" | 1.6 s total, but it cut sentences in half | no: the model may only extend the wait |
| Speaking the reply's first clause before its sentence ends | no measurable gain (most replies are one sentence) | no |
| webrtcvad, with rules for its false alarms (tonal hums, lone noise blocks) | brittle: every rule fixed one case and broke another | replaced by Silero VAD |

Not tried: OpenAI's Realtime API (speech in, speech out), reported around 1 s, because it ties the brain to OpenAI's
realtime models.
