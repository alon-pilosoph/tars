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
| `gpt-oss-120b` | cerebras (paid) | low | 60/60 | 18/18 | 24/24 | 12/18 | 36/36 | **150/156** | 0.30 / 0.44 | 0.67 / 1.09 | 0.30 / 0.60 | 2026-10-08 |
| `gpt-oss-120b` | cerebras (paid) | medium | 60/60 | 18/18 | 24/24 | 12/18 | 36/36 | **150/156** | 0.37 / 0.57 | 0.73 / 1.03 | 0.34 / 0.45 | 2026-10-08 |
| `qwen-3.8-27b` | cerebras (free tier) | none | 36/60 | 10/18 | 22/24 | 11/18 | 36/36 | **115/156** | 0.34 / 0.57 | 0.68 / 1.04 | 0.40 / 0.70 | 2026-10-08 |
| `qwen-3.8-27b` | cerebras (paid) | none | 30/60 | 7/18 | 24/24 | 11/18 | 36/36 | **108/156** | 0.34 / 0.68 | 0.80 / 1.48 | 0.35 / 0.45 | 2026-10-08 |
| `qwen-3.8-27b` | cerebras (free tier) | low | 59/60 | 18/18 | 24/24 | 12/18 | 36/36 | **149/156** | 0.40 / 0.74 | 0.99 / 1.51 | 0.42 / 0.71 | 2026-10-08 |
| `qwen-3.8-27b` | cerebras (paid) | low | 59/60 | 18/18 | 24/24 | 12/18 | 36/36 | **149/156** | 0.42 / 1.35 | 1.05 / 1.70 | 0.41 / 0.80 | 2026-10-08 |
| `qwen-3.8-27b` | cerebras (free tier) | medium | 58/60 | 18/18 | 24/24 | 12/18 | 36/36 | **148/156** | 0.39 / 0.89 | 1.17 / 1.84 | 0.53 / 0.85 | 2026-10-08 |
| `qwen-3.8-27b` | cerebras (paid) | medium | 60/60 | 18/18 | 24/24 | 12/18 | 36/36 | **150/156** | 0.43 / 0.87 | 1.12 / 1.67 | 0.41 / 0.77 | 2026-10-08 |
| `claude-haiku-5-5` | claude | low | 60/60 | 18/18 | 24/24 | 12/18 | 36/36 | **150/156** | 0.68 / 1.79 | 2.32 / 4.73 | 0.79 / 1.42 | 2026-10-08 |
| `claude-haiku-5-5` | claude | medium | 60/60 | 18/18 | 24/24 | 12/18 | 36/36 | **150/156** | 0.75 / 2.26 | 2.58 / 4.07 | 0.67 / 1.40 | 2026-10-08 |
| `gpt-6-luna` | openai (fast) | none | 60/60 | 18/18 | 24/24 | 9/18 | 36/36 | **147/156** | 0.68 / 0.94 | 1.66 / 2.41 | 0.74 / 1.69 | 2026-10-08 |
| `gpt-6-luna` | responses (fast) | none | 60/60 | 18/18 | 24/24 | 13/18 | 36/36 | **151/156** | 0.72 / 1.11 | 1.79 / 2.57 | 0.67 / 0.82 | 2026-10-08 |
| `gpt-6-luna` | responses (fast) | low | 60/60 | 18/18 | 24/24 | 14/18 | 36/36 | **152/156** | 0.58 / 1.18 | 1.88 / 2.80 | 0.74 / 0.86 | 2026-10-08 |
| `gpt-6-luna` | responses (fast) | medium | 60/60 | 18/18 | 24/24 | 13/18 | 36/36 | **151/156** | 0.80 / 1.49 | 1.76 / 2.15 | 0.84 / 1.32 | 2026-10-08 |

**Reading it.** Scores are right answers out of runs (each request six times). *Itself* is answering without a tool
or a hand-off, including saying plainly that TARS can't play music or turn off lights. Times are the median / the 90th
percentile, in seconds, from the request to the first words TARS would speak (the voice adds about 0.4 s, see
[response time](latency.md)); *hand-off decided* is how long the quick model takes to reply `<look-up>` or `<ponder>`,
before the stronger model even starts. *Where* is the service and the API (`openai` is Chat Completions, `responses`
OpenAI's Responses API), with the tier: the one responses said they were served on (OpenAI's `fast`), or the
account's (Cerebras's free tier, then paid).

**What TARS uses: Qwen on Cerebras at low reasoning** (`cerebras_model = "qwen-3.8-27b"`,
`quick_reasoning_effort = "low"`, `quick_tools = true`). Within a request or two of the best on the score, among the
fastest, and the only fast one that sounds like TARS. The bench scores what a model does, not how it sounds, so the
fast candidates were also asked the same everyday questions:

