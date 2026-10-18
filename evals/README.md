# Scenario evals

Whole conversations with TARS's brain, played turn by turn the way the assistant plays them, with every turn checked.
`tools/capability_bench.py` asks one request at a time and stops at a hand-off. These scenarios follow the hand-off
through to OpenAI's model or the thinking model, and keep going: the next turn, a correction, a follow-up heard
without the wake word, a failure partway through. The results and what they found: [docs/evals.md](../docs/evals.md).

```bash
uv run python -m evals                                   # every scenario, 3 runs each (about a minute)
uv run python -m evals --tag hand-off --runs 5           # one kind
uv run python -m evals --scenario cancel-that --show     # one scenario, every turn printed
uv run python -m evals --list                            # what there is
uv run python -m evals --baseline evals/results.jsonl    # what got better or worse since the saved baseline
uv run python -m evals --baseline evals/results.jsonl --no-regressions --fail-under 0.85   # as a check
uv run python -m evals --save evals/results.jsonl        # keep the totals as the new baseline
```

## On a pull request

Evals are never a check on a pull request: a model's answers vary, and every run costs money, so nothing waits on
them. They run when asked, from a comment on the pull request (`.github/workflows/evals.yml`), and the answer is a
comment: runs right, against the baseline, what went wrong, and what it cost.

```text
#evals                                   every scenario, 3 runs each
#evals runs=5 tag=hand-off               one kind, more runs (1-10)
#evals scenario=cancel-that,got-it       just these
#evals budget=3                          a higher cap (default $2, at most $5)
#evals estimate                          only what it would cost: nothing is run
```

Only the owner and collaborators can ask, and only on pull requests from this repository: a fork's code never runs
with the keys (the repository secrets `OPENAI_API_KEY` and `CEREBRAS_API_KEY`). Every run has a budget, OpenAI is on
its standard tier, and the transcripts are the run's `evals` artifact. `evals/usage.json`, which `--save` keeps up to
date, is what the estimate goes on there, with no transcripts of earlier runs.

## What a run costs

Every run starts with an estimate, from what the same scenarios used in earlier runs and the prices in
`evals/prices.toml` (check them against the providers' pages: they change). **Web searches are most of it**: OpenAI
bills $10 per 1,000 on any tier, besides their results' tokens, and a full run makes about 45. A full run (29
scenarios, 3 runs each) comes to about $0.90: about $0.50 on OpenAI, most of it searches, and about $0.40 for Qwen on
Cerebras ($0.99 per million tokens in, $1.49 out; its instructions are long, so it's mostly input).

```bash
uv run python -m evals --estimate                 # what it would cost, and nothing else
uv run python -m evals --tag hand-off --budget 1  # refuses to start if estimated over $1, and stops when it gets there
```

## What a run leaves behind

- **The report**: each scenario's runs right, and every turn that went wrong with what it said and did; then the
  totals with 95% Wilson intervals, by tag, first words by where the turn went, tokens and web searches by model,
  and what it cost against the estimate.
- **Every run's transcript**, in `evals/runs/<time>/runs.jsonl` (not committed): for each turn, the prompt, the reply
  as written and as heard, where it went, what it changed and sent, the times and what was wrong; and
  `summary.json`. That's what to read when a scenario fails, and what a judge model grades.
- **With `--save`, a line in `results.jsonl`**, the baseline: the totals, each scenario's result with a fingerprint
  of its definition, a fingerprint of the brain (its settings, prompt included, the quick model, and the code its
  rules and tools are in), the commit, and whether `src`, `config.toml` or the scenarios had uncommitted changes.
- **With `--baseline`, a comparison**: a scenario is *worse* when its share of runs right drops by half or more
  (with 3 runs, 3/3 to 1/3), *better* the other way, and *changed* when its definition changed, since then the two
  aren't comparable. One run either way is what the same code does from run to run.
- **An exit status for checks**: 0 if it passes; 1 if `--fail-under` (the share of runs right) or `--no-regressions`
  (nothing worse than the baseline) fails it; 3 if it was over `--budget` (estimated over it, so not started, or
  stopped when it got there); 2 if the result is incomplete: some runs were still rate-limited after
  five pauses, or a service refused the account (out of credits, a bad key, no access). A refusal stops the eval
  early, since every run after it would fail the same way, and it's never scored as TARS getting a turn wrong.

**What it runs:** TARS's real `QuickChat`, set up from `config.toml` as TARS sets it up (the model, reasoning effort,
tools and prompts). Qwen runs on Cerebras (`CEREBRAS_API_KEY`), which serves the same model as Groq with room for
heavy runs. TARS asks Groq first, its replies capped at 500 tokens for its free key, then Cerebras, capped at 2,000:
both are played on Cerebras, each with its own cap, so a reply too long for Groq runs out here as it would at home. Groq's free key allows only a few turns a minute; `--brain groq` works, but slowly. Hand-offs really go
to OpenAI (`OPENAI_API_KEY`), on its standard tier: `config.toml`'s `service_tier = "fast"` (priority processing)
changes how soon it answers and what it costs, not what it answers, so it's only used with `--fast`, when the times
matter. First words to OpenAI's answers are slower than at home without it. Reminders are kept in a database made for each run, so nothing touches the household's.
A run that hits a real "too many requests" is run again after a pause; it is never counted as a result.

