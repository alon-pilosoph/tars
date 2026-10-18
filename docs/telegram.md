# TARS on Telegram

The household can chat with TARS on Telegram from anywhere, in writing or with a voice message: ask it something,
set a reminder, have it send something. TARS also sends things there: a reminder set in a chat comes to that chat
when it's due, and what TARS sends at home for someone ("send me the recipe") comes to their phone as well as the
TARS page.

It's the same TARS as at home, with the same tools and reminders, told it's writing rather than speaking, and from a
chat it can also say things aloud at home. Chats are answered by OpenAI's model directly rather than Qwen first
([why](#design)). Telegram rather than WhatsApp because Telegram allows bots like this one; WhatsApp allows them only
through its Business API, which since January 2026 turns away general-purpose assistants, or through an unofficial
client, which its terms forbid.

## Setting it up

1. **Make a bot.** On Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, and pick a name and a
   username ending in `bot`. Put the token it gives you in `.env` as `TELEGRAM_BOT_TOKEN`.
2. **Find each person's chat id.** Each person opens the bot and sends it anything. TARS answers nobody it doesn't
   know, but logs each new chat once: `(Telegram: Alon wrote from chat 123456789, which isn't in [telegram] people;
   not answered)`. Without TARS running, the same id is in
   `https://api.telegram.org/bot<token>/getUpdates`, under `message.chat.id`.
3. **Say who may chat.** In `config.local.toml`, next to `config.toml` (this machine's own settings, never
   committed, so the chat ids stay out of the repo and `git pull` never meets a local edit):

   ```toml
   [telegram]
   enabled = true
   people = { Alon = 123456789, Stacey = 987654321 }
   ```

   Use the names TARS already knows people by (from their voices, or the web UI's Voices page), so a reminder "for
   Stacey" set by voice finds Stacey's chat.
4. **Run it.** `uv run voice-assistant --telegram`, next to the assistant and the web UI. On a Pi,
   `deploy/install-pi.sh` installs and starts `voice-assistant-telegram.service` once `[telegram]` is on.
   `voice-assistant --check` checks the token and says who the bot is for.

## What it does

| Written in the chat | What happens |
|---|---|
| "what's 17 times 23?" | TARS answers in the chat |
| a voice message: "remind me to call mum at six" | written down by speech to text, as at home, and answered in writing |
| "what's the weather?" | handed to OpenAI with web search, as by voice; "typing..." while it looks |
| "remind me at 6 to call mum" | set, and posted to this chat at 6:00 |
| "set a pasta timer for 12 minutes" | "Your pasta timer is done." in this chat after 12 minutes |
| "tell Stacey dinner's at 8, at 7:30" | to Stacey's chat at 7:30 if she has one; aloud at home if not |
| "tell the house dinner's ready" | said aloud at home now ("Everyone, a message from Alon: dinner's ready."), and the chat hears when it was said and who said they got it |
| "tell Stacey at home to call me" | said aloud at home, even if Stacey has a chat |
| "send me a packing list for Rome" | the list is posted in the chat, and kept on the TARS page |
| /new | starts a new conversation: TARS forgets this one |
| "I took the pills" | acknowledges a reminder that was said aloud at home and is waiting for a "got it" |

And from home: "hey TARS, send me the recipe" puts it on the TARS page and in the asker's chat. Things sent for the
household (a shared shopping list) stay on the TARS page only.

Chats are on the web UI's Home page like any conversation, marked "A chat on Telegram", and the Reminders page says
which reminders were sent on Telegram.

## Design

**A process of its own.** `voice-assistant --telegram` (`telegram.py`) runs beside the assistant and the web UI,
over the same database, so a problem with Telegram never touches the voice, and the other way round.

**Nothing opened to the internet.** TARS asks Telegram for new messages (long polling, up to 50 s a request) rather
than having Telegram call in (a webhook), which would need a public address for the Pi. It's about one request a minute
when nobody's writing.

**Only the people named.** Anyone can find a bot and write to it, so TARS answers only private chats whose id is in
`[telegram] people`, and treats each as that person, as speaker ID does by voice (`[Speaker: Alon]`). Everyone else
gets no answer at all, so a stranger learns nothing, not even that TARS is there. Group chats are ignored.

**OpenAI's model, not Qwen first.** At home Qwen answers first because it starts speaking about 0.3 s sooner, and
with a voice the first word is what you feel. A chat is read whole, so that buys nothing, and every hand-off made the
chat wait for two models. So a chat goes to `[llm] model` directly (`[telegram] brain = "openai"`), which also
searches the web and sends real links itself, on the standard tier rather than "fast" (`[telegram] service_tier`,
nobody's waiting on a first word) and with a little thinking (`reasoning_effort = "low"`). Without an OpenAI key, or
with `brain = "quick"`, Qwen answers first as by voice. There's no backup the other way: if OpenAI fails, the chat
says so.

**One thread, many conversations.** In Telegram a chat is one long thread; TARS splits it by time, as by voice.
Each person has their own brain, so their own history: everything said since the conversation started (up to its
last 50 exchanges), sent with each message, and nothing older. A conversation ends once the chat has been quiet for
`[telegram] memory_minutes` (60, longer than by voice's 10, since a written reply often comes back much later), or
when they send **/new**, for a change of subject; the next message starts a new one. What TARS posts by itself (a
reminder that's due, how a message said at home went, what was sent at home for them) is part of the conversation
too, so "remind me again in ten minutes" right after a reminder knows which, and snoozes it: a reminder posted in
the chat is in the model's list, by number, for an hour after, and snoozed it's posted again at its new time. On the web UI, each conversation is its own, like one at home.

**Never in the middle of an exchange.** Posting runs on its own thread, every 2 s. While TARS is answering
someone, nothing it posts by itself goes to their chat: a reminder that comes due meanwhile is posted right after the
answer, so neither the chat nor what the model remembers has it between a question and its answer.

**Voice messages, heard as at home.** A voice message (up to five minutes) is fetched from Telegram, read from its
Opus into the 16 kHz audio TARS keeps of everything said at home, and written down by the same speech to text, from
`[stt]` and the keys there are: Deepgram on its EU servers, then OpenAI if Deepgram fails. It goes to Deepgram's
endpoint for whole recordings, not Flux: Flux decides when a speaker is done, and would end the message at its first
long pause. The words are answered as if written, tagged `[Voice message]`, so a misheard word is asked about rather
than acted on; the recording is kept with the turn, and the web UI plays it like one said at home. Nothing heard:
"I couldn't make out any words in that."; speech to text failing: "I couldn't listen to that just now.", kept on
the web UI with why. Who sent it is the chat's person, as for anything written there: speaker ID isn't asked, and a
voice message never trains it.

**Writing, not speaking.** The system prompt is written for the ear. For a chat, a rule goes last in the
instructions, so it wins: this is in writing, reply as in a text message, plain text, links are fine, what you send is
posted here, and who's on Telegram (so a reminder for someone without a chat is said aloud). The lines TARS says while
a slow answer comes ("Looking it up.", "Let me think about that for a moment.") are taken out: the chat shows
"typing..." instead.

**Done once it's delivered.** As by voice, what a reply asks for (a reminder set, something sent) happens only once
the reply has reached the person: a reply that can't be sent, after three tries, is forgotten, so a reminder they
never saw confirmed isn't set behind their back.

**Where a reminder is said.** Each reminder has a `channel`: `voice` (aloud at home) or `telegram` (to its person's
chat). One set in a chat goes to the chat of the person it's for, or of whoever set it when it's for "whoever's
there" (a timer), if they have one; otherwise it's aloud at home. Messages that wait until someone's back are always
aloud: they wait for a voice. The assistant at home only ever sees `voice` reminders (`Reminders.due` and the clock
filter by channel), so it never stops listening for one it won't say.

**Aloud at home, from a chat.** A chat's `remind` tool has one more field, `aloud`: said aloud at home rather than
posted to a chat. For the whole house it's a message for "everyone" ("Everyone, a message from Alon: …"), due now or
later. The assistant picks it up within 2 s, after a chime, never over a conversation, and repeats it until someone
says got it, as any message. The chat is told how it went (`reminders.reported` keeps what it was last told): "Said
at home at 7:41 PM", then "Stacey said they got it at 7:42 PM" or "Nobody said they got it (said 4 times at home)".
It's only said while the assistant at home is running; if it's off, it's said when it's back, with "This was due at…".

**Said once, not repeated.** A phone keeps the message until it's read, and Telegram doesn't tell a bot when it's
read, so a reminder posted to a chat is marked Said, once, whether or not it waited for a "got it". Late ones say so,
as aloud: "This was due at 8:00."

**Late messages aren't taken as new.** A message more than 10 minutes old when TARS reads it (it was off) gets "I was
off when this came in, at 8:02. Send it again if it still matters.", not an answer: "in ten minutes" would be
wrong by then.

**What TARS sent at home** for a person with a chat is posted there within a couple of seconds (`items.delivered`
records when). Only things from the last hour: after TARS has been off a long time, a backlog of old notes would be
noise. Things sent before this feature existed were marked delivered by the upgrade. `[telegram] send_items = false`
turns this off.

**Failures.** Telegram down: TARS waits and asks again (1 s, then 2, 5, 10, 30 and 60 s), and posts what's due once
it's back. A brain that fails: "That didn't work. Try again in a moment.", kept on the web UI with why. A token
Telegram refuses stops the service with a clear line (exit 78, so systemd doesn't restart it in a loop). The token is
part of every request's address, so no error message includes the address.

## What it sends Telegram

Telegram gets what's written in the chats, the voice messages sent there, TARS's replies, the reminders posted
there, and what TARS sends to them. Nothing from home goes to Telegram except the things sent for someone with a
chat. A voice message's recording also goes to speech to text, as a request at home does: Deepgram, or OpenAI if
Deepgram fails. Telegram bot chats aren't
end-to-end encrypted: Telegram's servers can read them, as they can any chat that isn't a secret chat.

## Not yet

- **Photos, and replies by voice.** A photo gets "I can only read text and listen to voice messages for now."; a
  photo could go to OpenAI's model, which reads images. TARS answers a voice message in writing; it could answer in
  its own voice, as a voice message.
- **"Got it" for a reminder posted to a chat.** It's posted once and done, so there's nothing to acknowledge;
  "remind me again in ten minutes" sets it again.
- **Missed reminders at home forwarded to a phone.** A reminder nobody acknowledged aloud could go to its person's
  chat before it's marked Missed.
- **Evals.** The conversation evals ([evals](evals.md)) play conversations aloud; a set written for chats would check
  the writing rules and the reminders' channels with the real models.
