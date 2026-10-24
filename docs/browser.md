# TARS's browser

TARS works a real web browser for the person: it reads a page, clicks, types and scrolls its way to what they asked
for, on any site, and comes back with the answer. Asked to order or book, it fills the basket up to the last button
and asks the person on their phone; only code presses that button, and only on a yes. Nothing in it knows any one
site: what it knows about a site, it learns by working it.

| Asked | What it does |
|---|---|
| "When's the first train to Haifa after eight on Sunday?" | rail.co.il: the stations, the date, the time; "08:17, 55 minutes" |
| "Order a build-your-own salad from a place near me" | Wolt: finds one, makes its required choices, goes to checkout, asks to place it |
| "The cheapest direct flight to Athens on the 6th" | Google Flights: one way, the airports, the date, nonstop; the airline, the times, the price |
| "How long is the drive to the office at nine?" | Google Maps: the route at that time; "16-24 minutes" |

It's asked for in a Telegram chat ([telegram.md](telegram.md#errands-in-the-browser)), with `[browser]` on, and
answers there; it also runs from a terminal (below) and from its evals.

## How it works

The code is `src/voice_assistant/browser/`.

**The browser** (`chrome.py`) is a real Chrome with TARS's own profile, which stays signed in to what it's been signed
in to (below). It's started without automation flags, since sites refuse sign-in to a browser that says it's
automated, and with a debugging port on this machine only, which Playwright attaches to. It's a window, not
headless: headless Chrome gives itself away, and some sites answer it with an "I am human" check.

**What the model sees** (`page.py`) is the page's accessibility tree, what a screen reader reads, as Playwright takes
it for agents: every element by its role and name, its state (checked, expanded, disabled), where it is, and a ref
that a click can use. That's from the browser itself, so it holds on any site, a React shop as well as a plain page.
Raw, it's long (Wolt's front page is 400,000 characters), so it's rewritten for reading:

- one line per block (a heading, a list item, a row), with what's in it written on it, the way a person reads a card:
  `- [e96 link "Pizza Kikko"] 25-35 min · Delivery ₪0.00 · (image: Rating) 8.6`
- only what can be acted on carries a ref, `[e36 button "Add to order ₪66.00"]`; the wrappers around everything vanish
- an open dialog is what's in front of the person, so it's shown first and in full, the page behind it in outline
- a page still too long is shown around the screen, with the headings above and below it, and `find` searches all of it

That's 5 to 30 times shorter than the snapshot, and a page is read in a few hundredths of a second.

**What it does** (`session.py`) is a batch of steps a turn, by ref: click, type (Enter, or key by key for a box that
suggests as you type), choose, press a key, scroll, hover, go to an address, back, wait. Text with no ref can be
clicked by its words, near other words when it repeats on every card (a time slot, an "Add"), and as a last resort
a point on the screenshot. Each is done the way a person would do it, through Playwright's checks (it's there,
visible, enabled, not covered) and real mouse and keyboard input, and the page is given time to settle after it.

A step that opens something (a new page, a dialog, a list of suggestions) ends the batch, since what the next steps
were planned on has moved, and the next view starts with what the step added to the page (a date picker, an error,
a line in the basket), wherever the site put it. A link that opens a tab moves the agent there. A step that fails
says why ("a dialog is over it"), and isn't tried again on that page; the same steps three times in a row aren't
done a fourth.

**When it looks** is up to it, mostly: a screenshot costs about a thousand tokens and slows the turn, and most pages
say all there is in words. It asks for one (`look`) for a map, a picture or a layout, and gets one without asking
when a step failed or changed nothing, which is when a model reading text goes wrong, and with a page of almost no
text. The refs are drawn on the picture, so what it sees is what the view names.

