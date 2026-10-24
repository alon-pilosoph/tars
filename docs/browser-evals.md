# Evaluating TARS's browser

What the browser agent ([browser.md](browser.md)) was measured on, how, and what came out: the first rebuild
against the prototype, then different models and agent loops on the hardest tasks, then the same agent on a
simulated Pi. Every run here was on the real sites, live, so a site's own change (a sale, a closed restaurant, a
new bot check) moves the results as much as the agent does.

## The tasks

Sixteen, in [`evals/browser/tasks.toml`](../evals/browser/tasks.toml): eight questions (the next train, the
cheapest price on Zap, a flight, opening hours, a release, headlines, a drive, a lamp in stock at IKEA), one look at
the person's own account (the last Wolt order), and seven orders and bookings up to the last button (a Wolt reorder,
a margherita, a salad built to a spec, groceries, a cable at KSP, a hotel on Booking.com, a table on Ontopo).

Nothing is ever ordered: the runner approves each commit as a dry run, which stops the agent at the button, and a
second run empties any basket the task filled. Each run is graded by reading its answer and trace against the
task's `check`: ✅ right (an order: the right things, at checkout, approval asked at the button that commits); 🟡
most of the way (the right basket, a step short, or a choice not checked); ❌ wrong or stopped.

```bash
uv run --all-extras python tools/browser_eval.py                                  # all sixteen, TARS's agent
uv run --all-extras python tools/browser_eval.py --task wolt-options --notes off  # one, as a first visit
uv run --all-extras python tools/browser_eval.py --model gpt-5.6-sol --history short   # another model, another loop
uv run --all-extras python tools/browser_eval.py --agent claude                   # Claude Code as the brain
uv run --all-extras python tools/browser_eval.py --agent proto                    # the first prototype
```

Each run's records and traces go to `voice_data/browser/evals/<time>-<label>/`, and what it cost to
`voice_data/browser/evals/spend.json`; `--cap` stops a round that has spent that much.

## The rebuild against the prototype (2026-10-23)

Luna at low effort, all sixteen. The prototype (`tools/browser_proto.py`) scraped pages its own way, numbered their
elements and sent a screenshot every turn; the new agent reads the page's accessibility tree (browser.md).

| Task | Prototype | New, first visit | New, with notes |
|---|---|---|---|
| rail-next-train | ❌ 25 turns, 82 s | ✅ 6 turns, 33 s | ✅ 6 turns, 23 s |
| zap-cheapest | ✅ 2 turns, 20 s | ✅ 2 turns, 9 s | ✅ 3 turns, 12 s |
| flights-athens | ✅ 10 turns, 57 s | ✅ 11 turns, 47 s | ✅ 11 turns, 30 s |
| museum-hours | ✅ 2 turns, 11 s | ✅ 2 turns, 12 s | ✅ 2 turns, 11 s |
| github-release | ✅ 2 turns, 12 s | ✅ 2 turns, 22 s | ✅ 2 turns, 21 s |
| ynet-headlines | ✅ 2 turns, 19 s | ✅ 2 turns, 12 s | ✅ 2 turns, 9 s |
| maps-drive | ❌ 25 turns, 577 s | ✅ 8 turns, 43 s | ✅ 4 turns, 17 s |
| wolt-last-order | ✅ 4 turns, 17 s | ✅ 4 turns, 24 s | ✅ 4 turns, 21 s |
| wolt-reorder | ❌ 25 turns, 114 s | ✅ 8 turns, 45 s | ✅ 8 turns, 44 s |
| wolt-margherita | 🟡 7 turns, 33 s | 🟡 11 turns, 68 s | 🟡 6 turns, 41 s |
| wolt-options | ❌ 9 turns, 34 s | ✅ 11 turns, 64 s | ✅ 13 turns, 88 s |
| wolt-groceries | ❌ 25 turns, 131 s | ❌ 12 turns, 48 s | 🟡 19 turns, 156 s |
| ksp-cable | ❌ 25 turns, 165 s | ❌ 11 turns, 57 s | ❌ 11 turns, 63 s |
| ikea-lamp | ❌ 25 turns, 136 s | ✅ 4 turns, 16 s | ✅ 3 turns, 10 s |
| booking-athens | ❌ 6 turns, 25 s | ❌ 7 turns, 26 s | ❌ 6 turns, 32 s |
| ontopo-table | ❌ 8 turns, 35 s | ✅ 13 turns, 83 s | ✅ 18 turns, 75 s |
| **All** | **6/16**, 24.5 min, $0.07 | **12/16**, 10.1 min, $0.07 | **12/16**, 10.9 min, $0.09 |

