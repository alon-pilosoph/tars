# Reminders, timers and messages

TARS sets timers, reminds someone of something at a time, and passes messages from one person to another, by voice
or from the web UI. Most of them wait for an acknowledgement: TARS says it again until someone says "got it", up to a
limit, and the web UI shows who acknowledged it and how.

Three rules shape the design: a reminder is **never said over a conversation**, "got it" is **understood, not
matched** against a phrase list, and **times are computed by TARS, not the model**, so a misheard time is heard back
at once. With `[reminders] enabled = false`, or in push-to-talk mode, there are none, and TARS says it can't set
timers or reminders (`CANT_RULES` in `llm.py`) instead of pretending it did.

## What it does

| Kind | Said by voice | Spoken when it's due |
|---|---|---|
| **Timer** | "set a pasta timer for twelve minutes" | "Your pasta timer is done.", then it rings until turned off |
| **Reminder** | "remind me to call the bank at nine" | "Alon, a reminder: call the bank." |
| **Message** | "tell Stacey dinner's at eight, at seven thirty" | "Stacey, a message from Alon: dinner's at eight." |
| **Message when they're back** | "tell Stacey the plumber called when she's back" | the next time TARS recognizes Stacey's voice, after answering her |

By voice, also: "what reminders do I have?", "cancel the pasta timer", "remind me again in ten minutes" (snooze; a
missed one can be snoozed too).

### Who it's for

A reminder or message can be for anyone, by name. TARS starts with the name ("Stacey, …"), so it works for people
TARS has no voiceprint for. A message says who it's from: the voice that set it, or the name typed on the web UI.
Without a name, it's for whoever is there. **Waiting until they're back** needs that person's voiceprint (a named
voice); without one, TARS asks for a time instead, and the web UI won't set it.

### Acknowledging

Anyone can acknowledge anything; the status records who.

- **Right after it's said**, TARS listens for a few seconds without the wake word: "got it", "okay, thanks", "yep";
  for a timer, "stop" or "turn it off".
- **Later:** "hey TARS, I got the message" acknowledges the one that's waiting (the most recent, if there are
  several and it isn't clear which).
- **On the web UI:** Got it, or Turn off for a ringing timer.
- **Who** is recorded as the recognized voice, "an unknown voice", or "on this page".

### When it's said

- **A chime** comes before each one, so the first words don't land in silence.
- **Repeating.** A reminder or message that waits for an acknowledgement is said again every 5 minutes, at most 4
  times, then it's **Missed** (both can be changed for each reminder; the defaults are in `config.toml`). One that
  doesn't wait is said once.
- **Timers ring** like a kitchen timer until someone turns them off: a chime every 10 seconds, with "Your pasta
  timer is done." on the first ring and about once a minute after, for at most 15 minutes (`timer_ring_min`), then
  Missed. TARS can't hear while it makes a sound, so it rings in bursts and listens in between: "stop" works without
  the wake word, and "hey TARS, turn the timer off" any time.