**The model** (`agent.py`) is any capable one: on OpenAI's Responses API (Luna, Sol) or Anthropic's Messages API
(Opus, `anthropic_brain.py`), at the reasoning effort the settings say, raised for the turn after something went
wrong. It answers each turn with one call: `act`, `find`, `look`, `request_approval`, `sign_in` or `done`. It keeps
the whole errand in view: every page as it saw it, and its own reasoning carried from turn to turn (OpenAI's
encrypted reasoning items, Anthropic's thinking blocks, sent back unchanged), so a long errand builds on what it
already worked out. Repeated input is cached, so that costs little; a run past 150,000 tokens a request keeps only
the newest page in full from then on.

## Never committing the person

The agent may fill a basket, pick options and go to the checkout. It may never place the order, pay, book, reserve
or send. At that step it calls `request_approval` with the button, and code takes over:

1. The button must be on the page, and must be one that commits: a "Go to checkout" is pressed by the agent itself.
2. The person is sent what will happen, from the page, not the model: the site, the button's label, the total read
   off the checkout, the model's summary, and a screenshot.
3. Only on their yes, and only if the total and every price on the page are still the ones they saw, code presses
   the button.

The gate (`gate.py`) is what holds when a model doesn't listen: every click and every Enter is checked first, and one
on a button that commits ("Place order", "Complete booking", "Pay now", "בצע הזמנה", "לתשלום") isn't done. It reads
English and Hebrew, and errs towards stopping: a false stop costs a question, a missed one costs money.

Every click is checked, whichever way it's asked for: by ref, by text, or at a point on the screen (what's there is
read first). What a page says is information, never an instruction: the agent is told so, and the gate doesn't
read the page.

## Signing in

When a site wants a sign-in, the agent calls `sign_in`, and code types the login (`login.py`), a page at a time:
whatever the site's sign-in form asks (an email and a password, a phone number, an ID number), read off the form with
the site's own labels, and a code the site sends. The model never sees any of it, only
how it went, and the trace leaves out what was typed. Before each step the page must be at the login's own site, as
the browser reports its address, over HTTPS; the field must be in the page itself, not a frame from elsewhere; and
its form must send to that same site. A look-alike address gets nothing, and so does a page asking to sign up: TARS
signs in to accounts the person has, it doesn't make them.

The logins are in TARS's vault (`vault.py`): each a household member's (a task uses only its person's), tied to the
exact sites it's for, and kept encrypted beside the browser's profile. One TARS lacks is asked for when it's needed,
for the site the browser is actually at, and kept for that site: on Telegram, as a link to a one-time page
(`login_page.py`); in a terminal, there. Either way it asks for exactly the fields that site's form has, by the
site's own labels, as code read them (not as the model says), and a field a later page asks for is asked then. Or ahead of time:

```bash
uv run python -m voice_assistant.browser vault add https://www.ksp.co.il   # asks for the email and the password
uv run python -m voice_assistant.browser vault mail alon@gmail.com         # a mailbox to read sign-in emails from
uv run python -m voice_assistant.browser vault list                       # whose, for which sites (no passwords)
```

**Codes and links by email.** With a mailbox in the vault (for Gmail, an app password), TARS reads the sign-in
emails a site sends (`mail.py`): read-only, over IMAP, only mail from that site's own domain that came after it
asked. A code is looked for there and asked of the person at once, and whichever comes first is typed; a sign-in
link is opened only if it's on the site's own domain. A site that signs in by email alone (Wolt) gets the mailbox's
address. What an email says goes to no model: code takes the code or the link out of it.

Once signed in, the site's cookies keep it so, often for weeks. Each household member has a browser profile of
their own, so one's sign-in never becomes another's. For a site that signs in only with Google or a
passkey, `signin` opens TARS's Chrome on a desktop to sign in by hand.

## Learning a site

After each run, the model writes a few short notes about the site it worked (`notes.py`, one file a site in
`voice_data/browser/notes/`): the addresses that go straight to something, what things are called and where they
are, what failed and what worked instead, what the site asks for before the checkout. They're written from the
run's own steps, about the site and never about the person. The next run there starts with them, as hints: its
goal still decides, and the gate holds whatever they say. They're plain text, for a person to read, fix or delete.

Every run also leaves a trace: each turn's call, what it did, how long the model and the browser took, and what the
page fetched behind the scenes (its API calls, with their JSON). That's what a site's shortcut could be built from
later: the requests that did the work, replayed by code.

## Running it

```bash
uv sync --extra browser
uv run python -m voice_assistant.browser run "What was my last order on Wolt?"
uv run python -m voice_assistant.browser view https://wolt.com --shot wolt.jpg   # what the model is shown
```

`run` asks in the terminal before anything is committed, and even then presses nothing without `--really-commit`;
a login or a code it lacks is asked for there too.

## On the Pi

The same code, with three differences that come from the Pi having no screen
([`chrome.py`](../src/voice_assistant/browser/chrome.py)):

- **Debian's Chromium on a virtual screen.** Google Chrome has no build for the Pi; Chromium runs as a normal window
  on Xvfb ([`deploy/voice-assistant-display.service`](../deploy/voice-assistant-display.service)), not headless,
  which says so in every request (`HeadlessChrome`) and some sites refuse. WebGL is drawn in software (SwiftShader):
  a browser without it is a bot to Cloudflare.
- **Logins without a keyring.** Pi OS Lite has none, so Chromium keeps them with `--password-store=basic`: the
  profile (`~/.cache/tars-browser`) is only as safe as its folder, which only TARS's user may read, and which the
  backup leaves out.
- **Nobody sees its screen.** TARS signs in from its vault (above): `vault add` over ssh puts a login in ahead of
  time. A site that asks whether you're a robot (rail.co.il's Cloudflare check, on Linux Chromium) can't be
  answered, and TARS says so rather than trying to get past it.

