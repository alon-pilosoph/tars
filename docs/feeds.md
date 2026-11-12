# Feeds

TARS follows RSS and Atom feeds for each person, in categories of their own, and everything about them is done by
talking to it, aloud or in a chat:

| Said | What happens |
|---|---|
| "I want to follow gaming" / "any good feeds about cooking?" | TARS searches the web for the topic's feeds, checks each, and suggests three or four, with what each is good for |
| "What's the latest from Eurogamer?" | that feed's newest few stories, told, before deciding to follow it |
| "Follow the first two, under gaming" / "add ynet.co.il to the news" | followed; a site is enough, TARS finds its feed |
| "What's new in gaming?" / "what's new?" | the few stories that matter from that category (or all of them), each in a sentence or two; on Telegram, with links |
| "What else?" | the stories not told yet: the rest stay new until they are |
| "Tell me more about the Zelda one" | its page is read ([reading pages](pages.md)), or looked up on the web |
| "What am I following?" / "drop Kotaku" / "stop following cooking" | listed, or unfollowed; a whole category only once you've said yes |

## How it works

- **A feed is worth following** only if it's live and its items say something on their own. Previewing one (which
  TARS does before suggesting it) reads it and says how fresh it is, how often it updates, and how long its summaries
  are: a feed of bare headlines, or of teasers for paywalled articles, gives the model nothing to tell. When a summary
  is too short but the feed carries the whole article, its start is used instead.
- **Finding a feed from a site**: the page's own `<link rel="alternate">` to RSS or Atom, else where feeds usually
  are (`/feed`, `/rss`, `/rss.xml` and the like).
- **What's new** is the last three days' stories, ten at most from each feed and thirty in all, newest first, that
  haven't been told to this person before, numbered. The model picks and tells the few that matter; the same story
  in several feeds is a bigger one. Nothing new: it says so, and offers the latest again.
- **Told is what was told**: the model ends that reply with a marker of the stories it told, `<told 2 5>`, which is
  taken out before anyone hears or reads it, and only those count as told. The rest stay new, for "what else?" or the
  next time. A reply with no marker counts as all of them told, so a model that forgets never repeats the news.
- **Each person's own**: who's asking is the voice TARS recognized, or the chat's person. When the voice wasn't
  recognized, `[feeds] default_person`'s; without one, TARS says it doesn't know whose. Previewing is anyone's.
- **Following is held back as any change is.** Following or dropping a feed waits for the turn to be over, so a
  reply thrown away follows nothing. It's never done in a reply that searched the web, whose pages could name a feed
  to follow: TARS suggests feeds in one turn and follows what was picked in the next, as above, and if it searched
  again meanwhile, it says to ask once more. Nor in a turn a door's lock set off. Reading, previewing and listing are
  never held back.
- Feeds are read when asked, all at once (about a second), and kept three minutes. Each "what's new?" is one model
  turn reading a few thousand words of headlines and summaries.

Kept in the event database: the `feeds` table, and `feed_seen` (what each person was told,
for thirty days). The backup has them with the rest of the database.

## Setting it up

Nothing to set up beyond the event database. In `config.local.toml`, optionally:

```toml
[feeds]
default_person = "Alon"   # whose feeds an unrecognized voice gets
```
