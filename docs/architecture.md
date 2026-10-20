# How TARS fits together

TARS is a few processes on one machine (a Mac while developing, a Raspberry Pi 5 at home) sharing one folder of
data: the assistant, which listens and answers; the web UI, which shows and edits what the assistant kept; and,
optionally, TARS on Telegram, which answers the household's chats ([TARS on Telegram](telegram.md)), and the backup,
which copies what TARS keeps to an S3 bucket every 15 minutes ([backing up to S3](backup.md)). Five principles shape
the design:

- **Local until it's sure.** The wake word, the double-check, the speech detector and speaker ID run on the machine.
  Nothing leaves it before a wake is confirmed ([what leaves the machine](#what-leaves-the-machine)), but for the
  backup, when it's on, which copies everything TARS keeps to the household's own S3 bucket later. After a wake,
  Deepgram's Flux, in the cloud, decides when you're done by default; the local end of turn (silence, then Smart
  Turn) is the fallback.
- **Streamed and overlapped.** Speech to text runs while you talk, the answer is drafted before your turn is
  confirmed over, and the voice speaks a sentence at a time ([response time](latency.md)).
- **Every stage behind a small interface**, so a provider or a local model is one class and a config change.
- **A failure costs at most a turn.** Each cloud stage has a fallback or a spoken error, and a failed write never
  costs a reply. A microphone that stops delivering audio is the exception: the assistant exits, and systemd
  restarts it ([when something fails](#when-something-fails)).
- **Every paid service is optional.** Any subset of the API keys runs, as long as something can hear, think and
  speak ([running on the keys there are](#running-on-the-keys-there-are)).

```mermaid
flowchart LR
    subgraph assistant ["voice-assistant (the assistant)"]
        mic[Mic] --> wake["Stage 1: wake model"] --> check["Stage 2: double-check"] --> rec["Record until you stop"]
        rec --> stt["Speech to text (streamed)"] --> llm["LLM, with web search and send"] --> tts["Text to speech"] --> spk[Speaker]
        rec --> sid["Speaker ID (in parallel)"] --> llm
        journal["journal.py"]
    end
    subgraph web ["voice-assistant --web (the web UI)"]
        api["FastAPI: /api/…"] --- page["React page"]
    end
    data[("voice_data/events/<br/>events.db + audio + files")]
    journal --> data
    api <--> data
    stt <-.-> deepgram["Deepgram"]
    tts <-.-> cartesia["Cartesia"]
    llm <-.-> groq["Groq"]
    llm <-.-> cerebras["Cerebras"]
    llm <-.-> openai["OpenAI"]
    phone["Phone / laptop browser"] <--> page
```

## The assistant

One mic stream, read in 80 ms blocks from one queue, so nothing fights over the audio device. On each block:

1. **Stage 1** (microWakeWord, local) scores the block. Close calls are logged as near-misses.
2. **Stage 2** (Vosk with a grammar plus a learned layer, local) decides: answer, ask "Did you call me?", or ignore.
   See [hearing "hey TARS"](wake-word.md).
3. **Recording** runs until you've finished. [Silero VAD](https://github.com/snakers4/silero-vad) (local, 2 MB)
   hears you start; Deepgram's [Flux](https://deepgram.com/learn/introducing-flux-conversational-speech-recognition)
   decides when you're done, from your words as well as the pause. If Flux fails, the local rules take over: after
   `end_silence_s` (0.8 s) of silence, [Smart Turn](https://github.com/pipecat-ai/smart-turn) (local, 8 MB) may keep
   waiting up to `max_pause_s` (1.6 s). A bare "hey TARS" followed by a pause gets "Yes, Alon?" (speaker ID from the
   wake alone) and waits.
4. **Speech to text** is Flux too, streamed while you talk, so the words come with its decision; if the stream
   fails, OpenAI transcribes the same recording. **Speaker ID** (WeSpeaker ResNet34 on ONNX, local) runs at the same
   time, so knowing who's talking adds no latency. The request reaches the LLM tagged `[Speaker: Alon]`.
5. **The LLM** answers in TARS's voice, streamed: Qwen on Groq first (about 0.4 s to its first sentence, at low
   reasoning with its tools). If Groq hasn't started answering within 0.5 s, the same Qwen on Cerebras is asked too;
   the first to say anything is kept, and the other is closed before any of its tools run. When the answer needs the
   web or the TARS page, Qwen replies `<look-up>`, and that turn goes to OpenAI's model (the Responses API, with web
   search and the send tool), which also answers when neither Groq nor Cerebras can; all of them share one
   conversation. Each finished sentence goes to **text to speech** (Cartesia's Sonic voice James; or a Deepgram
   voice, or OpenAI's Onyx) straight away, and playback starts on the first audio chunk, so TARS starts talking while
   the reply is still being written. With `[tts] effect = "tars"`, a speaker-in-a-metal-box effect is applied as it
   streams (off by default). All of this starts early, when Flux thinks you may be done, while the recording goes on:
   if you carry on talking the draft is thrown away, and it's only played, logged and allowed to send anything once
   your turn is confirmed over (see [response time](latency.md)).
6. **Follow-ups:** after answering, it listens a few more seconds without the wake word. The LLM answers `<skip>`
   when what it overheard wasn't meant for it, and TARS stays quiet and forgets it. The conversation is sent to the
   LLM until it's been quiet for `memory_minutes`.

Every stage sits behind a small interface (`Trigger`, `Transcriber`, `Brain`, `Voice`), so a local model is one new
class and a config change. Latency is printed for every turn, by stage, and `tools/latency_bench.py` measures it end
to end. When TARS wakes, it connects to Groq, Cerebras, OpenAI and the voice while you're still talking.

### When something fails

| What fails | What TARS does |
|---|---|
| Deepgram's speech-to-text stream, or Flux never sending a turn's last words | OpenAI transcribes the same recording, and the rest of that request, so a stalled stream costs its timeout once |
| Flux's end of turn | the local rules (Silero VAD, then Smart Turn) take over for that turn |
| Groq: slow to start | after 0.5 s, Cerebras is asked too, and the first to start answering is kept |
| Groq or Cerebras: an error, or "too many requests" | the next one answers, and the failed one is skipped for two minutes, or for as long as it asks after "too many requests" |
| Both Groq and Cerebras | OpenAI's model answers |
| Qwen's reply runs into its cap (500 tokens on Groq, for its free key; 2,000 on Cerebras) before saying or doing anything | Cerebras writes it, with room for a long note |
| Qwen's reply runs into its cap after saying or doing something | OpenAI's model carries on, told what was already said, so nothing is said twice |
| Qwen writes a tool call out as text (`<toolcall>`, `<function=`, also HTML-escaped) | the reply is cut off there, unsaid, and OpenAI takes the turn |
| Qwen writes a marker HTML-escaped (`&lt;look-up&gt;`, `&lt;skip&gt;`) | it still counts as the marker: it isn't spoken, and does what the marker does |
| The voice | gives up after 4 s without audio, then says a spoken error line ("That didn't work. Not my finest moment. Try again.", plainer below 50% humor), made at startup so it plays even when the voice service is what failed; a reminder or how a task on the TV went waits, with no chime, and is tried again a minute later; the lines made ahead are tried again once a minute, not at every chance |
| Speaker ID | "Yes?" for the wake word, and the request answered as from an unknown voice |
| The microphone stops delivering audio | the assistant exits so systemd restarts it |
| A write to the event log | `journal.py` absorbs it; the reply goes ahead |

### Running on the keys there are

**Constraint:** TARS uses five services with a key each (Deepgram, Cartesia, Groq, Cerebras and OpenAI), and not
every household will have an account with each, or keep it paid. A missing key shouldn't mean a TARS that won't
start, or one that fails on the first request.

**Design:** at startup, `config.with_keys` cuts `config.toml`'s setup down to the services `.env` has keys for, and
the rest of the code only sees the setup that runs:

| No key for | What runs instead |
|---|---|
| Groq or Cerebras | Qwen on the other one; with neither, OpenAI's model answers everything |
| Deepgram | OpenAI transcribes once you've stopped (the local rules decide when). Cartesia still speaks; with `[tts] provider = "deepgram"`, OpenAI's voice speaks instead |
| Cartesia | Deepgram's Flux voice Wes speaks, or OpenAI's without Deepgram |
| OpenAI | Deepgram hears (Flux) and Cartesia speaks; Qwen answers everything itself, hard questions included, with no web search or speech-to-text backup; it's told it can't look things up, so asked to, it says so |

Each change prints one line at startup ("no OPENAI_API_KEY: Qwen answers everything, with no web search, thinking
model or backup"). TARS stops, naming the keys, only when hearing, thinking or speaking has no service left. `--check`
reports the same lines as a warning, and `deploy/install-pi.sh` runs the same function, so the three never disagree.

**Trade-off:** a cut-down TARS is less capable, and says so once at startup rather than on every turn. Without
OpenAI, a failed quick service has nothing behind it, so when every one is being skipped they're asked anyway.
`tests/test_config.py` covers each case.

### The LLM's tools

- **Web search** (OpenAI's built-in tool, `[llm] web_search`): for current questions and real links. When the
  model starts a search, TARS says "Looking it up." so the wait isn't silent.
- **Send** (`[llm] send`, needs `[learning] log_events`): a strict function tool for a link, a note (short
  markdown), a list, or a text file (`.txt`, `.md`, `.csv`, `.ics`), for whoever asked or for the household. The
  assistant checks every call (links must be http(s), a file needs a known type and its text) and returns errors to
  the model, which gets up to three rounds; the last round can't call tools, so TARS always says something. Sent
  things appear in the web UI; TARS says "It's on the TARS page." It never reads a link aloud. The same thing (kind and
  title) sent twice in one reply is sent once, whichever model asks the second time. Something sent "for
  whoever asked" goes to the household when speaker ID didn't know who asked.
- **Reminders** (`remind`, `cancel_reminder`, `snooze_reminder`): see [reminders](reminders.md).
- **The house** (`home`, with `[[home.devices]]`): devices through Home Assistant, an Android TV box over ADB with an
  agent for tasks that take several steps on screen, and a plugin's own. A change is "done" only when the device
  reports it, waits until the turn is over (a draft thrown away changes nothing), and unlocking has rules in code: by
  voice, a trusted voice recognised surely on the request itself, and the word "unlock" heard. See
  [the house](home.md).

Requests to OpenAI use `store=False`. The send tool is off in `--text` mode and when logging is off, since there'd
be nowhere to put what it sends.

**Who does what.** Each model is told, from one list (`abilities()` in `llm.py`), what it can do itself, what it
hands over and how, and what TARS can't do at all, so it never claims something it can't do and never hands over
something it could have done. Turning a feature off in config changes both prompts. Qwen hands over:

| Marker | When | To | TARS says first |
|---|---|---|---|
| `<look-up>` | the web: weather, news, prices, a real link; and reminders, sending and the house, unless `quick_tools` is on | OpenAI's `model`, with all the tools | "Looking it up." once a search starts |
| `<ponder>` | real thinking: planning, comparing options, several steps | `think_model` (default `model`) at `think_effort`, for at most `think_timeout_s` | "Let me think about that for a moment." |

With `[llm] quick_tools` on, Qwen has the reminder tools and the send tool (notes, lists and files, but not links, which
need web search) and uses them itself, saving those turns the hand-off. A request with a part of each ("set a timer, and
what's the weather?") gets both: Qwen does its part with its tools, then hands the rest over, and what its tools did
goes along, so nothing is done twice (the tools also turn down a repeat). Told only to reply with the marker "and
nothing else", Qwen deliberated over which to do until it ran out of tokens, and the turn fell to OpenAI as if Qwen had
failed ([evals](evals.md#what-it-found)). Now and then, in a long reply, Qwen writes a tool call out as text
(`<toolcall> <function=send> ...`) instead of making it. Its words go to the voice as they come, so the reply is cut off
at the first sign of one (`TOOL_CALL_TEXT`, also when written HTML-escaped), unsaid, and OpenAI takes the turn. An
HTML-escaped marker (`&lt;look-up&gt;`, `&lt;skip&gt;`, `&lt;ack&gt;`) still counts as the marker: it isn't spoken, and
does what the marker does. Groq's replies are capped at 500 tokens (its free key allows 1,000 a minute), Cerebras's at
2,000: a reply that runs out on Groq before anything was said or done, like a long note, is written on Cerebras instead;
one that already said something goes to OpenAI, which is told what was said and carries on from it. A tool round never
says again what the reply already said. `tools/capability_bench.py` checks it gets them right; [choosing the quick
model](models.md) has every model and reasoning effort tried, and how to try the next. The marker isn't `<think>`
because Qwen 3 models write their own reasoning between those tags.

**Why a quick model that hands off, rather than one model for everything:** Qwen on Groq starts its first sentence
in about 0.4 s against about 0.7 s for OpenAI's model, and answers what it can itself. The turns that need the web
or real thinking go to models that are slower but stronger, and TARS says so first, so the wait isn't silent.

## What it keeps, and where

Everything the assistant writes goes to `voice_data/events/` (gitignored) through `journal.py`, whose writes can
fail without costing a reply. The layout, retention and labeling rules are in [self-learning](self-learning.md).
Voiceprints live in `voice_data/voiceprints.npz`; the personal wake models in `models/personal/` (gitignored). The
short lines made ahead ("Yes?", "Did you call me?", the error lines) are kept in `voice_data/phrases/`, one folder per
`[tts]` setup, so TARS can still say something went wrong after a restart with the network down. After changing the
voice effect's code (`effects.py`), delete that folder so the lines are made again. With `[backup]` on, all of it is
copied to an S3 bucket, and a request's audio leaves the machine after a month, living on in the bucket
([backing up to S3](backup.md)).

## What leaves the machine

- **Before the wake is confirmed, nothing.** Stage 1, stage 2, the speech detector and the fallback end-of-turn
  model run locally.
- **After it,** five services each get part of the turn (with the default `config.toml`); one with no key in `.env`
  gets nothing:
  - **Deepgram**, on its EU servers (`deepgram_host`), gets your request's audio, streamed for speech to text from
    when you start speaking after a wake.
  - **Cartesia** gets TARS's reply text, a sentence at a time, to speak it.
  - **Groq** gets the conversation so far (its text, with each request's `[Speaker: name]` tag and TARS's replies)
    and answers first.
  - **Cerebras** gets the same conversation when Groq is being skipped, fails, or hasn't started answering within
    0.5 s.
  - **OpenAI** gets the same conversation for the turns Qwen hands over (anything that needs the web or the TARS page)
    or can't answer, and runs those turns' web searches. With `[llm] home_city` and `home_country` set, both models
    and the web search are told the city and country, so "what's the weather?" is about home; nothing closer. It also
    transcribes a request's recording when Deepgram's stream fails. With `[stt] provider = "openai"`, or no Deepgram
    key, it gets the audio instead of Deepgram. It gets the reply text, to speak it, with `[tts] provider =
    "openai"`, with `provider = "deepgram"` and no Deepgram key, or with neither a Cartesia nor a Deepgram key.
    Without a Cartesia key but with Deepgram's, Deepgram gets the reply text instead.
- **With `[telegram]` on,** Telegram gets what's written and said in the household's chats with TARS, its replies
  there, and the reminders and things TARS sends to them ([TARS on Telegram](telegram.md#what-it-sends-telegram)).
  A voice message there goes to Deepgram (or OpenAI) to be written down, as a request at home does. TARS asks
  Telegram for new messages, so nothing at home is opened to the internet.
- **With `[home]` devices,** Home Assistant, the devices and an Android TV box are all on the home network. The
  models see only what the home tool answered, as part of the conversation; the TV's agent (OpenAI) also sees the
  TV's screen while it works a task. A plugin says what it sends: `examples/my_house` looks titles up on IMDb
  ([the house](home.md#what-leaves-the-house)).
- **When the web UI's Status page is opened,** each service TARS uses is asked for something small with its key (a
  model, the voices, the key's or the bot's own details: no conversation, no tokens), and each one's public status
  page is read (nothing is sent to it).
- **With `[backup]` on,** your own S3 bucket gets everything TARS keeps: the recordings, the files it sent, and every
  six hours the database (transcripts and voice embeddings included), the voiceprints, the wake models trained on
  the household and `config.local.toml`, encrypted at rest ([backing up to S3](backup.md#what-it-sends-aws)). `.env`
  never goes.
- **Never, without `[backup]`:** the event log, the audio kept for learning, voiceprints, and anything else the web UI
  shows. The web UI is served from the same machine, and talks to nothing else but the Status page's checks and,
  with `[backup]` on, that bucket, to play a recording that's moved there.

## The web UI

`voice-assistant --web` is a separate process: a FastAPI server (`webui.py`) over the same database, serving the
React page built from `webui/` (committed as `src/voice_assistant/webui_static/`, so the Pi needs no Node). The
assistant writes and the web UI reads and edits (as does TARS on Telegram, when it's on): SQLite in WAL mode lets
them all work at once, and every change of more than one row is one transaction. Each runs as its own service, so a
crash in one never takes another down. The web UI is described in [the web UI](web-ui.md).

**Each process writes a heartbeat**, `voice_data/events/heartbeats/<name>.json`, every 30 seconds (`health.py`), and
the web UI's Status page reads them: a process whose heartbeat is older than three beats has stopped, crashed or
frozen whole, on a Mac as on the Pi ([the Status page](web-ui.md#the-status-page)). It's written from a thread of
its own, never touches the database, and one that can't be written costs a line in the log.

**It has no accounts**, so it's meant for the home network, and it protects itself against the two ways a web page
elsewhere could reach it through someone's browser:

- **Host allow-list:** it only answers requests addressed to an IP address, `localhost`, `*.local`, `*.ts.net`, a
  single-label name (`tars`, the Pi's hostname) or a name in `[web] allowed_hosts`. Anything else gets 421, which
  stops DNS-rebinding attacks.
- **Same-origin changes:** a request that changes something (POST, DELETE) must come from the TARS page itself, a
  page at a name in `[web] allowed_hosts`, or `localhost`; anything else gets 403.

To use it away from home, put the Pi and the phone on Tailscale rather than forwarding a port.

## Code map

The assistant is `src/voice_assistant/`:

| Area | Files |
|---|---|
| Audio in and out | `audio.py` (mic stream, devices by name, playback), `effects.py` (the TARS speaker box), `echo.py` (the WebRTC echo canceller that takes TARS's greeting back out of what the mic heard), `mic_test.py` (the `--mic-test` meter) |
| Hearing "hey TARS" | `wake.py` (stage 1, push-to-talk), `verify.py` (stage 2), `versions.py` (trained pairs, which is in use, switching live) |
| Hearing when you're done | `stt.py` (Flux), `recorder.py`, `vad.py` (Silero), `turn.py` (Smart Turn, the fallback) |
| Understanding and answering | `stt.py` (Deepgram, OpenAI as backup), `llm.py` (Qwen with its own tools on Groq and Cerebras, OpenAI with its tools, the thinking model), `speech.py` (sentence pipelining), `tts.py`, `phrases.py` (the short lines made ahead), `draft.py` (start early, speak late), `errors.py` (a service failing, told from a bug) |
| Reminders | `reminders.py` (the table, what's due, where it's said, what to say, the brain's tools), and in `assistant.py` saying them and hearing "got it" |
| Telegram | `telegram.py` (`--telegram`: the household's chats, posting reminders and what TARS sent, the Bot API) |
| The house | `home.py` (the `home` tool, its rules, the `Device` interface, plugins), `hass.py` (Home Assistant and its devices by domain), `androidtv.py` (an Android TV box over ADB, and the agent that works it), `found.py` (finding a device again when the router gives it another address, kept in `voice_data/found.json`), and in `assistant.py` saying how a task went (`Announcements`); a plugin's devices in its own module, like `examples/my_house` |
| Who's talking | `speaker.py` (voiceprints), `clustering.py` (grouping voices), `enroll.py` (recording people) |
| The main loop | `assistant.py` (wake, listen, answer, follow-ups, errors, timing), `__main__.py` (wiring, command line), `config.py` (`config.toml`, and `with_keys`, which cuts it down to the keys there are) |
| What's kept | `store.py` (database, audio, upgrades), `events.py` (wakes and labels), `conversations.py` (turns and sent things), `journal.py` (writes that never cost a reply), `files.py` (crash-safe writes), `backup.py` (`--backup`: to S3, making room, snapshots, lifecycle, bringing back) |
| Downloads | `models.py` (the small local models, on first use) |
| Checking a setup | `check.py` (`--check`: keys, devices, models, echo cancellation, voiceprints, the services and Telegram, voice, mic, storage, the web UI, Home Assistant and each device in the house, the backup, and systemd's services) |
| Is it all running | `health.py` (heartbeats, today's counts, the services and their status pages, the machine: what the Status page shows) |
| The web UI | `webui.py` (server and API), `webui/` (the React page), `tools/webui_demo.py` (demo data) |

Around it: `training/` (the scripts that made the wake models, [training/README.md](../training/README.md)),
`tools/` (benchmarks: `latency_bench.py`, `turn_bench.py`, `wakeword_bench.py`, `verifier_bench.py`;
`capability_bench.py` and `router_bench.py` for the quick model's tools and hand-offs; `echo_bench.py` for echo
cancellation in the room), `evals/` (whole conversations played through the brain, hand-offs followed through,
[docs/evals.md](evals.md)), `deploy/` (the Pi's install script and services, and `home-assistant/`: Home Assistant in
Docker), `examples/` (plugins: one household's own devices, [examples/my_house](../examples/my_house/README.md)) and
`tests/`.
