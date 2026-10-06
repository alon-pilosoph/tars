# Reminders, timers and messages

TARS can set a timer, remind someone of something at a time, or pass a message from one person to another, by voice
or from the web UI. Anything can wait for an acknowledgement: until someone says "got it", TARS says it again every
couple of minutes, up to a limit, and the web UI shows who acknowledged it and how.

**Status:** planned, being built on the `reminders` branch. Until it ships, TARS says it can't set timers or
reminders (`CANT_RULES` in `llm.py`) instead of pretending it did.

## What it does

| Kind | Said by voice | Spoken when it's due |
|---|---|---|
| **Timer** | "set a pasta timer for twelve minutes" | "Your pasta timer is done." |
| **Reminder** | "remind me to call the bank at nine" | "Alon, a reminder: call the bank." |
| **Message** | "tell Stacey dinner's at eight, at seven thirty" | "Stacey, a message from Alon: dinner's at eight." |
| **Message when they're back** | "tell Stacey the plumber called when she's back" | the next time TARS recognizes Stacey's voice, after answering her |

- **For a person.** A reminder or message can be for anyone, by name. TARS starts with the name ("Stacey, …"), so
  it works for people TARS has no voiceprint for. A message says who it's from: the voice that set it, or the name
  picked on the web UI. Without a name, it's for whoever is there.
- **Waiting until they're back** needs that person's voiceprint (a named voice). Without one, TARS asks for a time
  instead, and the web UI doesn't offer it.
- **Acknowledging.** Anyone can acknowledge anything; the status records who.
  - Right after it's said, TARS listens for a few seconds without the wake word: "got it", "okay, thanks", "yep".
  - Later: "hey TARS, I got the message" clears the reminder that's waiting (the most recent one, if there are
    several and it isn't clear which).
  - On the web UI: Acknowledge.
  - Who: the recognized voice, "an unknown voice", or "on the web UI".
- **Repeating.** One that waits for an acknowledgement is said again every 2 minutes, at most 10 times, then it's
  **Missed** (both changeable per reminder; defaults in `config.toml`). One that doesn't wait is said once.
- **By voice, also:** "what reminders do I have?", "cancel the pasta timer", "remind me again in ten minutes"
  (snooze).
- **A chime** before each one, so the first words don't land in silence.
- **Never over a conversation.** If something comes due while someone is talking to TARS, it waits until the
  conversation is over. (Interrupting needs ["TARS stop"](roadmap.md#2-tars-stop-interrupting-a-reply).)
- **After downtime.** Anything that came due while TARS was off is said when it starts again, with "This was due at
  8:00."
- **The web UI's Reminders page** sets them (kind, text, for whom, from whom, when, wait for an acknowledgement,
  repeat), and shows each one's status: scheduled, waiting (said 3 of 10 times, next at 9:14), acknowledged (by whom,
  how, when), missed, or cancelled; with Acknowledge, Snooze and Cancel. Away from home, the page is reachable over
  Tailscale, so that's how they're set remotely.
- **In the conversations.** A reminder that was said, and what was said back, show on Home like any conversation.

Later, not in the first version: quiet hours (nothing spoken overnight), messages shown only on the web UI and never
read aloud, recurring reminders ("every Tuesday"), a fallback time for "when she's back".

## Design

**One table, three users.** A `reminders` table in the event database (migration 3): what, for and from whom, when
(`due`, or `when_heard` for "when they're back"), whether it waits for an acknowledgement, the repeat settings, and
its state: `status` (scheduled, waiting, done, missed, cancelled), `tries`, `next_at`, `last_said`, and
`acked_at` / `acked_by` / `acked_via`. A `Reminders` class in `reminders.py` owns it: creating, what's due, what to
say, acknowledging, snoozing, cancelling. The assistant delivers, the brain's tools set them, and the web UI does
both, each through that class. The web UI is a separate process; the assistant reads the table every 2 s, so a
change on the page is picked up within that.

**Interrupting the wait, not a conversation.** The wake loop (`VerifiedTrigger.wait`) checks once per mic block
(80 ms) whether anything is due and returns `DUE` if so. `run_forever` then delivers: the chime, each due line
(made when the reminder is set and kept on disk like the other short lines, so it plays at once and even offline),
then, for any that wait, a few seconds of listening without the wake word. While a conversation is going on, nothing
checks, which is the "never over a conversation" rule for free.

**Acknowledgement is the model's call.** What's said after a reminder goes to the brain tagged as a reply to it, like
the reply to "Did you call me?", with a new marker next to `<skip>`: `<ack>` when it acknowledges, optionally
followed by a short spoken line. Anything else carries on as a conversation ("remind me again in ten minutes").
For a later "hey TARS, I got the message", the reminders waiting for an acknowledgement go into both models'
instructions (after the cached part, like the time of day), and the brain answers with `<ack 12>`. A model can say
"okay thanks" a hundred ways; a phrase list would miss them.

**Setting by voice goes through OpenAI.** Setting, listing, snoozing and cancelling are tools on OpenAI's model,
and Qwen hands those turns over with `<look-up>`, as it does for sending. That costs a second or two on those turns
only. Like sent items, a tool call doesn't change anything until the turn is kept: a draft thrown away because you
kept talking leaves no reminder behind. Listing reads the table directly. Times are given either as minutes from
now or a local date and time; the tool rejects the past and anything over a year away, so the model asks again.

**Waiting until they're back** hooks into the conversation: after TARS answers someone whose voice matches (speaker
ID's confident match), any message waiting for them is said ("By the way, Stacey, a message from Alon: …") and the
follow-up listening that comes next is its acknowledgement window.

**Settings** in `config.toml`'s new `[reminders]`: `enabled`, `repeat_every_min = 2`, `max_tries = 10`,
`ack_window_s`. Push-to-talk and typed (`--text`) modes don't deliver reminders.

## Building it

Each stage has its tests and is committed on its own:

1. **The table and `Reminders`:** the migration, creating, due times, what to say (names, sender, overdue),
   repeats and Missed, acknowledging, snoozing, cancelling.
2. **Delivery:** the wake loop's `DUE`, the chime, the ack window and `<ack>`, repeating, after downtime.
3. **By voice:** the tools, changes applied only once the turn is kept, Qwen's hand-off, `<ack N>` from the
   waiting list, `CANT_RULES` no longer ruling out timers and reminders.
4. **When they're back.**
5. **The web UI:** the Reminders page, its API, demo data, end-to-end and screenshot tests.
6. **Docs:** this page's status, the architecture and web UI pages, the roadmap.

**Measure on the Pi:** that "got it" and its variants are taken as acknowledgements and other replies aren't (a
dozen of each), that nothing is said in the middle of a conversation, and that a reminder set from the phone over
Tailscale is said within a few seconds of its time.
