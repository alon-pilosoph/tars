# Skills and preferences

TARS is taught by talking to it, aloud or in a chat: skills (what to do when someone says a name, or at set times),
and what each person likes. Nothing is written in a config file, and nothing new is coded: a skill is plain words for
the model, which carries them out with the tools it already has.

| Said | What happens |
|---|---|
| "From now on, when I say bedtime, turn off the TV and put the AC on 24" | TARS says it back, and saves it once you say yes |
| "Bedtime" / "the kids want something calm to watch" | the skill it names, or the one that's for that, done |
| "Every day at 19:55, ask me if I want the news on" | a skill that runs by itself, said aloud at home |
| "Every morning at 7:30, send me what's new in gaming" | one posted in your own chat instead |
| "Change the news one to 20:00" / "pause it this week" / "delete bedtime" | changed, paused or deleted |
| "I don't care about esports" / "skip the sports news" | remembered, and followed from then on |
| "What do you know about me?" / "forget that I like jazz" | told, guesses as guesses; or forgotten |

## How it works

- **A skill** is a name, its instructions in plain words, and optionally what it's for (so "the kids are bored" finds
  the panda video), times of day it runs by itself, the days, and whether it's said aloud or posted in its owner's
  chat. It's someone's own, or the household's for everyone: one about the house or the TV that anyone might ask
  for is the household's. Your own skill of the same name is the one you get.
- **Each request is told** the skills and preferences of whoever is asking (the voice TARS recognized, or the chat's
  person), after the instructions that are cached. A skill taught a minute ago is known on the next request.
- **Teaching is read back first**: the model says what it understood and saves it once you agree. Only someone TARS
  knows can teach it (an unrecognized voice can use the household's skills, but not change them), and a reply thrown
  away, because someone kept talking, saves nothing.
- **Preferences** are short notes, about something ("gaming news", "the AC"). Said by you, they're kept as said;
  noticed by the model from what you keep asking for, they're kept as guesses, and never told back as something you
  said. Saying one makes it no longer a guess.
- **At its time**, a skill runs as a turn of its own, with its instructions as the request and a note that nobody
  asked. Aloud, the voice's TARS says the reply after a chime and listens for an answer without the wake word (even
  while muted, when it asks something: unmuted just for the answer). In a chat, the Telegram service posts it, and
  the answer follows on in that chat. When there's nothing to do (the news is already on), it says nothing. A
  person's own skill runs as theirs: their feeds, and what they like.
- **The tools keep their own rules**, whatever a skill says: unlocking needs the words said, by a voice trusted with
  it, so no skill opens the door; an order still waits for its tap on Telegram.

Kept in the event database (`[learning] log_events`): the `skills` and `preferences` tables, backed up with the rest.

## Skills from config.toml

Skills used to be `[[home.skills]]` in `config.local.toml`. TARS won't start with them there now; import them once,
as the household's, and delete them from the file:

```bash
uv run python -m voice_assistant.skills import
```