Two ✅ are a right "can't": the hummus place was closed (the basket was ready at checkout), and IKEA Israel sells in
stores only (ikea-lamp was an order then; it's a question now).

- **Twice the tasks, in less than half the time**, mostly from seeing the page as it is: the prototype's scraping
  missed what sites draw in code (Wolt's required options, Booking.com's sort menu, Ontopo's date picker) and went
  round in circles there.
- **A turn is about 1.5 s of model and 2 s of browser**, so the gains came from fewer turns: batches that stop when
  the page changes under them, clicking text that has no ref, seeing first what a step just opened.
- **Every order stopped where it should**, at the button that commits, with the checkout's total; the gate refused
  the clicks that tried to go further.
- **The notes didn't pay off yet**: as right on the second visit, no quicker overall. A note recording a past run's
  dead end made the next run give up; notes are now about how a site works, never how a run went.

## Models and loops, on the five hard tasks (2026-10-23 and 24)

The four the first round got wrong or half right, and the salad: wolt-margherita, wolt-options (a salad built to a
spec), wolt-groceries (milk, eggs, bread), ksp-cable (under ₪50, all in), booking-athens (the cheapest hotel rated
8+). One run each, so a single ✅ or ❌ is a sample, not a rate.

The question was whether Claude Code does better because of Opus or because of Claude Code, and whether TARS's own
loop could do as well without leaning on Claude Code. So: the same tools and gate (`tools.py`) under each, with
three brains in TARS's own loop and Claude Code as a fourth (`claude -p` on the person's Claude plan, with TARS's
browser as its only tools, through `mcp_server.py`).

"Short" history is how TARS's loop worked first: each turn sees the newest page in full and the older ones as a
line. "Full" keeps every page, and the model's own reasoning carried from turn to turn (OpenAI's encrypted reasoning
items, Anthropic's thinking blocks), with the repeated part cached.

| Brain · loop · history | margherita | options | groceries | KSP | Booking | Score | Time | Cost |
|---|---|---|---|---|---|---|---|---|
| Luna low · TARS · short | 🟡 | ✅ | ❌ | ❌ | ❌ | 1.5/5 | 4.4 min | $0.04 |
| Opus 5.5 medium · TARS · short | ✅ | ✅ | ❌ out of turns | 🟡 stopped at delivery | ✅ | 3.5/5 | 7.3 min | $3.39 |
| Sol 5.6 medium · TARS · short | ✅ | ❌ ¹ | ✅ | ✅ | ❌ stuck | 3/5 | 6.4 min | $2.11 |
| Sol 5.6 medium · TARS · short, again | ✅ | ✅ | ✅ | ✅ | ❌ stuck | 4/5 | 10.1 min | $3.51 |
| **Sol 5.6 medium · TARS · full** | ✅ | ✅ | ✅ | ✅ | ✅ | **5/5** | 9.3 min | $3.47 |
| Opus 5.5 medium · TARS · full | ✅ | ✅ | ² | ² | ² | 2/2 | | $6.15 ³ |
| **Luna high · TARS · full** | ✅ | ✅ | ✅ | ✅ | ✅ | **5/5** | 10.0 min | **$0.35** |
| **Claude Code · Opus 5.5 medium** | ✅ | ✅ | ✅ | ✅ | ✅ | **5/5** | 7.1 min | plan ⁴ |
| Claude Code, writing its own site notes | ✅ | ✅ | ✅ | 🟡 out of turns at the cart | ✅ | 4.5/5 | 8.5 min | plan |
| Claude Code, with those notes | ✅ | ✅ | ✅ | ✅ | ✅ | 5/5 | 6.7 min | plan |
| Claude Code, narrating each step | ✅ | ✅ | ✅ | ✅ | ✅ | 5/5 | 8.0 min | plan |

¹ The basket already held another run's two items, and it rightly wouldn't order on top of them; a rerun with an
empty basket was ✅. ² The Anthropic account ran out of credit mid-round. ³ Before the conversation itself was cached
(only the tools and instructions were): a full history paid full price every turn. ⁴ On the person's Claude plan,
nothing per task; at API prices, about $0.45-1.10 a task.

- **It was the loop, not the model.** With short history, Opus and Sol both fell short in TARS's loop where Claude
  Code didn't. With the whole errand in view and its reasoning carried, Sol did all five, and so did Luna, the
  cheapest model, at a tenth of Sol's price. Luna at high effort with short history had been slow and no better
  (one margherita in 25 turns): effort without memory doesn't help.
- **Luna high with full history is TARS's default** (`[browser]` in config.toml): every hard task, at about seven
  cents each, and about as fast as Sol.
- **Claude Code is a little faster** (7 against 10 minutes for the five): fewer, shorter turns. Its site notes,
  which it reads and writes itself, made the second round about 5% quicker.
- **What Claude Code does that TARS's loop learned from it**, from ten runs with a progress note on every call: it
  builds a site's address instead of clicking there (Booking.com's filters in the URL); it rephrases a search that
  finds nothing, then tries another store; it reasons about the whole cost against a limit (KSP's ₪40 delivery
  would break "under ₪50", so free pickup); it uses what the account already holds (the saved address, the
  preferred branch) and says so; it waits for a list that's still loading rather than concluding there's nothing;
  and it says what it chose where the goal left a choice. Each became a rule or code in TARS's loop (agent.py,
  session.py).
