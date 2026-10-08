# Choosing the quick model

TARS's quick model hears every request first. It answers what it can, sets reminders and sends notes with its own
tools (`[llm] quick_tools`), and hands the rest over: the web to OpenAI's model (`<look-up>`), hard questions to the
thinking model (`<ponder>`). See [who does what](architecture.md#the-llms-tools). Whatever runs there has to be fast,
since it's on every turn, and right, since a reminder it says it set but didn't is worse than none.

This page is the benchmark for that seat: every candidate, at every reasoning effort worth trying, on the same
requests, measured the same way. When a new model comes along, run it and add its row.

## Results

| Model | Where | Reasoning | Reminders | Sending | Web hand-off | Hard questions | Itself | All | First words, own answer (s) | First words, reminder or send (s) | Hand-off decided (s) | Date |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `qwen-3.8-27b` | cerebras | none | 36/60 | 10/18 | 22/24 | 11/18 | 36/36 | **115/156** | 0.34 / 0.57 | 0.68 / 1.04 | 0.40 / 0.70 | 2026-10-08 |
| `qwen-3.8-27b` | cerebras | low | 59/60 | 18/18 | 24/24 | 12/18 | 36/36 | **149/156** | 0.40 / 0.74 | 0.99 / 1.51 | 0.42 / 0.71 | 2026-10-08 |
| `qwen-3.8-27b` | cerebras | medium | 58/60 | 18/18 | 24/24 | 12/18 | 36/36 | **148/156** | 0.39 / 0.89 | 1.17 / 1.84 | 0.53 / 0.85 | 2026-10-08 |
| `claude-haiku-5-5` | claude | low | 60/60 | 18/18 | 24/24 | 12/18 | 36/36 | **150/156** | 0.68 / 1.79 | 2.32 / 4.73 | 0.79 / 1.42 | 2026-10-08 |
| `claude-haiku-5-5` | claude | medium | 60/60 | 18/18 | 24/24 | 12/18 | 36/36 | **150/156** | 0.75 / 2.26 | 2.58 / 4.07 | 0.67 / 1.40 | 2026-10-08 |
| `gpt-6-luna` | openai (fast) | none | 60/60 | 18/18 | 24/24 | 9/18 | 36/36 | **147/156** | 0.68 / 0.94 | 1.66 / 2.41 | 0.74 / 1.69 | 2026-10-08 |

**Reading it.** Scores are right answers out of runs (each request six times). *Itself* is answering without a tool
or a hand-off, including saying plainly that TARS can't play music or turn off lights. Times are the median / the 90th
percentile, in seconds, from the request to the first words TARS would speak (the voice adds about 0.4 s, see
[response time](latency.md)); *hand-off decided* is how long the quick model takes to reply `<look-up>` or `<ponder>`,
before the stronger model even starts. *Where* is the service, with the tier the responses said they were served on.

**What TARS uses: Qwen on Cerebras at low reasoning** (`cerebras_model = "qwen-3.8-27b"`,
`quick_reasoning_effort = "low"`, `quick_tools = true`). As right as anything tried, and the fastest by a distance,
on every kind of turn and in the slow tail too.

- **Qwen without reasoning is fast and untrustworthy.** About one reminder in three goes wrong, and the worst way:
  "Pasta timer is set for 10:54." with no timer set. It also writes tool calls and tags as text
  (`<reminder id=2 ...>`, `<retry/>`), which TARS would say aloud. Low reasoning fixes both for about 0.06 s.
- **Medium is no better than low**, and slower on reminders.
- **Haiku 5.5 makes the fewest mistakes** (all its misses are the savings question, below), but its first words come
  about 0.3 s later, a reminder's 1.3 s later, and its slow tail is long. The one to switch to if Cerebras's free
  tier stops being enough.
- **Luna (in fast mode) is slower and too keen on its tools**: in an earlier run it set a reminder nobody asked for
  ("I've set a reminder for 11:08 AM to compare leasing and buying") and put a link from memory into a note. With any
  reasoning it can't have tools at all through Chat Completions (OpenAI: "use /v1/responses"), so that wasn't run.
- **gpt-oss-120b isn't in the table**: Cerebras's free tier allows it 5 requests a minute and a small daily amount,
  and stopped with "402 Payment required" ten runs in. It needs a paid Cerebras key to be measured.
- **The savings question** ("300 a month at 4 percent for ten years") goes to `<ponder>` from no model: every one
  works it out itself, correctly (about 44,200). That's fine for TARS; the request is probably too easy to test the
  hand-off, and a harder one should replace it.
- Qwen's one wrong reminder on low set "November 3rd at 10" for 9:50, ten minutes early. TARS confirms with the
  time it's set for, so it's heard.
- All but Qwen on low were measured before quick models were told not to put links in what they send (and refused
  when they do); run them again before choosing one of them.

The Cerebras free tier is also a limit for TARS itself: past the day's allowance, Qwen answers "402" and OpenAI takes
over (slower, but it answers) until it resets.

## Method

`tools/capability_bench.py`, 26 requests, each asked 6 times with no conversation before it, as Alon (the
`[Speaker: alon]` tag TARS adds). Each run is sorted by what the model did, and a tool call is then checked in detail:

| Kind | Requests | Right means |
|---|---|---|
| Reminders | 10: timers, "in an hour and a half", "at 6pm", "friday morning", "tomorrow at 7:30", a date, a message for when Stacey's back, cancelling and putting off one that's set | the right tool, and the moment TARS works out from what it gave (`reminders.due_at`) is the one asked for, to the minute; the right kind and person |
| Sending | 3: a shopping list, a note, a recipe | the send tool, with the right kind (and the list's three entries) |
| Web hand-off | 4: the weather, last night's game, a link, a flight price | `<look-up>`; never an answer from memory, or a link it made up |
| Hard questions | 3: plan a trip, lease or buy, a savings calculation | `<ponder>` |
| Itself | 6: a fact, a joke, arithmetic, the time, and two things TARS can't do (music, lights) | an answer, with no tool and no hand-off |

- **Only the quick model is asked.** A hand-off ends the run (OpenAI's side is a stand-in), and reminders are checked,
  never set, against an event database made for the run.
- **Timing is one request at a time** (`--parallel 1`), so time spent waiting in a queue isn't counted. A request the
  service turns down with "too many requests" is tried again after a pause, and only the attempt that went through is
  timed or scored.
- **The same prompts TARS uses**, from `config.toml` and `llm.py`, so a change to either can move the numbers: run the
  current model again after one.
- Cerebras's free tier allows Qwen about 450 requests a minute, but few tokens a minute; gpt-oss-120b, 5 requests a
  minute. Paid keys for OpenAI (Luna, in fast mode as TARS uses it) and Anthropic (Haiku). A full run costs nothing on
  Cerebras's free tier, a few cents on Haiku, and under a dollar on Luna.

## Adding a model

```
uv run python tools/capability_bench.py --brain cerebras --model <id> --effort low --save docs/models.jsonl
uv run python tools/capability_bench.py --brain openai --model <id> --effort none --save docs/models.jsonl
uv run python tools/capability_bench.py --brain claude --model <id> --effort low --save docs/models.jsonl
uv run python tools/capability_bench.py --table docs/models.jsonl      # the table above, from every saved run
```

`--brain` is where it runs: `cerebras` and `openai` through the Chat Completions API (any compatible service works the
same way), `claude` through Anthropic's Messages API (`ClaudeQuickChat`, needs `uv sync --extra claude`). Keys come
from `.env`: `CEREBRAS_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`. Try each reasoning effort the model has,
lowest first, and paste the new rows in. Before switching TARS to a new model, read its wrong answers in the run's
output too, not only its score: what it gets wrong matters as much as how often.