- **Never over a conversation.** If something comes due while someone is talking to TARS, it waits until the
  conversation is over. (Interrupting needs ["TARS stop"](roadmap.md#2-tars-stop-interrupting-a-reply).)
- **Late ones say so.** Anything that came due while TARS was off, or in a long conversation, is said as soon as it
  can be, with "This was due at 8:00." A snoozed one is on time at its new time.
- **When the voice is down**, only the chime plays; the reminder isn't counted as said, and it's tried again a
  minute later.

### On the web UI

The **Reminders** page sets them (kind, text, for whom, from whom, when, whether to say it again until someone says
got it, and how often), and shows each one's status: due, ringing, said 2 of 4 times, acknowledged (by whom, how,
when), missed, or stopped; with Got it (Turn off for a ringing timer), Again in 10 min, and Stop. Away from home the
page is reachable over Tailscale, so that's how they're set remotely. It has no accounts, so a message set there asks
who it's from, and an acknowledgement made there shows as "on this page". A reminder that was said, and what was said
back, show on Home like any conversation.

## Design

**One table, three users.** A `reminders` table in the event database: what, for and from whom, when (`due`; empty
for "when they're back"), whether it waits for an acknowledgement, the repeat settings, and its state: `status`
(scheduled, waiting, acknowledged, said, missed, cancelled), `tries`, `next_at` (when it's next said), `last_said`,
and `acked_at` / `acked_by` / `acked_via`. `Reminders` in `reminders.py` owns it: creating, what's due, what to say,
acknowledging, snoozing, stopping. The assistant says them, the brain's tools set them, and the web UI does both,
each through that class. The web UI is another process; the assistant reads the table every 2 s, so a change on the
page is picked up within that.

**Interrupting the wait, not a conversation.** The wake loop (`VerifiedTrigger.wait`) asks once per block of mic
audio (80 ms) whether anything is due, and returns `DUE` if so. `run_forever` then says it: the chime, each due line
(made ahead, as soon as the reminder is set, so it plays at once), then, for any that wait, a few seconds of
listening without the wake word. While a conversation is going on nothing asks, so the "never over a conversation"
rule needs no code of its own. What's said back answers the reminder, never the last wake, so it can't turn a
rejected wake into a "real" one for training.

**When the table fails** (a full SD card, a damaged file), what it says is due would stay due, and the wake loop
stops the moment something is. So the clock that answers the wake loop keeps its own holds: a reminder it couldn't
mark as said waits out its repeat time anyway, and after a failed read nothing is due for 30 s. TARS neither repeats
a line over and over nor goes deaf to "hey TARS".

**Acknowledgement is the model's call.** What's said after a reminder goes to the brain tagged as a reply to it,
like the reply to "Did you call me?", with a marker next to `<skip>`: `<ack>` when it acknowledges it, optionally
followed by a short spoken line ("Got it." when there's none). Anything else carries on as a conversation ("remind me
again in ten minutes"). Every active reminder is in both models' instructions by number (after the part that can be
cached, like the time of day), so "what reminders do I have?" needs no tool, and a later "hey TARS, I got the
message" gets `<ack 12>`. **Why not a phrase list:** people say "okay thanks" a hundred ways, and a list would miss
them.

**Setting by voice** uses three tools (`remind`, `snooze_reminder`, `cancel_reminder`). Qwen has them itself
(`[llm] quick_tools`, on by default); with it off, they're OpenAI's, and Qwen hands those turns over with
`<look-up>`, which costs a second or two (see [the brain](architecture.md#the-llms-tools)). Like sent items, a tool
call changes nothing until the turn is kept: a draft thrown away because you kept talking leaves no reminder behind.

**Times are worked out by TARS, not the model.** The model gives minutes from now, or a clock time (`HH:MM`) and the
day as it was said: `today`, `tomorrow`, a weekday, a date, or none for the next time the clock shows it.
`reminders.due_at` turns that into the moment, so "Friday" over a month's end can't come out wrong. The tool's
answer says when it's set for ("tomorrow at 9:00 AM"), and TARS confirms with that, so a misheard time is heard
straight away. It turns down the past and anything over a year away, so the model asks again. The
[quick-model benchmark](models.md) checks every reminder against `due_at` to the minute.

**Waiting until they're back** hooks into the conversation: after TARS answers someone whose voice matches (speaker
ID's confident match), any message waiting for them is said ("By the way, Stacey, a message from Alon: …"), and the
follow-up listening that comes next is its acknowledgement window.

## Settings

In `config.toml`'s `[reminders]`: `enabled`, `repeat_every_min = 5`, `max_tries = 4`, `timer_ring_min = 15`,
`ack_window_s = 6`. Push-to-talk and typed (`--text`) modes don't say reminders.

## Not built yet

Quiet hours (nothing spoken overnight), messages shown only on the web UI and never read aloud, recurring reminders
("every Tuesday"), and a fallback time for "when she's back".