| Asked | Qwen, low | gpt-oss-120b, low |
|---|---|---|
| tell me something interesting about Saturn | "Saturn's average density is less than water. It would float in a bathtub, if you had the bathtub." | "Saturn's rings are made mostly of ice particles ... and they could fit inside Earth. ... `<skip>`" |
| I burned the toast again | "Consistency is a virtue, Alon. The toast at least has a reliable schedule." | "Try lowering the heat and watching it closely." |
| how are you today? | "Functioning within expected parameters. Slightly less interesting than yesterday, but that's a constant." | "I'm operational. Nothing particularly interesting today.`<skip>`" |

- **Qwen without reasoning is fast and untrustworthy.** A third to half of reminders go wrong, and the worst way:
  "Pasta timer is set for 10:54." with no timer set. It also writes tool calls and tags as text
  (`<reminder id=2 ...>`, `<retry/>`), which TARS would say aloud. Low reasoning fixes both for about 0.06 s.
  Medium is about as right, and slower on reminders.
- **Paying Cerebras didn't make Qwen faster**; it lifts the free tier's daily allowance, past which Qwen answers
  "402" and OpenAI takes over (slower, but it answers) until the next day.
- **gpt-oss-120b is the fastest** (first words in 0.30 s, a reminder in 0.67 s) and scores 150, but it's plain where
  Qwen is TARS, it adds `<skip>` to the end of ordinary answers (TARS only looks for one at the start, so it would say
  it), it once repeated its answer twice over, says "$" for every amount, and writes numbers with narrow spaces
  ("44 000"). Measured on the paid tier while Cerebras warned of high traffic on it.
- **Luna through the Responses API is the most accurate** (152 at low, and the only model that sometimes hands the
  savings question to `<ponder>`), but its first words come 0.2-0.4 s later than Qwen's, and a reminder's 0.8 s later.
  Through Chat Completions it can't use tools with any reasoning, and without reasoning it was too keen on its tools
  (a reminder nobody asked for, a link from memory in a note) before quick models were refused links.
- **Haiku 5.5 scores 150 at low and medium**, with every miss the savings question, but its first words come about
  0.3 s later than Qwen's, a reminder's 1.3 s later, and its slow tail is the longest.
- **The savings question** ("300 a month at 4 percent for ten years") nearly always gets worked out on the spot,
  correctly (about 44,200), rather than handed to `<ponder>`. That's fine for TARS; it's probably too easy to test the
  hand-off, and a harder one should replace it.
- Qwen's one wrong reminder on low set "November 3rd at 10" for 9:50, ten minutes early. TARS confirms with the time
  it's set for, so it's heard.
- The first Qwen none and medium rows, Haiku's, and Luna's Chat Completions row were measured before quick models were
  told not to put links in what they send (and refused when they do).

## Routers

