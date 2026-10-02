# What's next

Four steps, in this order. Each one makes the next one better: real use shows whether speaker ID and the wake word
hold up on the speakerphone (memory depends on the first); interrupting matters more once answers can run long (the
larger model); the larger model is one more hand-off, built the way `<look-up>` already is, and memory is a tool,
built the way the send tool is.

| | Step | Why now | Size |
|---|---|---|---|
| 1 | [A week of real use](#1-a-week-of-real-use) | the first real wakes, voices and conversations; everything after needs them | no code, a checklist |
| 2 | ["TARS stop": interrupting a reply](#2-tars-stop-interrupting-a-reply) | the one daily annoyance left; longer answers make it worse | medium |
| 3 | [Handing hard questions to a larger model](#3-handing-hard-questions-to-a-larger-model) | smallest new feature, self-contained, keeps the brain swappable | small |
| 4 | [Long-term memory and personalization](#4-long-term-memory-and-personalization) | the biggest one, and it leans on speaker ID being right | large |

Benched for now: [learning from how conversations go](#benched-learning-from-how-conversations-go).

## 1. A week of real use

With the speakerphone on the Mac (or the Pi), run the assistant and the web UI side by side for a week, starting
with the 20-minute [first test run](deployment.md#a-first-test-run). No new code: the point is data and a list of
what actually goes wrong.

**Before starting:** set `input_device` and `output_device` in `config.toml` to the speakerphone's name
(`uv run voice-assistant --list-devices`), and check it with `--mic-test` from across the room. Speaker ID's
voiceprint was made on the laptop mic; if TARS doesn't greet you by name, record and enroll again on the speakerphone
(`--record-voice alon --mic powerconf`, then `--enroll alon --mic powerconf`), or name your voice in the Voices tab
once it has 5 requests.

**Keep track of:**

| What | Where it shows | Good enough |
|---|---|---|
| Missed "hey TARS" | near-misses in Review, and your own notes | rare in a quiet room |
| False wakes (TV, talk) | Review | at most one or two a day, none answered |
| Wrong names | Home, a conversation's "who was talking" | rare once voiceprints come from the speakerphone |
| Cut off mid-sentence | your notes | never |
| Slow replies | the `TOTAL to first sound` line in the assistant's log | about 1.1-1.6 s, as on the laptop ([response time](latency.md)) |
| Answers you'd rate bad | Home's good / bad buttons | a list, for steps 3 and 4 |

**At the end of the week:** answer what's left in Review and name the voices. The Models page then shows how
many real, missed and not-real wakes there are to train on. Decide whether the order below still holds: if missed
wakes, false wakes or wrong names are the real problem, they come first.

## 2. "TARS stop": interrupting a reply

**Today:** the mic is muted while TARS speaks (`Microphone.paused()` around playback in `assistant.py`), so nothing
can interrupt it. A "TARS stop" wake phrase was trained alongside "hey TARS" and parked: it turned into a detector
for the word "stop" ([wake word](wake-word.md), tried and dropped).

**Plan:**

1. **Listen while speaking.** Keep the mic open during playback and run the wake model on it. This is what the
   speakerphone makes possible: its echo cancellation removes most of TARS's own voice from what the mic hears,
   which a laptop mic and speaker can't.
2. **Start with "hey TARS" as the interrupt.** It's the phrase that already works, and it means what people expect:
   "hey TARS" in the middle of an answer stops it and listens for a new request. The double-check still has to
   agree, so a lookalike in TARS's own answer doesn't cut it off.
3. **On an interrupt:** stop the playback, the reply (`StreamedReply.stop()`) and the brain (`brain.interrupt()`),
   all of which exist; keep what was already said in the conversation, marked as cut off; then record the new
   request, with no greeting.
4. **"TARS stop" later, if still wanted.** Retrain it with the stage 1 recipe that worked for "hey TARS",
   with "stop", "top", "star stop" and TARS's own voice saying them as negatives, which is what the first try lacked.
   A bare stop (no new request) is then its own action: cut the reply and go back to waiting.

**Measure before turning it on:** false interrupts per hour of TARS talking (play an hour of its own answers through the
speakerphone, with and without the TV on), and how long from the phrase to silence. Done when false interrupts are about
zero and it stops within half a second.

**Open question:** whether the Pi can run the wake model and the voice's playback at once without glitches. It
should (the wake model is tiny), but it's the first time both run together.

## 3. Handing hard questions to a larger model

When a question needs real thinking (planning a trip, comparing options, a tricky calculation), TARS says
"Let me think about that for a moment." and comes back with an answer from a larger, slower model.

**Design:**

- **A `<think>` hand-off**, next to `<look-up>` in `llm.py`. Qwen on Cerebras gets no tools: for the web or the TARS
  page it already replies `<look-up>` and the turn goes to OpenAI's model. A second marker sends a turn to the larger
  model instead. Its line in Qwen's prompt decides when: questions that need several steps of reasoning, never small
  talk, facts or anything web search answers.
- **No silence.** The moment the hand-off starts, TARS plays a line made ahead of time, like the spoken error lines
  (`ERROR_LINES` in `assistant.py`), dry or plain by the humor setting.
- **The larger model is a second brain**, set in config (`[llm] think_model`, with its own reasoning effort), with
  the same conversation and web search. Its answer is streamed and spoken like any other, so the brain stays
  swappable: any model can be the thinker.
- **Its own time limit** (`think_timeout_s`, around 45 s), with a spoken "I couldn't work that one out in time."
  when it runs out. With step 2 done, "hey TARS" interrupts a long answer too.
- **Visible in the web UI:** a turn that went to the larger model is marked, with how long it took.

**Measure:** on 50 typical questions from step 1's log, how often it hands off (it should be rare, and never for
the simple ones), the time to the "let me think" line (should be the usual time to first sound), and the cost per
call.

**Open question:** whether Qwen hands off when it should, and only then. If it doesn't, a stricter line in its
prompt comes first; a separate classifier only if that fails.

## 4. Long-term memory and personalization

TARS remembers things that matter over time, per person: "Stacey is vegetarian", "Alon's standup is at 9:30",
"the car is a 2019 Corolla". It can offer to remember something, anyone can say "remember this" or "forget that",
and the web UI shows exactly what it keeps, where it can be edited or deleted. Personalization is the same page:
each person's settings, and the persona part of the system prompt.

**Design:**

- **Storage:** a `memories` table in the event database (`store.py`, with the usual automatic upgrade): the text,
  whose it is (a person, by the same names speaker ID uses, or the household), the conversation turn it came from,
  and when it was made and last changed.
- **Writing:** `remember` and `forget` tools, like the send tool, so those turns go to OpenAI's model the way
  `<look-up>` turns do. TARS offers only for lasting facts and
  preferences, at most once a conversation, and never for anything said to someone else. The call doesn't hold
  up the reply: TARS speaks what the model said and saves in the background, without another round to the model.
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
people corrected, "not meant for TARS", the same question asked again right after an answer, interruptions (after
step 2). It groups them, and suggests prompt changes, tested by replaying past conversations through the old and the
new prompt, the way new wake models are tested before they're installed. That's more telling than sentiment scores on
transcripts. Benched until there are a few hundred real conversations to learn from.
