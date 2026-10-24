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

It's asked for aloud at home or in a Telegram chat, with `[browser]` on
([telegram.md](telegram.md#errands-in-the-browser)): what it needs on the way (a yes before ordering, a login, a
code) comes to the asker's phone, and the answer comes there, and is said aloud too when it was asked aloud. It also
runs from a terminal (below) and from its evals.

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

1. The button must be on the page, and must be one that commits: a "Go to checkout" is pressed by the agent itself
   (but not on a page that takes payment).
2. The person is sent what will happen: the site and the button's label, the agent's summary (marked as the agent's),
   the amounts the page shows beside the button, read by code (`money.py`), and a screenshot.
3. Only on their tap on Order, and only if those amounts are still the ones they saw, code presses the button.

The gate (`gate.py`) is what holds when a model doesn't listen, or is told otherwise by a page: every click and every
key is checked before it's done, and one that would press something that commits isn't. It goes by two things:

- **What it says**, in English and Hebrew, as plain lists of phrases ("place order", "pay", "confirm and book",
  "לתשלום", "בצעו הזמנה"), after the label is put in one form (Unicode's, without the invisible marks right-to-left
  text carries, without Hebrew's one-letter prefixes), and a label with a confirming verb and an order word
  ("Confirm the scheduled order"). A bare "Confirm" or "אישור" commits only where its form or dialog shows an
  amount: on a time picker it's just a choice.
- **Where it is, from the page itself:** every label a press goes by (its own text, the button or clickable area
  it's in, as a framework's div button is, the control a label is for), the element that has focus for Enter and
  Space, followed into shadow roots and frames, and whether the page takes payment (card fields, a payment
  provider's frame): there, any submit button commits, whatever it says. A click or a key into a frame of another
  site (a payment provider's button) is refused, since what's in it can't be read.

It errs towards stopping: a false stop costs a question, a missed one costs money. It's tested in a real Chrome on
built pages for each way a press can be asked for (`tests/test_browser_chrome.py`), and against the labels the eval
runs clicked. What a page says is information, never an instruction: the agent is told so, and the gate takes nothing
from the page but its buttons' labels and shape.

**Nothing at home is opened** (`homenet.py`, shared with the web UI): no address on this machine or the home network
(TARS's web UI, the router, Home Assistant), whether by name, by address however it's written, or by what a name
resolves to. A page that leads there is left before anything of it is read.

**Secrets stay out of the model's view.** A password field's value, in shadow roots and frames too, and anything a
sign-in typed are left out of every page the model reads and covered in its screenshots, and blanked out of the
trace.

## Signing in

When a site wants a sign-in, the agent calls `sign_in`, and code types the login (`login.py`), a page at a time:
whatever the site's sign-in form asks (an email and a password, a phone number, an ID number), read off the form as a
browser reads it (the standard's autocomplete tokens, then the field's type, then its label, Hebrew too), and a code
the site sends (in one box, or one character a box). The model never sees any of it, only how it went. Before each
field is typed the page must be at the login's own site, as the browser reports its address, over HTTPS; the field
must be in the page itself, not a frame from elsewhere; its form must send to that same site, and must not be one
that pays, orders or books (a guest checkout's email field). A look-alike address gets nothing, and so does a form
asking to sign up (a new password, a name to fill in): TARS signs in to accounts the person has, it doesn't make them.

The logins are in TARS's vault (`vault.py`): each a household member's (a task uses only its person's), tied to the
exact sites it's for, and kept encrypted in a folder only TARS's user can read. One TARS lacks is asked for when it's
needed, for the site the browser is actually at, and kept for that site: on Telegram, as a link to a one-time page
(`login_page.py`); in a terminal, there. Either way it asks for exactly the fields that site's form has, by the
site's own labels, as code read them (not as the model says), and a field a later page asks for is asked then. Or
ahead of time:

```bash
uv run python -m voice_assistant.browser vault add https://www.ksp.co.il   # asks for the email and the password
uv run python -m voice_assistant.browser vault mail alon@gmail.com         # a mailbox to read sign-in emails from
uv run python -m voice_assistant.browser vault list                       # whose, for which sites (no passwords)
```

**Codes and links by email.** With a mailbox in the vault (for Gmail, an app password), TARS reads the sign-in
emails a site sends (`mail.py`): read-only, over IMAP with the server's certificate checked, and only mail the
person's own mail server found really from that site's domain (DKIM or DMARC passing), by the server's own time it
came after TARS asked. A code is looked for there and asked of the person at once, and whichever comes first is
typed. A model reads the email for the code and the link, and what it says is taken only as a code (codes.py: a few
letters and digits, that the email has as written) or as one of the email's links on the site's own domain, as a
browser reads its host. A site that signs in by email alone (Wolt) gets the mailbox's address, and the login is
kept once that worked.

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

## Setting it up

1. In `config.local.toml`, turn on `[browser]` (with `[telegram]` and `[learning] log_events`, which it needs: the
   Telegram service runs the errands, and they're queued in the event database):

   ```toml
   [browser]
   enabled = true
   really_order = false   # true once you trust it: an approved order is then placed
   ```

2. `OPENAI_API_KEY` in `.env`, and the browser's packages: `uv sync --all-extras`. On the Pi, `deploy/install-pi.sh`
   also installs Debian's Chromium and the virtual screen it draws to (`voice-assistant-display`).
3. The logins, ahead of time (`vault add`, `vault mail`, above), or when they're first needed.
4. Restart the Telegram service and the assistant, then `voice-assistant --check`: it has a line for the browser
   (its model, the Chromium it found), the errands waiting, and the display service.

## Running it

```bash
uv sync --all-extras
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

[`deploy/sim`](simulator.md) tries all of it on a simulated Pi first. At about a
Pi 4's speed there, a page takes 1-4 s to load and 1.5-5 s to read (most of it waiting for a busy page to stop
moving), so an errand takes longer than on a Mac, mostly in the browser: the model is in the cloud either way.

## What leaves the machine

- **To OpenAI,** for each errand: the goal, and the pages the browser works as the model reads them (their text, and
  now and then a screenshot), which can be the person's own accounts on those sites (an order history, a saved
  address), with what TARS noted about each site before. A site's sign-in email, when one comes, for its code or
  link. Never a password, a code TARS types or anything from the vault: those are typed by code, kept out of what
  the model reads and out of the traces.
- **To the sites,** what any visitor's browser sends them, and the logins TARS keeps for them, each only to its own.
- **From the mailbox,** over IMAP, read-only: the sign-in emails from the site being signed in to, while it signs in.
- **On the home network,** the one-time page for a login TARS lacks, served on this machine while it waits
  (`[browser] login_port`), plain HTTP: better reached over Tailscale.
- **To Telegram,** the checkout's screenshot and what an order commits to, to approve, and the codes sent back
  ([TARS on Telegram](telegram.md#what-it-sends-telegram)).

The browser never opens an address on this machine or the home network (TARS's web UI, the router), whatever a
page asks: a page can say anything to a model, and those don't ask for a password.

## How well it works

Measured on sixteen tasks on the real sites, every order stopped at the last button
([browser-evals.md](browser-evals.md), with every run). The short of it:

- **The rebuild did 12 of 16 where the first prototype did 6**, in less than half the time, mostly from seeing the
  page as it is (the accessibility tree) rather than scraping it.
- **On the five hardest** (a Wolt margherita, a salad built to a spec, groceries, a cable at KSP under ₪50 all in,
  the cheapest well-rated hotel on Booking.com), TARS's loop with the whole errand in view and the model's reasoning
  carried did all five with Luna, at about seven cents a task, and with Sol at ten times that. With only the newest
  page in full, Opus and Sol each managed 3-4. Claude Code with Opus did all five too, a little faster.
- **On a simulated Pi 4**, the nine tasks that need no account all came out right, about twice as slowly as on a
  Mac.
- **Every order stopped at the button that commits**, with the checkout's total, in every run of every loop.

```bash
uv run --all-extras python tools/browser_eval.py                         # every task, the new agent
uv run --all-extras python tools/browser_eval.py --task wolt-options --notes off   # one, as a first visit
```

## Not yet

- **Approving aloud.** An order asked for aloud is still approved on the phone: a spoken yes is anyone's in the room,
  and the checkout is a picture worth seeing.
- **Errands at once.** One runs at a time: there's one browser. A browser per person could run theirs side by side.
- **Shortcuts.** A site's own API calls, learned from successful traces, for what's asked often.