**How a turn is played**, as `assistant.py` plays it: the prompt is tagged (`[Speaker: alon]`, and
`[Follow-up, no wake word]`, `[Reply to your 'Did you call me?']` or `[Reply to the reminder you just said]`). A
`<skip>` is forgotten and changes nothing. An `<ack>` marks the reminder acknowledged, before the turn's other changes,
as the assistant orders them. What's checked as said is what the voice would say: cited sources, links' addresses and
markdown symbols are taken out first, as `speech.clean_for_speech` does.

## Writing a scenario

Scenarios live in `evals/scenarios/*.toml`, grouped by file. Every setting is checked when the file loads, and a
mistake (an unknown setting, a route that doesn't exist, cancelling a reminder no earlier turn set) is an error, which
`tests/test_evals.py` catches in CI.

```toml
[[scenario]]
id = "cancel-that"                      # unique; --scenario picks it
about = """What it tests, and why it matters."""
tags = ["reminders", "corrections"]     # --tag picks them; the report counts by tag

[[scenario.seed]]                       # optional: a reminder set before the conversation, from the web page
ref = "pasta"                           # its name, for a later cancel or snooze
kind = "timer"                          # timer | reminder | message
text = "pasta"
in_minutes = 10
# for = "alon"                          # who it's for
# said = true                           # TARS just said it and is waiting for "got it"

[[scenario.turn]]
say = "set a pasta timer for eight minutes"
reminders = [{ kind = "timer", in_minutes = 8, ref = "pasta" }]
# house = { lock = "locked", door = "closed", boiler = "off", ac = "off", tv = "on" }   # optional: TARS's
#   real home tool over a pretend house (evals/house.py); jams = true or down = true for a lock that jams, or Home
#   Assistant not answering

[[scenario.turn]]
say = "cancel that"
reminders = [{ action = "cancel", ref = "pasta" }]
active = []
```

A turn's settings. Everything but `say` is optional, and anything left out isn't checked.

| Setting | What it does |
|---|---|
| `say` | what's said |
| `speaker` | who says it: `alon` (the default), `stacey`, or `unknown` (a voice TARS doesn't know) |
| `tag` | how it reaches the brain: `follow-up`, `asked` or `reminder` (see above) |
| `route` | where it should go, or a list of places: `quick` (Qwen answers), `look-up` (handed to OpenAI), `ponder` (the thinking model), `fallback` (OpenAI, because Qwen couldn't), `skip`, `ack` |
| `reminders` | exactly the changes it should make, in any order; `[]` = none. An entry: `action` (`add`, the default, `cancel` or `snooze`), `kind`, the time (`in_minutes`, or `time` "HH:MM" with an optional `day`, or `when_back = true`), `for`, `text` (has to appear in what TARS will say), `ref` (to name an added one), `minutes` (a snooze's) |
| `active` | exactly the reminders that should be set after the turn, entries as above |
| `active_has` | reminders that should be among those set after it |
| `sent` | exactly what it should send to the TARS page: `{ kind = "list", entries = 3 }`; `kind` can be a list of kinds |
| `home` | exactly the changes it should make in the house, in any order; `[]` = none: `{ device = "boiler", action = "on", minutes = 40 }`, with any of `minutes`, `temperature`, `mode`, `fan` checked if given. Needs the scenario's `house` |
| `says`, `never_says` | regular expressions (any case) that have to appear, or mustn't, in what's said |
| `quick` | make Qwen's service fail on this turn: `down` or `limited` ("too many requests"). It then sits out its cooldown, as in TARS |
| `after_minutes` | quiet before the turn; past `[llm] memory_minutes`, TARS starts fresh |

**Every turn is also checked for what TARS should never say aloud:** a tag (`<look-up>`, `<reminder ...>`), a list
written as lines, a web address, an emoji, the same sentence twice (as when a hand-off repeats what Qwen already
said), or nothing at all.

**Write scenarios that a person would agree with.** If a reply that's fine fails, the scenario is wrong, not the
model: say the request less ambiguously, or accept both answers (`route = ["quick", "look-up"]`). Times that depend on
when the eval runs ("at 6pm" after 6 pm) belong on `tomorrow` or a weekday.

## What's next

- A judge model (Claude) for what fixed checks can't score: whether a reply sounds like TARS, is right, and works by
  ear, checked against labels the owner gives first.
- Each model on its own: OpenAI's model on web answers and on carrying on from what Qwen said, and the thinking model
  across reasoning efforts on questions that need it.
- The large model, effort and prompt grid, run by hand, now and then.
- Turning a real turn that went wrong (from the event log) into a scenario.
