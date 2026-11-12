# Skills and preferences

TARS is taught by talking to it, aloud or in a chat: skills (what to do when someone says a name, at set times, or
once, later), and what each person likes. Nothing is written in a config file, and nothing new is coded: a skill is plain words for
the model, which carries them out with the tools it already has.

| Said | What happens |
|---|---|
| "From now on, when I say bedtime, turn off the TV and put the AC on 24" | TARS says it back, and saves it once you say yes |
| "Bedtime" / "the kids want something calm to watch" | the skill it names, or the one that's for that, done |
| "Every day at 19:55, ask me if I want the news on" | a skill that runs by itself, said aloud at home |
| "Turn off the AC now and back on at 5am" / "in two hours, turn off the TV" | done now, and done once at that time, by TARS itself, then gone |
| "Every morning at 7:30, send me what's new in gaming" | one posted in your own chat instead |
| "When Stacey gets home, tell her what's waiting" / "when the last one leaves, turn off the AC" | run when someone comes home or goes out |
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
  asked for in a chat is said in that chat, one asked aloud aloud, and the household's aloud, but for a one-off,
  which answers whoever asked for it where they asked ([once, later](#once-later)). Only someone TARS
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
- **Once, later**: "turn the AC back on at 5am" is a one-off, not a reminder: TARS does it at its time, and says
  what it did ("Done: the AC's back on, cool at 23°"), where it was asked; if it didn't happen, it's tried again,
  and then whoever asked is told. See [once, later](#once-later). A reminder is only for what someone asked to be
  reminded of.
- **On TARS being updated**: a skill can run when TARS itself is updated, told what changed from the version it last
  saw to the one running (each change's PR title, from the checkout's git history, [`tars` tool](architecture.md)),
  so "tell me when you learn something new" is said in plain words, as what someone can now ask for. Each such skill
  keeps the version it last saw in the database, so an update, which restarts TARS, is told once even though it
  happened while TARS was down; one just saved only notes the version it starts from.
- **What it did** is kept: each run by itself, when, why, and what it said (`skill_runs`), so "what did you do
  today?" has an answer; a one-off that didn't happen is kept as failed, with what its asker was told.
- **The tools keep their own rules**, whatever a skill says: unlocking needs the words said, by a voice trusted with
  it, so no skill opens the door; an order still waits for its tap on Telegram.

The web UI's **Skills** page shows all of it, and what skills did by themselves lately; a skill can be paused,
resumed or deleted there, and a preference forgotten ([the web UI](web-ui.md)).

Kept in the event database (`[learning] log_events`): the `skills`, `preferences` and `skill_runs` tables, backed
up with the rest.

Not yet: a device change shorter than ten seconds (unlocked and locked again in between) isn't seen; Home
Assistant's own event stream would catch it.

## Once, later

"Turn off the AC now and back on at 5am" is two things: the AC off now, and a one-off, a skill saved with `once`
that TARS carries out itself at 5:00 and then forgets.

- **Its time** is given by the model the way reminders take it, a time with the day as said ("tomorrow", or none:
  the next time the clock shows it) or minutes from now, and the code works out the date. A time already past, or
  more than a year ahead, is refused with why. When it puts something back, the model looks at how it is first and
  writes that into the instructions ("turn the AC back on: cool, 23°, fan auto"), so it comes back as it was.
- **It answers where it was asked**: in the chat it was asked in, or aloud. That holds for a household one-off too
  ("turn the heater off at six", from Alon's chat): it's the household's, for anyone to see or change, and how it
  went is posted in Alon's chat.
- **Whichever of TARS's processes is up runs it, and only one does.** The assistant (aloud) and the Telegram service
  (chats) both watch the one-offs. Each is run by the process that answers where it was asked while that one's
  heartbeat ([the Status page's](web-ui.md#the-status-page)) says it's running, and otherwise by the other; each try
  is taken in the database, marked with the process that has it in hand and its pid, so the two never both make it.
  One run by the other process is answered as a message, through the reminders table, that the one which answers
  there gives once it's up: asked aloud and run by Telegram while the assistant was down, it's said aloud when the
  assistant is back; with [presence], one to be said aloud for someone who's out goes to their chat instead, like
  any message ([reminders](reminders.md)).
- **It happened only when the house says so.** A change counts as done only when the device reports it ("done",
  "sent", "started": [the house](home.md)); "not confirmed", an error or a refusal means it didn't happen, and so
  does a turn that failed before anything was done. A try that didn't happen isn't said: there's no "Done" for an
  AC that didn't come on. A turn that failed after the house made its changes is kept as done, and whoever asked is
  told what the house made, since doing it again could undo it (a remote's power button pressed twice); and one done
  whose answer can't be given (the voice or Telegram unreachable) is kept as done, its answer given with the
  reminders.
- **Tried again, then given up.** A try that didn't happen is tried again two minutes later, three tries at most, all
  within fifteen minutes of the first; each try is told how the last one went, so it does only what isn't done yet.
  A refusal is final at once (unlocking stays a trusted voice's). Then it's given up: kept in `skill_runs` as failed,
  and whoever asked is told, where they asked, as a message: "What you asked me to do at 5:00 AM didn't happen (the
  ac on: not confirmed: …): Turn the AC back on: cool, 23°. Ask me again if you still want it."
- **TARS stopping** in the middle of a try counts as that try, and it didn't happen: back within the fifteen
  minutes, it's tried again; back later, it's given up and they're told, never done hours after it was tried. One
  that was never tried, because TARS was off at its time, runs as soon as TARS is back, told how late it is, so it
  can leave what no longer makes sense this late, and say so.
- **Once it's done**, or given up, it's gone; its run is kept like any other's. Saving it again ("make it six")
  starts its tries afresh, and a try still in hand for what it was before changes nothing.

Kept in the `skills` table: `once`, when it runs, and `tries`, its tries so far and who has one in hand; a run that
didn't happen is `failed` in `skill_runs`. The messages are said and posted by the reminders, so they need
`[reminders]`, which is on unless it's turned off.

## Skills from config.toml

Skills used to be `[[home.skills]]` in `config.local.toml`. TARS won't start with them there now; import them once,
as the household's, and delete them from the file:

```bash
uv run python -m voice_assistant.skills import
```
