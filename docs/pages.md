# Reading pages

When someone wants more than a story's summary ("tell me more about that one"), or sends TARS a link, TARS reads
the page itself and tells what it says, rather than searching the web for it. It learns, for each site, how its pages
are read, so the second time goes straight to the way that works.

| Said | What happens |
|---|---|
| "Tell me more about the Eisenkot one" | its page is read, and told in TARS's own words |
| A link sent in a chat: "what does this say?" | the same |
| A Times of Israel story | that site turns away a plain request: read in TARS's browser, the answer in their chat a few minutes later |
| A Haaretz story, the first time | it's for subscribers: "Do you subscribe to Haaretz?" Yes: read in the browser, signed in. No: the web is searched for the story |
| "How do you read Haaretz?" | what TARS has learned of the site, and whether they subscribe |
| "I cancelled TheMarker" | saved: not asked again, and its stories are searched for instead |

## How it works

- **The quick way first, always** (unless the site is known to turn it away): one plain request, as TARS, from
  `pages.py`, and the article's words from the page: its structured data's `articleBody`, which is the story itself
  and never the related ones beside it; else the paragraphs in its `<article>`; else all of its `<article>`'s words.
  Six thousand characters at most go to the model. Checked on 2026-11-12: Ynet, ynetnews, Israel Hayom, Maariv and
  Globes read this way.
- **The browser,** when the quick way can't: the site answers 401, 403, 429 or 451 (Davar and Times of Israel, at
  times), or the page has under three hundred characters of words, put there by a script. `read_page` starts the
  errand to read it itself, through the brain's browse tool ([browser.md](browser.md)), before anything of the page
  has reached the model, so the errand's own rules hold: only for a voice TARS knows, with a Telegram chat, only once
  the turn goes ahead, and never in a reply that's already read the web. Without the browser set up, it searches the
  web instead.
- **For subscribers** is the page saying so (`isAccessibleForFree: false`) and showing fewer than 1,500 characters:
  every Haaretz, TheMarker and Haaretz English article. TARS asks once whether they subscribe, and keeps the answer,
  yes or no. Yes: the browser reads it signed in, with the vault's login, or the one-time page for one when there's
  none yet, as any errand signs in. A site with both free pages and subscribers' (Globes) has its free ones read the
  quick way.
- **What's learned:** for each site, the household's, how its pages are read and whether some are for subscribers
  (`page_sites`); for each person, whether they subscribe (`page_subscriptions`), in the event database. A site's
  way is tried the quick way again thirty days after it was learned, and learned again whenever it stops working.
- **Never the home network:** an address there, or one any redirect goes to, isn't read (`homenet.at_home`, as the
  browser has it).
- **A page's words are anyone's:** `read_page` says its answers are from outside (`toolbox.Tool.outside`), so a reply
  that's read a page only reads the house from then on, as after a web search, and starts no errand
  ([security.md](security.md#words-from-outside-reaching-a-model)). Saving whether they subscribe is held back as
  any change is.

Nothing to set up beyond the event database (`[learning] log_events`); the browser, for the sites that need it, is
[TARS's browser](browser.md).
