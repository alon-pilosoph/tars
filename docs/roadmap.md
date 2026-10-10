# What's next

Four steps, in this order, each feeding the next. Real use shows whether speaker ID and the wake word hold up on the
speakerphone, and memory depends on speaker ID being right. Interrupting matters more now that a hand-off to a larger
model can make answers run long. Memory is the largest step, built as a tool the way the send tool is.

| | Step | Why now | Size |
|---|---|---|---|
| 1 | [A week of real use](#1-a-week-of-real-use) | the first real wakes, voices and conversations; everything after needs them | no code, a checklist |
| 2 | ["TARS stop": interrupting a reply](#2-tars-stop-interrupting-a-reply) | the one daily annoyance left; longer answers make it worse | medium |
| 3 | [Handing hard questions to a larger model](#3-handing-hard-questions-to-a-larger-model) | built; measuring is left | small |
| 4 | [Long-term memory and personalization](#4-long-term-memory-and-personalization) | the biggest one, and it leans on speaker ID being right | large |

Benched for now: [learning from how conversations go](#benched-learning-from-how-conversations-go).

[Reminders, timers and messages](reminders.md) were built alongside step 1, because people will ask for a timer
during the week of real use and TARS couldn't set one. The week checks them too: "got it" and its variants taken as
acknowledgements, nothing said over a conversation, and one set from a phone said on time.

## 1. A week of real use

**Goal:** data, and a list of what actually goes wrong. With the speakerphone on the Mac (or the Pi), run the
assistant and the web UI side by side for a week, starting with the 20-minute
[first test run](deployment.md#a-first-test-run). No new code.

**Before starting:** set `input_device` and `output_device` in `config.toml` to the speakerphone's name
(`uv run voice-assistant --list-devices`), and check it with `--mic-test` from across the room. Speaker ID's
voiceprint was made on the laptop mic; if TARS doesn't greet you by name, record and enroll again on the speakerphone
(`--record-voice alon --mic powerconf`, then `--enroll alon --mic powerconf`), or name your voice in the Voices tab
once it has 5 requests.

**What to track:**

| What | Where it shows | Good enough |
|---|---|---|
| Missed "hey TARS" | near-misses in Review, and your own notes | rare in a quiet room |
| False wakes (TV, talk) | Review | at most one or two a day, none answered |
| Wrong names | Home, a conversation's "who was talking" | rare once voiceprints come from the speakerphone |
| Cut off mid-sentence | your notes | never |
| Slow replies | under each answer in Home, with the model that wrote it (and `timings` in the `turns` table) | about 1.3-1.4 s, as on the laptop ([response time](latency.md)) |
| Turns Groq couldn't take | `quick_service` in the `turns` table: `Cerebras`, or none with `answered_by = 'fallback'` | rare; if Groq's free key runs out often, a paid Groq key or Cerebras first |
| Failed answers | Home, in red: where it failed (speech to text, the answer, the voice) and why | rare, and no one stage over and over |
| Answers you'd rate bad | Home's good / bad buttons | a list, for steps 3 and 4 |

**At the end of the week:** answer what's left in Review and name the voices. The Models page then shows how many
real, missed and not-real wakes there are to train on. Then check the order below still holds: if missed wakes, false
wakes or wrong names are the real problem, they come first.

## 2. "TARS stop": interrupting a reply

**Goal:** say "hey TARS" (later, "TARS stop") while TARS is answering, and it stops.

**Where it stands.** For answers, the mic is muted while TARS speaks (`Microphone.paused()` around playback in
`assistant.py`), so nothing can interrupt it. The pieces for listening while speaking are built:

- **Echo cancellation.** TARS can take its own sound back out of what the mic hears (`echo.py`,
  `[audio] echo_cancel`: WebRTC's echo canceller, on any mic and speaker), and keep the mic open while it says
  "Yes, <name>?", so what you say straight after "hey TARS" isn't lost (`[recorder] greet = "always"`; see
  [the greeting](latency.md#the-greeting-and-the-open-mic)). Both are off by default until they've been measured in
  the room with `tools/echo_bench.py`: `echo_cancel = false`, and `greet = "pause"` says "Yes, <name>?" only after a
  pause.
- **A "TARS stop" model**, trained with "stop" on its own and in sentences as negatives. It catches 99% of the
  held-out voices in quiet and 87% at 5 dB of TV, and 76% of the owner's held-out takes across all 8 conditions,
  with no false answers in an hour of TV or audiobooks and no false interrupts in 23 minutes of TARS's own replies
  ([wake word](wake-word.md#tars-stop)). It isn't in use yet.

**Plan:**

1. **Listen while speaking.** Keep the mic open during answers too, and run the wake model on it. The echo
   cancellation it needs is built; `tools/echo_bench.py` measures it in the room, and the speakerphone's own
   cancellation adds to it.
2. **Start with "hey TARS" as the interrupt.** It's the phrase that already works, and it means what people expect:
   "hey TARS" in the middle of an answer stops it and listens for a new request. The double-check still has to
   agree, so a lookalike in TARS's own answer doesn't cut it off.
3. **On an interrupt:** stop the playback, the reply (`StreamedReply.stop()`) and the brain (`brain.interrupt()`),
   all of which exist; keep what was already said in the conversation, marked as cut off; then record the new
   request, with no greeting.
4. **Then "TARS stop".** It still needs a test with TARS talking and echo cancellation on, where it will be used
   (with TARS's voice still in the signal it catches about half), and the owner's own takes. A bare stop (no new request) is then its own action: cut the reply and go back
   to waiting.

**Measure before turning it on:** false interrupts per hour of TARS talking (play an hour of its own answers through
the speakerphone, with and without the TV on), and how long from the phrase to silence. **Done when** false
interrupts are about zero and it stops within half a second.

**Open question:** whether the Pi can run the wake model and the voice's playback at once without glitches. It
should (the wake model is tiny), but it's the first time both run together.

## 3. Handing hard questions to a larger model

**Built**, alongside Qwen's own tools: see [who does what](architecture.md#the-llms-tools). Qwen replies `<ponder>`
for a question that needs real thinking (planning, comparing options, several steps), TARS says "Let me think about
that for a moment.", and `[llm] think_model` (default: `model`) answers at `think_effort` (default high), with the
same conversation, tools and web search, within `think_timeout_s` (45 s). The web UI marks those turns. Each model is
told, from one list, what it can do itself, what it hands over, and what TARS can't do at all.

**Design notes.** The marker isn't `<think>`, because Qwen 3 writes its own reasoning between those tags. The "let me
think" line goes through the voice like any sentence rather than being made ahead of time, and running out of time
says the usual error line rather than one of its own.

**Measured** ([choosing the quick model](models.md)): Qwen with its own tools at low reasoning gets reminders,
sends and web hand-offs right, where without reasoning it often said a timer was set when it wasn't. It stays the
quick model, against gpt-oss-120b, Haiku 5.5 and Luna; Jev, tried as a router in front, would save little.

**Left to do:** on 50 typical questions from step 1's log, measure how often it hands off to the thinking model (it
should be rare, and never for the simple ones), the time to the "let me think" line (it should be the usual time to
first sound), and the cost per call. If Qwen hands off too often or too rarely, a stricter line in its prompt comes
first. Then try Jev on TARS's yes/no decisions (an overheard follow-up, an acknowledgement), where it fits better.

## 4. Long-term memory and personalization

**Goal:** TARS remembers things that matter over time, per person: "Stacey is vegetarian", "Alon's standup is at
9:30", "the car is a 2019 Corolla". It can offer to remember something, anyone can say "remember this" or "forget
that", and the web UI shows exactly what it keeps, where it can be edited or deleted. Personalization is the same
page: each person's settings, and the persona part of the system prompt.

**Design:**

- **Storage:** a `memories` table in the event database (`store.py`, with the usual automatic upgrade): the text,
  whose it is (a person, by the same names speaker ID uses, or the household), the conversation turn it came from,
  and when it was made and last changed.
- **Writing:** `remember` and `forget` tools, given to Qwen with its reminder and send tools (`[llm] quick_tools`),
  so those turns skip the hand-off; with `quick_tools` off they go to OpenAI's model the way `<look-up>` turns do.
  TARS offers only for lasting facts and preferences, at most once a conversation, and never for anything said to
  someone else. The call doesn't hold up the reply: TARS speaks what the model said and saves in the background,
  without another round to the model.
- **Reading:** everything kept for the person speaking, plus the household's, goes into the prompt as a short
  "what you know" block. No search system: a household's memories fit in the prompt for a long time (with a cap
  and a warning well before they don't).
- **Only when sure who's talking.** Personal memories are used only when speaker ID's match is confident;
  otherwise only the household's. A wrong guess would put one person's memories in another's conversation, which
  is why step 1 checks speaker ID on the speakerphone first.
- **A Memory page in the web UI:** per person and for the household, every memory with where it came from (a
  link to the conversation), edit, delete, and add by hand. Deleting really deletes.
- **Personalization:** each person's settings on the same page, starting with humor (overriding `[llm] humor`).
  The system prompt splits in two: the persona (who TARS is, how it talks), editable there, with `config.toml`'s
  as the default; and the rules the code depends on (skipping overheard chatter, the send tool, memory), which stay
  in the code, so the page can't break them.

**Measure:** a set of scripted conversations (tell it a fact, ask about it days later, a second voice asking the
same, "forget that"), run through `--text` and through the speakerphone, checking what's kept, what's used, and that
one person's memory never reaches another's answer.

**Open question:** what TARS offers to remember. Too eager and it's a nag; the week of real use will show which
facts come up again.

## Benched: learning from how conversations go

An offline job that reads the logged conversations and finds where TARS did badly: answers rated bad, transcripts
people corrected, "not meant for TARS", the same question asked again right after an answer, and interruptions
(after step 2). It groups them and suggests prompt changes, tested by replaying past conversations through the old
and the new prompt, the way new wake models are tested before they're installed. That's more telling than sentiment
scores on transcripts. **Benched** until there are a few hundred real conversations to learn from.
