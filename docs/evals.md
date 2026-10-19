# Evaluating whole conversations

The [capability benchmark](models.md) asks the quick model one request at a time, and stops at a hand-off: it checks
that Qwen *decided* to hand a turn over, not what happened next. But most of what can go wrong in TARS happens across
models and across turns. OpenAI answers a weather question, and the follow-up comes back to Qwen with that answer in
front of it. Qwen sets a timer and then hands the rest of the request over, and OpenAI must not set it again. A timer
set while Groq is down has to be cancellable a turn later, by whichever model takes it.

The scenario evals (`evals/`, written up in [evals/README.md](../evals/README.md)) play whole conversations through
TARS's real brain, hand-offs followed through to OpenAI's model and the thinking model, and check every turn.

## Method

- **47 scenarios, 64 turns**, in five groups: hand-offs (9), reminders over a conversation (9), the conversation
  itself (8: follow-ups without the wake word, memory, voices, what TARS can't do, its humor setting), failures (3)
  and [the house](home.md) (18, in `evals/scenarios/house.toml`). The house's play TARS's real home tool over a
  pretend Home Assistant and TV box: only what was asked changed, only what the tool answered said, and the door
  unlocked for a trusted voice and nobody else. They came after the results below, which are of the first 29
  scenarios (46 turns), and are part of every run now, but no run of them has been saved yet.
- **TARS's own brain and prompts**, set up from `config.toml`: Qwen (`qwen-3.8-27b`) at low reasoning with its own
  tools, OpenAI's `gpt-6-luna` for the web and as the fallback, and the same model at high reasoning for hard
  questions.
- **Qwen runs on Cerebras.** It's the same model TARS asks on Groq first, and Cerebras's paid tier allows 150,000
  tokens a minute against Groq's free 7,000-8,000, so a run of the first 29 took about a minute instead of a day.
  TARS's two services are both played there, each with its own reply cap (Groq's 500 tokens, then Cerebras's 2,000),
  so a reply too long for Groq runs out as it would at home. Whether Groq's copy answers the same way is
  [not yet checked](#limitations).
- **On request, never a check.** Evals run from the command line, or from a pull request comment (`#evals`), and
  nothing waits on them ([evals/README.md](../evals/README.md#on-a-pull-request)).
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

Three runs of those 29 scenarios are 217 requests: 146 to Qwen (341,000 tokens in, 19,000 out, 12,000 of them
reasoning) and 71 to OpenAI (589,000 in, mostly web search results, and 12,000 out). They cost about $0.90: about
$0.50 on OpenAI's standard tier, most of it the 45 or so web searches ($10 per 1,000, on any tier), and about $0.40
for Qwen on Cerebras, mostly its long instructions. `--estimate` says what a run will cost before it starts; until a
run of the house's scenarios is saved, it counts each of them as an average scenario.

**What holds up:** a follow-up after a web answer is handed over again rather than answered from the old one (3/3);
Qwen answers from OpenAI's answer when it can (spelling a name from it); corrections ("actually, make it 8", "cancel
that") leave exactly the right reminders; overheard chatter gets a `<skip>`; a quiet spell past `memory_minutes`
really forgets; and when Qwen's service fails mid-conversation, OpenAI carries the conversation on, timers included.

## What it found

Nine of the eleven failures are hand-offs. They come down to six problems in TARS, none of which the capability
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
   (the order is in `assistant.py`: `_ack` before `_change_reminders`). With the scenario made strict (exactly one
   reminder afterwards), a worse problem showed: asked that way, Qwen mostly **set a second reminder** beside the one
   waiting, so it would be said twice (5 of 6 runs), and new reminders often copied the words TARS says around one
   ("Alon, a reminder: call mum.") into their text, to be said twice too.
5. **OpenAI doesn't know where home is.** Asked about the weather, it guesses Israel from nothing, or asks for "your
   city or ZIP code" (a third of weather answers). TARS has no home-location setting to give its web search.

6. **OpenAI sometimes sends two different links and then says it couldn't.** Asked for a link to a pancake recipe,
   it now and then sends two (both tool calls answered "sent") and says "I can't send it to your TARS page right now"
   (0 to 4 times in 10 runs, varying from one run to the next).

The other two failures are judgment calls the scenarios hold TARS to: with two timers running, "cancel the timer"
cancelled one without asking (1 of 3), and a message for when Stacey's back was set as a reminder that waits for her
instead of a message (1 of 3; it's said the same way).

## Fixed since

Each fix is its own pull request, measured with these scenarios before and after on the same day.

| Problem | Fix | Before | After |
|---|---|---|---|
| 4: answering a reminder lost it, or set a second one, or copied TARS's words | the change wins over an `<ack>`; "again later" puts that one off; the `remind` tool strips TARS's words | reminder scenarios 54/66 | 65/66 |
| 2: something done twice | the tools answer "already set" or "already sent" to a repeat in the same reply, or of one that's set | 4 doubles in 70 runs | 0 |
| 5: OpenAI didn't know where home is | `[llm] home_city` and `home_country`, told to both models and the web search | 15 of 24 weather answers asked where | 0 of 24 |
| 3: a tool call written as text, read aloud | the reply is cut off at the first sign of one, unsaid, and OpenAI takes the turn | rare: 1 in the baseline's 87 runs | unit tests; none said since |
| 1: two-part requests fell to OpenAI | the rules say what to do with one: its own part first, with its tools, then the marker | two-part scenarios 4/18, 15 turns fell to OpenAI | 16/18, 3 turns |

| Every scenario that wasn't 100% | escaped markers recognized, no repeats within or across a hand-off, clearer send, link, message and "which one?" rules, Groq's capped replies written on Cerebras when nothing was said yet, and three checks that failed fine answers made fair | 15 of 29 scenarios below 100% | 1 |

With all of them, 5 runs of each of the 29 scenarios: **143/145** (95% interval 95-100%), for $1.53 at
`evals/prices.toml`'s prices: $0.89 on OpenAI (78 web searches are $0.78 of it) and $0.65 for Qwen on Cerebras.
The run's line in `results.jsonl` says `cost_usd` 0.886, OpenAI's part alone: Qwen's price wasn't in `prices.toml`
yet when it ran, so its tokens counted as nothing. The one scenario left, a reminder and a hard question in one
request, is 3/5: Qwen starts working the question out itself after setting the reminder, instead of handing it over.

## Limitations

- **No judge yet.** Fixed rules check what was done and what mustn't be said, not whether an answer is right or sounds
  like TARS. That's the next step: Claude as a judge, checked against the owner's own labels first.
- **Cerebras, not Groq.** TARS asks Groq first. Both list the same model, but neither says how it's served, so a slow
  run on Groq's free key, a few turns a minute over several days, should confirm the two answer alike.
- **Three runs each** tell a problem that happens most of the time from one that doesn't, not 90% from 95%: a
  scenario is only called worse than the baseline when it drops by half its runs (`evals/report.py`).
- **The web answers are real**, so a scenario's result can depend on the day's news and weather.