A router decides where a request goes before anything answers it, so the turn could go straight to the right model.
Tried: **Jev** (`jev-1.13.0`, [TypeSafe](https://docs.typesafe.ai/api)), a decision model: it doesn't write text,
it answers a typed question with a probability for each option. Asked which of six ways each of the same 26 requests
should go (answer, reminders, send, web, ponder, can't), 6 times each, one at a time on a kept-open connection
(`tools/router_bench.py`, 2026-10-08):

| Router | Reminders | Sending | Web | Hard questions | Itself | All | Decided in (s) | Confidence when right / wrong |
|---|---|---|---|---|---|---|---|---|
| `jev-1.13.0` | 60/60 | 12/18 | 24/24 | 12/18 | 36/36 | **144/156** | 0.29 / 0.35 | 0.90 or more / 0.88 or less |

- **Every request got the same pick all six times**, and its two misses are arguable: "send me a recipe for
  pancakes" went to *answer* (a recipe can be said), and the savings question to *answer*, as with every model above.
- **Its confidence tells right from wrong**: never under 0.90 when right, never over 0.88 when wrong. Acting only on
  picks of 0.9 or more, 36 of the 42 web and hard-question requests would have gone straight on, and none wrongly.
- **Jev itself is fast; the distance isn't.** Of its 0.29 s, about 0.22 s is the round trip to TypeSafe's servers
  (an empty request on the same kept-open connection takes that long; requests leave through Cloudflare in Tel Aviv),
  and about 0.065 s is Jev. For comparison, the same empty round trip takes 0.16 s to Cerebras and 0.20 s to OpenAI,
  and Qwen takes about 0.26 s of its 0.42 s to decide on a hand-off. Measured from where TARS runs; closer to
  TypeSafe's servers, the picture changes.
- **In front of every turn, it would make TARS slower**, because a router in front adds a whole round trip before the
  quick model's own: its 0.29 s, then Qwen without reasoning (0.34 s to first words), is about 0.63 s, against 0.40 s
  for Qwen at low reasoning on its own.
- **Beside the quick model**, started at the same moment: a web or hard-question pick at 0.9 or more sends the turn on
  without waiting for the quick model's `<look-up>` or `<ponder>` (0.42 s median, 0.71-0.80 s at the 90th percentile,
  against Jev's 0.29 / 0.35 s), and anything else is left to the quick model. Those turns gain about 0.13 s at the
  median and 0.4 s in the slow tail, nothing gets slower, and it costs one Jev request a turn (TypeSafe doesn't
  publish prices). Not built: the gain is small next to what the quick model already does.
- **Better suited, not yet tried**: TARS's yes/no decisions, like whether an overheard follow-up was meant for it, or
  whether a reply acknowledges a reminder. Several questions in one Jev request take about as long as one.
- TypeSafe's own notes on where Jev is weak: arithmetic, dates and times, counting, and long inputs with irrelevant
  material in them. Routing needs none of these, as long as Jev is given the request alone and never asked for a time.

## Where the requests go from

Every request crosses from the house to the services' servers, and back. From TARS's home in Israel, through
Cloudflare in Tel Aviv, an empty request on a kept-open connection takes 0.16 s to Cerebras, 0.20 s to OpenAI and
0.22 s to TypeSafe; from a cloud machine in Ashburn, Virginia (US East), 0.06, 0.08 and 0.11 s, and Jev's whole
decision 0.18 s instead of 0.30 s (2026-10-08). So being next to the servers saves about 0.1 s a request.

**A relay there doesn't pay.** The idea: run TARS's brain on a server near the services, so the house makes one
crossing a turn and the chained requests happen next door. But the house still crosses to the relay (about 0.15-0.2
s), and TARS's chains are two requests long at most (a reminder's tool call and its confirmation; Qwen's `<look-up>`
and OpenAI's answer). That comes out about even for those turns, and slower for answers Qwen gives itself, which
are one request now. It pays from three requests in a chain, like a router in front (Jev, then the quick model), and
even then: 0.16 + 0.18 + 0.24 ≈ 0.58 s, against Qwen at low on its own, 0.40 s. Whole turns from the cloud machine
came out slower than from home (0.68 s to first words against 0.50 s), most likely the cloud session's own network
proxy, which every request goes through there; the round trips above are the part to trust.

**Web questions: what's slow is the search.** Followed through to OpenAI (`capability_bench --follow`), a web
question's first words came 4.3-4.5 s after the request, from home and from the US alike: Qwen hands over in about
0.4 s, OpenAI starts searching about a second later, and the search takes 1-3 s more. TARS says "Looking it up."
the moment a search starts, but didn't when Qwen's hand-off began with a blank line, which it often does; fixed, the
first words come after 1.5 s (median), and the answer itself after 4.3 s. Also seen: asked for tomorrow's weather,
OpenAI once asked which city, taking the house to be in the United States.

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
- Cerebras's free tier allows Qwen about 450 requests a minute but few tokens a minute, and gpt-oss-120b 5 requests a
  minute and a small daily amount (it ran out ten runs in); the rows marked paid came after a top-up. OpenAI's Luna
  runs in fast mode, as TARS uses it. A full run costs cents on Cerebras and Haiku, and under a dollar on Luna.

## Adding a model

```
uv run python tools/capability_bench.py --brain cerebras --model <id> --effort low --save docs/models.jsonl
uv run python tools/capability_bench.py --brain responses --model <id> --effort low --save docs/models.jsonl
uv run python tools/capability_bench.py --brain claude --model <id> --effort low --save docs/models.jsonl
uv run python tools/capability_bench.py --table docs/models.jsonl      # the table above, from every saved run
uv run python tools/router_bench.py                                     # Jev as a router (TYPESAFE_API_KEY)
```

`--brain` is where it runs: `cerebras` and `openai` through the Chat Completions API (any compatible service works the
same way), `responses` through OpenAI's Responses API (`ResponsesQuickChat`: OpenAI's models only take tools with
reasoning there), `claude` through Anthropic's Messages API (`ClaudeQuickChat`, needs `uv sync --extra claude`). Keys come
from `.env`: `CEREBRAS_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`. Try each reasoning effort the model has,
lowest first, and paste the new rows in. Before switching TARS to a new model, read its wrong answers in the run's
output too, not only its score: what it gets wrong matters as much as how often.
