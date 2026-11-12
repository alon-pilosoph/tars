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
| "When Stacey gets home, tell her what's waiting" / "when the last one leaves, turn off the AC" | run when someone comes home or goes out; as they walk in, when they open the front door, though then it only greets and reads the house, changing nothing ([the front door](presence.md#the-front-door)) |
| "If the CO2 goes over 1000, turn on the purifier and tell me" / "when the front door is unlocked, …" | run when a device changes |
| "Tell me when Alon teaches you something new" | runs when TARS is updated, and is told what changed: in your chat, or aloud |
| "What did you do today?" | what skills did by themselves, when, and why |
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
- **Asking is teaching**: nobody needs to know what a skill is. "Keep me updated when Alon teaches you new things" is
  just done, and answered the way a person agreeing would ("Will do: I'll message you whenever Alon adds something
  new"), never with "skill", "trigger" or "saved", and with a question only for what TARS can't sensibly fill in. One
  asked for in a chat is said in that chat, one asked aloud aloud, and the household's always aloud. Only someone TARS
  knows can teach it (an unrecognized voice can use the household's skills, but not change them), and a reply thrown
  away, because someone kept talking, saves nothing.
- **Preferences** are short notes, about something ("gaming news", "the AC"). Said by you, they're kept as said;
  noticed by the model from what you keep asking for, they're kept as guesses, and never told back as something you
  said. Saying one makes it no longer a guess.
- **At its time**, a skill runs as a turn of its own, with its instructions as the request and a note that nobody
  asked. Aloud, the voice's TARS says the reply after a chime and listens for an answer without the wake word (even
  while muted, when it asks something: unmuted just for the answer, a few seconds of it, then muted again). In a chat, the Telegram service posts it, and
  the answer follows on in that chat. When there's nothing to do (the news is already on), it says nothing. A
  person's own skill runs as theirs: their feeds, and what they like.
- **On something happening**: a skill can run when someone comes home or goes out (by name, anyone, or everyone:
  the first one home, the last one out), or when a device changes: its status comes to say a word it didn't
  ("unlocked", "open", "off"; as a word, so "unlocked" isn't "locked"), or a reading in it crosses a limit ("co2"
  over 1000). The turn is told what happened ("Stacey just came home"). Each service watches for its own skills,
  every 10 seconds: who's home from the event database, and only the devices a trigger names, by their status.
  What changed while TARS was starting is seen, never acted on, and a skill set off runs again no sooner than ten
  minutes later, so it can't set itself off, or flap with a reading around its limit. A skill runs at set times or
  on something happening, not both.
- **Dry runs**: a skill that waits on a device is replayed over the device's last 7 days before it's kept. Its Home
  Assistant history is worded as its status is now (`Entity.past`), and the same rule the clock uses, cooldown and
  all, counts when it would have run; a status gone again within the clock's 10 seconds isn't counted, as the clock
  could miss it. The model is told ("it would have run 41 times, most often around 16:00", the hour only from three
  runs) and says so only when it matters: far more often than wanted, or never, offering to change it. A device Home
  Assistant doesn't keep a history of (an Android TV, a plugin's without `past`) is kept without one.
- **On TARS being updated**: a skill can run when TARS itself is updated, told what changed from the version it last
  saw to the one running (each change's PR title, from the checkout's git history, [`tars` tool](architecture.md)),
  so "tell me when you learn something new" is said in plain words, as what someone can now ask for. Each such skill
  keeps the version it last saw in the database, so an update, which restarts TARS, is told once even though it
  happened while TARS was down; one just saved only notes the version it starts from.
- **What it did** is kept: each run by itself, when, why, and what it said (`skill_runs`), so "what did you do
  today?" has an answer.
- **The tools keep their own rules**, whatever a skill says: unlocking needs the words said, by a voice trusted with
  it, so no skill opens the door; an order still waits for its tap on Telegram. A skill set off by someone opening
  the front door's lock ("Stacey just came home, opening the front door") reads the house but changes nothing in it,
  whatever it says: what a lock says over MQTT is only ever who came in
  ([the front door](presence.md#the-front-door)).

The web UI's **Skills** page shows all of it, and what skills did by themselves lately; a skill can be paused,
resumed or deleted there, and a preference forgotten ([the web UI](web-ui.md)).

Kept in the event database (`[learning] log_events`): the `skills`, `preferences` and `skill_runs` tables, backed
up with the rest.

Not yet: a device change shorter than ten seconds (unlocked and locked again in between) isn't seen; Home
Assistant's own event stream would catch it.

## Skills from config.toml

Skills used to be `[[home.skills]]` in `config.local.toml`. TARS won't start with them there now; import them once,
as the household's, and delete them from the file:

```bash
uv run python -m voice_assistant.skills import
```
