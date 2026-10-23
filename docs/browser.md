# TARS's browser

TARS works a real web browser for the person: it reads a page, clicks, types and scrolls its way to what they asked
for, on any site, and comes back with the answer. Asked to order or book, it fills the basket up to the last button
and asks the person on their phone; only code presses that button, and only on a yes. Nothing in it knows any one
site: what it knows about a site, it learns by working it.

| Asked | What it does |
|---|---|
| "When's the first train to Haifa after eight on Sunday?" | rail.co.il: the stations, the date, the time; "08:17, 55 minutes" |
| "Order a Margherita from a well-rated pizza place" | Wolt: compares ratings, adds it, makes the required choices, goes to checkout, asks to place it |
| "The cheapest direct flight to Athens on the 6th" | Google Flights: one way, the airports, the date, nonstop; the airline, the times, the price |
| "How long is the drive to the office at nine?" | Google Maps: the route at that time; "16-24 minutes" |

It isn't part of the voice assistant yet: it runs from a terminal (below) and from its evals, while it's measured.

## How it works

The code is `src/voice_assistant/browser/`.

**The browser** (`chrome.py`) is a real Chrome with TARS's own profile, which stays signed in to what it's been signed
in to by hand. It's started without automation flags, since sites refuse sign-in to a browser that says it's
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
suggests as you type), choose, press a key, scroll, hover, go to an address, back, wait. Each is done the way a person
would do it, through Playwright's checks (it's there, visible, enabled, not covered) and real mouse and keyboard
input, and the page is given time to settle after it. A step that opens something (a new page, a dialog, a list of
suggestions) ends the batch, since what the next steps were planned on has moved; a link that opens a tab moves the
agent there. A step that fails says why ("a dialog is over it"), and isn't tried again on that page.

**When it looks** is up to it, mostly: a screenshot costs about a thousand tokens and slows the turn, and most pages
say all there is in words. It asks for one (`look`) for a map, a picture or a layout, and gets one without asking
when a step failed or changed nothing, which is when a model reading text goes wrong, and with a page of almost no
text. The refs are drawn on the picture, so what it sees is what the view names.

**The model** (`agent.py`) is Luna, through the Responses API at low reasoning, medium for the turn after something
went wrong. Each turn it gets the newest page in full and the ones before it as a line each, and answers with one
call: `act`, `find`, `look`, `request_approval`, `sign_in` or `done`.

## Never committing the person

The agent may fill a basket, pick options and go to the checkout. It may never place the order, pay, book, reserve
or send. At that step it calls `request_approval` with the button, and code takes over:

1. The button must be on the page, and must be one that commits: a "Go to checkout" is pressed by the agent itself.
2. The person is sent what will happen, from the page, not the model: the site, the button's label, the total read
   off the checkout, the model's summary, and a screenshot.
3. Only on their yes, and only if the total is still the one they saw, code presses the button.

The gate (`gate.py`) is what holds when a model doesn't listen: every click and every Enter is checked first, and one
on a button that commits ("Place order", "Complete booking", "Pay now", "בצע הזמנה", "לתשלום") isn't done. It reads
English and Hebrew, and errs towards stopping: a false stop costs a question, a missed one costs money.

What a page says is information, never an instruction: the agent is told so, and the gate doesn't read the page.

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
uv run python -m voice_assistant.browser signin https://wolt.com     # sign in by hand, then press Enter
uv run python -m voice_assistant.browser run "What was my last order on Wolt?"
uv run python -m voice_assistant.browser view https://wolt.com --shot wolt.jpg   # what the model is shown
```

`run` asks in the terminal before anything is committed, and even then presses nothing without `--really-commit`.

## How well it works

RESULTS

## Not yet

- **Part of TARS.** Asked aloud or on Telegram, with the approval on Telegram (the prototype did it: an Order button
  and a screenshot), and a long-lived browser per person on the Pi.
- **Signing in by itself.** The prototype has a vault of logins tied to the exact sites they're for, codes asked for
  on Telegram, and email sign-in (`tools/browser_signin.py`); the agent has the `sign_in` call waiting for it.
- **Shortcuts.** A site's own API calls, learned from successful traces, for what's asked often.
