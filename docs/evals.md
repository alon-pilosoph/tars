# Evaluating whole conversations

The [capability benchmark](models.md) asks the quick model one request at a time, and stops at a hand-off: it checks
that Qwen *decided* to hand a turn over, not what happened next. But most of what can go wrong in TARS happens across
models and across turns. OpenAI answers a weather question, and the follow-up comes back to Qwen with that answer in
front of it. Qwen sets a timer and then hands the rest of the request over, and OpenAI must not set it again. A timer
set while Groq is down has to be cancellable a turn later, by whichever model takes it.

The scenario evals (`evals/`, written up in [evals/README.md](../evals/README.md)) play whole conversations through
TARS's real brain, hand-offs followed through to OpenAI's model and the thinking model, and check every turn.

## Method

- **29 scenarios, 46 turns**, in four groups: hand-offs (9), reminders over a conversation (9), the conversation
  itself (8: follow-ups without the wake word, memory, voices, what TARS can't do, its humor setting) and failures (3).
- **TARS's own brain and prompts**, set up from `config.toml`: Qwen (`qwen-3.8-27b`) at low reasoning with its own
  tools, OpenAI's `gpt-6-luna` for the web and as the fallback, and the same model at high reasoning for hard
  questions.
- **Qwen runs on Cerebras.** It's the same model TARS asks on Groq first, and Cerebras's paid tier allows 150,000
  tokens a minute against Groq's free 7,000-8,000, so a whole run takes about a minute instead of a day. Whether Groq's
  copy answers the same way is [not yet checked](#limitations).
- **Played as the assistant plays them**: prompts tagged with the speaker and how they were heard, a `<skip>`
  forgotten, an `<ack>` marking the reminder acknowledged, and each turn's reminder changes made once it's over, so a
  later turn sees them. Failures are made on purpose: Qwen's service down, or saying "too many requests".
- **Checked by fixed rules, no judge yet**: where the turn went, exactly which reminders it changed and what it sent
  (so anything done twice shows), which reminders are set afterwards, words that must or mustn't appear, and on every
  turn, what should never be said aloud (tags, lists, web addresses, emoji, a sentence twice, silence). Text is checked
  as heard, after the voice's own cleaning (`speech.clean_for_speech`).
- **OpenAI on its standard tier.** `config.toml` asks for priority processing (`service_tier = "fast"`), which costs
  more per token and changes only how soon OpenAI answers, so the evals leave it off unless `--fast` asks for it.
  The baseline below ran on it, before that was the default.
- **Three runs of each**, run 6 at a time. Times are from the request to the first words, with runs in parallel, so
  they are a little slower than one at a time ([response time](latency.md) has the careful numbers).

## Results

2026-10-17, on `main` plus this change (`evals/results.jsonl`; every run's transcript was kept). Intervals are 95%
Wilson intervals: with three runs a scenario, they're wide, and a group's is the one to read.

| Group | Scenario runs right | 95% interval |
|---|---|---|
| Follow-ups, memory, voices, abilities, persona | 24/24 | 86-100% |
| Reminders over a conversation | 25/27 | 77-98% |
| Failures (Qwen down or rate-limited mid-conversation) | 9/9 | 70-100% |
| Hand-offs | 18/27 | 48-81% |
| **All** | **76/87** (126/138 turns) | **79-93%** |

An earlier full run the same day, before the harness kept transcripts, came out at 77/87, with the same failures.

| Where the turn went | First words, median / 90th percentile (s) | Turns |
|---|---|---|
| Qwen answered | 0.63 / 1.17 | 88 |
| Handed to OpenAI (web) | 1.08 / 1.25 | 15 |
| Handed to the thinking model ("Let me think about that for a moment.") | 0.38 / 1.25 | 7 |
| OpenAI, because Qwen couldn't | 1.64 / 2.62 | 22 |

A full run is 217 requests: 146 to Qwen (341,000 tokens in, 19,000 out, 12,000 of them reasoning) and 71 to OpenAI
(589,000 in, mostly web search results, and 12,000 out).

**What holds up:** a follow-up after a web answer is handed over again rather than answered from the old one (3/3);
Qwen answers from OpenAI's answer when it can (spelling a name from it); corrections ("actually, make it 8", "cancel
that") leave exactly the right reminders; overheard chatter gets a `<skip>`; a quiet spell past `memory_minutes`
really forgets; and when Qwen's service fails mid-conversation, OpenAI carries the conversation on, timers included.

## What it found

Nine of the eleven failures are hand-offs. They come down to five problems in TARS, none of which the capability
benchmark could see. They're listed here, and fixed one pull request at a time, each measured with these scenarios.

1. **A request with two parts rarely reaches the right model.** "Set a timer for ten minutes, and what's the weather
   tomorrow?": Qwen sets the timer, then, instead of replying `<look-up>`, keeps reasoning until it runs into its
   500-token cap (`QUICK_MAX_TOKENS`), or ends with nothing to say. The turn then falls to OpenAI as if Qwen had
   failed (7 of 9 runs of the three two-part scenarios), or Qwen says "I'll have to look that up" and never hands it
   over (1 of 9), so nothing is looked up at all. A fallback is slower (1.64 s median to first words, against 1.08 s
   for a clean hand-off), and a hard question in a two-part request mostly never reaches the thinking model (1/3).
2. **Something gets done twice.** After that fallback, OpenAI is shown what Qwen's tools already did, and still set the
   bank reminder a second time (twice across the day's runs). Asked to "send that to me", Qwen sent the same note
   twice in one reply (1 of 3 runs, and once in the earlier run).
3. **A tool call read aloud.** Asked to send a long trip plan, Qwen once wrote its tool call as text (`<toolcall>
   <function=send> <parameter=kind> file ...`) and ran into its token cap. Its words are streamed to the voice as they
   come, so TARS would have started reading the tool call out before OpenAI took over.
4. **"Remind me again in ten minutes" can lose the reminder.** Answering a reminder TARS just said, Qwen once both
   acknowledged it (`<ack>`) and put it off ten minutes, and said "Pushed to 11:19." The assistant marks the
   acknowledgement first, and a reminder that's been acknowledged can't be put off, so it would never be said again
   (the order is in `assistant.py`: `_ack` before `_change_reminders`).
5. **OpenAI doesn't know where home is.** Asked about the weather, it guesses Israel from nothing, or asks for "your
   city or ZIP code" (a third of weather answers). TARS has no home-location setting to give its web search.

The other two failures are judgment calls the scenarios hold TARS to: with two timers running, "cancel the timer"
cancelled one without asking (1 of 3), and a message for when Stacey's back was set as a reminder that waits for her
instead of a message (1 of 3; it's said the same way).

## Limitations

- **No judge yet.** Fixed rules check what was done and what mustn't be said, not whether an answer is right or sounds
  like TARS. That's the next step: Claude as a judge, checked against the owner's own labels first.
- **Cerebras, not Groq.** TARS asks Groq first. Both list the same model, but neither says how it's served, so a slow
  run on Groq's free key, a few turns a minute over several days, should confirm the two answer alike.
- **Three runs each** tell a problem that happens most of the time from one that doesn't, not 90% from 95%: a
  scenario is only called worse than the baseline when it drops by half its runs (`evals/report.py`).
- **The web answers are real**, so a scenario's result can depend on the day's news and weather.