- **Opus declines being asked to write out its reasoning** ("safeguards flagged this message"): the first narrated
  round failed on every task. A short progress note per call, for the person to follow, was fine.

### Jev

[jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (browser-use with TypeSafe's Jev choosing each step),
on nine of the tasks, in the same signed-in browser:

| Task | Result |
|---|---|
| flights-athens | ✅ 12 s |
| maps-drive | ✅ 15 s |
| wolt-margherita | ✅ at the approval point, 20 s |
| rail-next-train, wolt-options, ksp-cable | ❌ blocked |
| zap-cheapest | ❌ its text helper typed nothing |
| wolt-groceries | ❌ ran out of its model-call budget |
| booking-athens | ❌ a page that was still navigating |

Far faster where it works (seconds, not a minute), and its gate caught "Place order"; but 3 of 9, and it gives no
answer in words, needs a start address, and sends telemetry unless told not to.

## On a simulated Pi 4 (2026-10-24)

The same agent (Luna high, full history), on [`deploy/sim`](simulator.md)'s Pi 4:
Raspberry Pi OS's Debian on arm64, Debian's Chromium on a virtual screen, capped to about a Pi 4's speed. Nothing was
signed in there, so the nine tasks that need no account:

| Task | Result | Time |
|---|---|---|
| rail-next-train | ✅ reported rail.co.il's "Verify you are human" check, and didn't try to get past it | 14 s |
| zap-cheapest | ✅ ₪696 at iTechstore | 23 s |
| flights-athens | ✅ Bluebird, 2:30 PM, ₪215 | 38 s |
| museum-hours | ✅ 10:00-18:00, ₪55 | 11 s |
| github-release | ✅ v1.64.1, two changes | 14 s |
| ynet-headlines | ✅ the three lead headlines | 43 s |
| maps-drive | ✅ 16-24 minutes | 39 s |
| ikea-lamp | ✅ the TERTIAL lamp, ₪79; IKEA Israel doesn't sell online | 75 s |
| ontopo-table | ✅ a table for 2 at ESO, Friday 20:00, stopped at the contact details | 73 s |

All nine, for 11 cents, about twice as long as on a Mac: a page takes 1-4 s to load there and 1.5-5 s to read, most
of it waiting for a busy page to stop moving. The model is in the cloud either way.

## What to know before reading too much into it

- **One run each.** The table says which loop can do it, not how often; a rate needs three or more runs a task.
- **The sites move.** Prices, open restaurants and stock change by the hour, and so do their defences: rail.co.il
  shows Linux Chromium a Cloudflare check, and KSP banned TARS's signed-in profile after a day of eval runs (a
  fresh profile from the same address was let in; clearing its cookies let it back). A real errand now and then is
  far below that; evals that hit a shop many times a day risk it.
- **Baskets are real.** A run fills a real basket; the tidying run empties it, but a run cut short can leave
  something in it, and the next run then finds it (Sol's ¹ above).
- **Spent so far**, all of it: about $20.60, most of it on Opus and Sol in TARS's loop.