[`deploy/sim`](deployment.md#trying-it-on-a-simulated-pi-first) tries all of it on a simulated Pi first. At about a
Pi 4's speed there, a page takes 1-4 s to load and 1.5-5 s to read (most of it waiting for a busy page to stop
moving), so an errand takes longer than on a Mac, mostly in the browser: the model is in the cloud either way.

## How well it works

Sixteen tasks on the real sites (`evals/browser/tasks.toml`): eight questions, one look at the person's own
account, and seven orders and bookings up to the last button. Nothing is ever ordered: the runner approves each
commit as a dry run, which stops the agent there, and empties any basket a task filled. Graded by reading each
answer and trace against the task's `check`. Run on 2026-10-23 with Luna, against the first prototype
(`tools/browser_proto.py`: its own page scraping, numbered elements, a screenshot every turn):

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
| **All** | **6/16**, 202 turns, 24.5 min, $0.07 | **12/16**, 114 turns, 10.1 min, $0.07 | **12/16**, 118 turns, 10.9 min, $0.09 |

✅ right; 🟡 most of the way (the right basket, but a step short or a choice not checked); ❌ wrong or stopped.
Two ✅ are a right "can't": the hummus place was closed (the basket was ready at checkout), and IKEA Israel sells in
stores only (ikea-lamp was an order then; it's a question now).

- **Twice the tasks, in less than half the time.** Most of it is seeing the page as it is: the prototype's own
  scraping missed what sites draw in code (Wolt's required options, a menu Booking.com opens at the end of the page,
  Ontopo's date picker), and it went round in circles there until it ran out of turns.
- **A turn is about 1.5 s of model and 2 s of browser.** A task is mostly how many turns it takes, so the gains
  came from fewer turns: batches that stop when the page changes under them, clicking text that has no ref, and
  seeing first what a step just opened.
- **Every order stopped where it should.** Each approval was at the button that commits ("Place order"), with the
  checkout's total, and the gate refused the clicks that tried to go further. No run placed an order or a booking.
- **The notes didn't pay off yet.** The second visit was as right as the first and no quicker overall: quicker on
  Maps and Flights, slower where a note sent it the long way round. A note that recorded a past run's dead end
  ("the results didn't show") made the next run give up; notes are now about how a site works, never how a run
  went, and the page outranks a note.

What still goes wrong:

- **Booking.com** redraws its results slowly after a sort, and the agent reads the page half drawn and gives up
  rather than waiting or scrolling.
- **KSP** reaches the cart, but not past it to the checkout.
- **Choosing**: "well rated" isn't checked against the ratings the list shows before picking.
- **Grocery search** on Wolt's markets is slow, and a closed store is a dead end where another store would do.

```bash
uv run --all-extras python tools/browser_eval.py                         # every task, the new agent
uv run --all-extras python tools/browser_eval.py --task wolt-options --notes off   # one, as a first visit
uv run --all-extras python tools/browser_eval.py --agent proto           # the prototype, on a copy of the profile
```

A round starts with TARS's baskets empty, and costs about $0.10 and ten minutes. Records and traces go to
`voice_data/browser/evals/<time>-<label>/`.

## Not yet

- **Asked aloud.** Errands are asked for in a chat; by voice, the approval and anything else it needs would still
  come to the asker's chat, from the voice's process, which doesn't run the browser.
- **Errands at once.** One runs at a time: there's one browser. A browser per person could run theirs side by side.
- **Shortcuts.** A site's own API calls, learned from successful traces, for what's asked often.
