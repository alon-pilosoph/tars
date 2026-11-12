# What's next

Six steps, in this order. Real use comes first because it costs no code, runs alongside the rest, and everything
after learns from it. Security is next: the web UI's sign-in is built but not yet on at home, and the steps after it
widen what TARS does with nobody watching (plugins run others' code, automations act on their own), so what they may
do is settled first.
Interrupting starts with one measurement on the speakerphone, which decides how big it is. Plugins and updates come
before automations and skills, which hook into the events plugins bring. Memory is last, and leans on speaker ID
being right.

| | Step | Why now | Size |
|---|---|---|---|
| 1 | [A week of real use](#1-a-week-of-real-use) | the first real wakes, voices and conversations; everything after learns from them | no code, a checklist |
| 2 | [Security](#2-security) | anyone at home can open the web UI until sign-in is on, and steps 4 and 5 widen what TARS can do unwatched | medium, in parts; passkey sign-in built |
| 3 | ["TARS stop": interrupting a reply](#3-tars-stop-interrupting-a-reply) | the one daily annoyance left; the speakerphone may not allow it | a measurement, then medium or hardware |
| 4 | [Plugins and updates](#4-plugins-and-updates) | plugins only add devices today, and updating takes SSH | large |
| 5 | [Automations and skills](#5-automations-and-skills) | "every time someone gets home…", taught in plain words | mostly built; dry runs for devices built; the arrival greeting left |
| 6 | [Long-term memory and personalization](#6-long-term-memory-and-personalization) | the biggest one for conversations, and it leans on speaker ID | preferences built; facts, scopes and the page left |

Planned alongside the steps: [house commands on the Pi](#planned-house-commands-on-the-pi), small and independent of
them; [wishes that come true](#planned-wishes-that-come-true), where something TARS couldn't do becomes a pull request
and, once merged, a message to whoever asked; and [noticing routines](#planned-noticing-routines), where TARS learns
the household's habits from Home Assistant's history and offers to take them over. Benched for now: [learning from
how conversations go](#benched-learning-from-how-conversations-go). Later, after the six: [working
offline](#later-working-offline), [a brain anywhere, a body at home](#later-a-brain-anywhere-a-body-at-home) (TARS
itself off the Pi, the house's own hardware a small part that can fail without losing TARS) and [TARS in other
homes](#later-tars-in-other-homes).

**Built so far, alongside step 1:** [reminders, timers and messages](reminders.md), [the house](home.md) (Home
Assistant's devices, an Android TV box and the agent that works it, plugins for the rest), [TARS on
Telegram](telegram.md) (chats and voice messages, with reminders and what TARS sends on a phone), the web UI's
[Status page](web-ui.md#the-status-page), the [backup to S3](backup.md), [who's home](presence.md) (phones on the
Wi-Fi, from the routers), [TARS's browser](browser.md) (errands on any website, nothing ordered without a tap) and
[handing hard questions to a larger model](#built-handing-hard-questions-to-a-larger-model). Since then: [skills and
preferences](skills.md) taught by talking, run at set times, when someone comes or goes, when a device changes or
when TARS is updated, with a Skills page; [feeds](feeds.md) per person; Spotify, live TV channels and a camera in
[the house](home.md); [all of Home Assistant](home.md#all-of-home-assistant) (any entity, its history and logbook,
any service, the unlock rules kept), with [its setup versioned](home.md#home-assistants-own-setup) and sealed in the
backup; and TARS [knowing itself](architecture.md) (its version, what's new, its docs, its records and log), changing
its own settings when asked, combining what it has before saying "can't", and noting real gaps as wishes. What's left for each is
in its own doc: the house's "Not yet" under [things to know](home.md#things-to-know), [Telegram's](telegram.md#not-yet),
[who's home's](presence.md#not-yet), [the browser's](browser.md#not-yet), and the web UI's
[limitations](web-ui.md#limitations).

## 1. A week of real use

**Goal:** data, and a list of what actually goes wrong. With TARS on the Pi and the speakerphone, use it for a week,
starting with the 20-minute [first test run](deployment.md#a-first-test-run). No new code.

**Before starting:** the speakerphone is named in `input_device` and `output_device` (in the Pi's
`config.local.toml`; `uv run voice-assistant --list-devices` lists the names), and `--mic-test` from across the room
sounds right. Speaker ID has no voiceprints until someone names their voice in the Voices tab, which offers it after 5
requests, or records a [voice session](web-ui.md#recording-sessions) on the speakerphone
(`--record-session voice --for alon`). Until then nobody is greeted by name or can unlock by voice.

**What to track:**

| What | Where it shows | Good enough |
|---|---|---|
| Missed "hey TARS" | near-misses in Review, and your own notes | rare in a quiet room |
| False wakes (TV, talk) | Review | at most one or two a day, none answered |
| Wrong names | Home, a conversation's "who was talking" | rare once voiceprints come from the speakerphone |
| Cut off mid-sentence | your notes | never |
| Slow replies | under each answer in Home, with the model that wrote it (and `timings` in the `turns` table) | about 2 s in use on the Pi (1.2-1.5 s on a benchmark; [response time](latency.md)) |
| Turns Cerebras couldn't take | `answered_by = 'fallback'` in the `turns` table | rare |
| Hard questions handed over | `answered_by = 'ponder'` in the `turns` table, and the time to "Let me think" | rare, never for a simple question, and "Let me think" as quick as any first sound |
| Failed answers | Home, in red: where it failed (speech to text, the answer, the voice) and why | rare, and no one stage over and over |
| Answers you'd rate bad | Home's good / bad buttons | a list, for step 6 and the benched step |
| The house: a change not made, or not asked for | the conversation in Home, and `journalctl --user-unit voice-assistant \| grep -E 'home:\|tv:'` | none; "done" only when the device reported it, and the door unlocked only for a trusted voice that asked |
| TV tasks that didn't finish | `tv:` lines in the same log (`done` or `not done`), and what TARS said after the chime | most finish; one that doesn't says why |
| Telegram | the chats, and their conversations in Home | reminders posted on time, voice messages written down right |
| Browser errands | the chat, and `journalctl --user-unit voice-assistant-telegram` | most finish; every order stops for a tap, with the right total |
| Who's home | "who's home?", and a reminder that went to a chat because someone was out | right within the ten minutes it allows |
| Backup passes | `--check`'s Backup line, and `journalctl --user-unit voice-assistant-backup` | every pass good; `--check` warns when the last good one is over an hour old |
| A part that stopped | the Status page | none, or restarted by systemd |

**At the end of the week:** answer what's left in Review and name the voices. The Models page then shows how many
real, missed and not-real wakes there are to train on (`training.household`, which installs a pair only if it does
better; the optional large pair, `models/generic-large`, is the one to start from if it wins on the week's wakes).
Then check the order below still holds: if missed wakes, false wakes or wrong names are the real problem, they come
first.

## 2. Security

**Goal:** a written threat model for an always-on mic wired to an LLM that works the house and a browser, each threat
with its defense and a test that shows the defense holds. Not "impenetrable": what's covered, how it's checked, and
what's accepted as a risk.

**Where it stands.** Nothing at home is opened to the internet (Telegram is polled, and only `[telegram] people` get
answers). The mic's audio stays on the machine until a wake is double-checked. The web UI only answers to
home-network names and only takes changes from its own page ([web UI](web-ui.md)), which stops a page elsewhere
reaching it through someone's browser. Unlocking takes a trusted voice saying "unlock", never typed text. The browser
never opens an address at home, keeps passwords out of what the model reads, and code, not the model, holds every
button that orders or pays until the person taps Order ([browser](browser.md#never-committing-the-person)); its vault
is encrypted, with the key beside it. `.env` is for TARS's user alone, and a reply that read web search results
changes nothing in the house, nor reads its past or TARS's records. Home Assistant's setup, with its integrations'
passwords, goes to the backup only sealed with a key kept on the Pi. The speakerphone's own mute is TARS's: while
it's red, the wake model doesn't run.

What's open: until passkey sign-in is turned on at home, anyone on the home network (a guest, a compromised device)
can play every recording and read every conversation. The browser's one-time login page is plain HTTP. A plugin runs
inside TARS with everything TARS has, `context.secret()` gives it any key in `.env`, and nothing says which plugins
are trusted. The systemd services run unconfined.

**Plan, most worth it first:**

1. **The threat model.** Written: [security](security.md) lists who might attack, the defense and test for each,
   and what's open, and [SECURITY.md](../SECURITY.md) says how to report a vulnerability. It's kept current as each
   item below lands.
2. **The web UI.** Built: passkey sign-in per household member from one-time invites, HttpOnly, SameSite=Strict
   session cookies, HTTPS from `tailscale cert`, and a test that walks every route and fails if one answers without a
   session ([signing in](web-ui.md#signing-in)). Left: turning it on at home, the browser's login page on HTTPS too,
   and serving on the tailnet only, not the LAN, with Tailscale's access rules as the outer wall.
3. **The AI's own risks.** Started: a reply that read the web changes nothing and reads no one's past, and a chat
   reads only its own person's conversations. Still: no text from outside (web search results, a web page the browser reads, Telegram, a sent
   file) can lead to an unlock, turning off an alarm or sending data out; an eval set of injection attempts that
   must all fail (it calls the models: cost estimate first). No tool lets a model read the recordings, `.env` or
   someone else's conversations. Against a replayed or cloned voice: built, three random digits said back by the same
   voice before unlocking ([unlocking](home.md#unlocking)).
4. **What plugins and automations may do.** Each declares what it needs (the secrets by name, the devices, network
   access) and gets only that from `context`; the actions that matter most (unlock, send something out of the house,
   order) are never theirs unless granted by name. Step 4 builds on this; step 5's rules go through the same checks.
5. **The Pi.** SSH only over Tailscale, keys only; a firewall that drops anything incoming by default; automatic
   security updates. systemd sandboxing for each service (NoNewPrivileges, ProtectSystem=strict, PrivateTmp,
   RestrictAddressFamilies, IPAddressDeny for the web UI), the web UI as its own user that can't read `.env` or reach
   the internet. `.env` mode 600; each key as narrow as its service allows, with spending caps; a Home Assistant user
   limited to the listed devices.
6. **Data.** A shorter `keep_audio_days`; backups encrypted before they leave. The Pi's key has to read the bucket
   (the web UI plays recordings that only the bucket still has, and a restore reads it) and delete from it (the
   backup mirrors deletes), so it can't be a write-only key: instead, versioning on the bucket, keeping deleted and
   overwritten files for a while, and a key that can't delete old versions or change that rule, so a stolen key
   can't wipe the backup. Disk encryption on a Pi with no secure key store is weak (the key has to be on it to boot
   unattended): written down as an accepted risk.
7. **Supply chain.** Built: pip-audit, `npm audit` and bandit fail CI, and each run keeps SBOMs (Python and web
   UI), alongside `uv.lock`'s hashes ([security](security.md#the-code-tars-runs-its-dependencies)). Left: a scheduled
   audit between pushes, and signed releases for step 4's updates.
8. **The mic itself.** A hardware mute switch (built: the speakerphone's mute stops TARS listening at all), and a
   light while audio is leaving the house.

**Done when** each threat in `docs/security.md` has a defense and a passing test (or is written down as accepted),
and a self-run pentest of the web UI and the Pi finds nothing new.

## 3. "TARS stop": interrupting a reply

**Goal:** say "hey TARS" (later, "TARS stop") while TARS is answering, and it stops.

**Where it stands.** TARS listens while it greets you, but not while it answers:

- **The greeting is heard over.** With `[audio] echo_cancel = true` and `[recorder] greet = "always"`, "Yes,
  <name>?" plays with the mic open: WebRTC's echo canceller (`echo.py`) takes TARS's sound out of what the mic hears,
  and any trace of the greeting left in the transcript is taken off its start (`without_echo`). Both settings are off
  by default, since they haven't been measured in the room (`tools/echo_bench.py`).
- **Answers aren't.** Everything else TARS says, answers, reminders and announcements, plays with the mic paused
  (`Microphone.paused()` in `assistant.py`), so nothing can interrupt it.
- **A "TARS stop" model** is trained, with "stop" on its own and in sentences as negatives. It catches 99% of the
  held-out voices in quiet and 87% at 5 dB of TV, and 76% of the owner's held-out takes across all 8 conditions,
  with no false answers in an hour of TV or audiobooks and no false interrupts in 23 minutes of TARS's own replies
  ([wake word](wake-word.md#tars-stop)). With TARS's voice still in the signal it catches about half. It isn't in
  use yet.

**What was tried, and why the speakerphone is the question.** The PowerConf is half-duplex: it turns its own mic
down while its speaker plays. Keeping the mic on through "Yes?" with only the PowerConf's own echo cancellation let
part of the greeting through, which speech-to-text heard as "Yes." and took as the whole request; that was reverted
for TARS's own canceller. And in a simulation, WebRTC's canceller cut hard into speech said over TARS ("what's the
weather" came through as "the were"), where a neural canceller or a microphone array would do better.

**Plan:**

1. **Measure first.** `tools/echo_bench.py` on the PowerConf, in the room: what the mic still hears of "hey TARS"
   said over an answer, with and without the TV. If the speakerphone mutes its mic during playback, no canceller can
   help, and the choices are a full-duplex speakerphone or mic array, or a button (the PowerConf's call button, read
   over HID, as "stop").
2. **Listen while speaking.** If it hears enough: keep the mic open during answers too, and run the wake model on
   it.
3. **"hey TARS" as the interrupt first.** It's the phrase that already works, and it means what people expect: said
   in the middle of an answer, it stops it and listens for a new request. The double-check still has to agree, so a
   lookalike in TARS's own answer doesn't cut it off.
4. **On an interrupt:** stop the playback, the reply (`StreamedReply.stop()`) and the brain (`brain.interrupt()`),
   all of which exist; keep what was already said in the conversation, marked as cut off; then record the new
   request, with no greeting.
5. **Then "TARS stop",** tested with TARS talking and echo cancellation on, and on the owner's own takes. A bare stop
   (no new request) is its own action: cut the reply and go back to waiting.

**Measure before turning it on:** false interrupts per hour of TARS talking (an hour of its own answers through the
speakerphone, with and without the TV on), and how long from the phrase to silence. **Done when** false interrupts
are about zero and it stops within half a second.

**Open question:** whether the Pi can run the wake model and the voice's playback at once without glitches. It
should (the wake model is tiny), but it's the first time both run together.

## 4. Plugins and updates

**Goal:** someone else's code can add to TARS without forking it, and TARS and its plugins update themselves safely.

**Where it stands.** A plugin is a Python module named in `[home.plugins]` whose `setup(settings, context)` returns
kinds of device for the house tool, with `BACKUP_FILES` for what it learned
([your own devices](home.md#your-own-devices)); `examples/my_house` is the household's. It can only add devices: no
tool of its own, nothing it reacts to, and no version. Updating is `git pull && deploy/install-pi.sh` over SSH
([deployment](deployment.md)); the database upgrades itself.

**Plan:**

1. **Plugins beyond devices.** A plugin may also add a tool the brain can call, events others can react to ("someone
   unlocked the door", "the washing machine finished"), and `--check` lines, besides devices. Built-in parts send
   events the same way: who's home (arrived, left), a device's state changing, a time of day.
2. **Declared, versioned, granted.** Each plugin says its name, version, what it adds and what it needs (step 2's
   item 4); TARS loads only what's granted in config, and `--check` lists each plugin, its version and what it may
   do. Installed as a package or from a git URL, pinned.
3. **`examples/my_house` moves onto it first,** which shows the plugin interface covers a real household.
4. **Updates.** Built from the branch rather than releases ([deployment](deployment.md#updates)): what's merged into
   `main` installs itself at a quiet moment, checked silently, and is taken back with its database and config if it
   fails; one that changes the database waits for a maintainer's tap, and one that needs sudo is left to them. Left:
   tagged, signed releases with a changelog, the Status page saying an update is waiting, and plugins updating the
   same way.

**Later:** TARS writing a plugin itself when asked for something it can't do, tested in a sandbox against recorded
events and opened as a pull request for a person to review: planned now as
[wishes that come true](#planned-wishes-that-come-true).

**Done when** `my_house` runs as a declared plugin with only the grants it needs, an update and a rollback have both
been done on the Pi without SSH, and a plugin that asks for more than it was granted fails to load.

## 5. Automations and skills

**Goal:** "when Stacey gets home, tell her what's waiting", "turn the boiler off if nobody's home", "movie night",
said in plain words, and done the same way every time.

**Built** ([skills](skills.md)): everything is a skill, plain words for the model carried out with its tools; only
the trigger is code: a time (with days), someone coming home or going out, a device's status or reading, or TARS
being updated. Asking for one is just done and agreed to in plain words ("Will do: I'll message you whenever Alon
adds something new"), never called a skill, said in the chat it was asked in or aloud. Each person's preferences are
kept the same way. "What did you do today?" is answered from the runs kept, and the web UI's Skills page lists every
skill and preference, where it came from and what it did. Limits are in code: unlocking stays a trusted voice saying
"unlock", an order still needs its tap.

**Left:**

- **Dry runs.** Built for device triggers: replayed over the device's last week of Home Assistant history before
  it's kept, and said only when it matters ("that would have gone off 30 times last week")
  ([skills](skills.md)). Left: who's home keeps no history yet, so coming-and-going triggers aren't dry-run.
- **Editing on the page.** Pausing, changing and deleting from the Skills page needs step 2's sign-in first.
- **Habits offered**, not only asked for: [noticing routines](#planned-noticing-routines).

**The first one:** the arrival greeting. The Nuki's own events say who unlocked; with who's home, TARS greets them by
name as they come in and says what's waiting ([who's home, not yet](presence.md#not-yet)).

**Measure:** a set of phrasings turned into rules, checked for the rule they should make; dry runs over recorded
event logs; and a rule built to unlock or order must be refused. **Done when** the arrival greeting and three
household rules have run for a week with no wrong run.

## 6. Long-term memory and personalization

**Goal:** TARS remembers things that matter over time, per person: "Stacey is vegetarian", "Alon's standup is at
9:30", "the car is a 2019 Corolla". It can offer to remember something, anyone can say "remember this" or "forget
that", and the web UI shows exactly what it keeps, where it can be edited or deleted. Personalization is the same
page: each person's settings, and the persona part of the system prompt.

**Where it stands.** Half of it is built as preferences ([skills](skills.md)): what someone likes or doesn't, kept per
person in the `preferences` table, said by them or noticed by the model and marked as a guess, told on every request
of theirs, and "what do you know about me?", "forget that" answered by talking; the Skills page shows them. TARS's
settings change by asking too (humor, how long it remembers, its voice), but for the whole household, not per
person. Left: facts as well as likes ("the car is a 2019 Corolla"), the household's own memories, using a person's
only when speaker ID is sure (no voiceprints are enrolled yet, so by voice nobody is), editing on the page, and
settings per person. The design below still holds for those; the `memories` table may simply be `preferences`
grown.

**Design:**

- **Storage:** a `memories` table in the event database (`store.py`, with the usual automatic upgrade): the text,
  whose it is (a person, by the same names speaker ID uses, or the household), the conversation turn it came from,
  and when it was made and last changed.
- **Writing:** `remember` and `forget` tools, given to Qwen with its reminder and send tools (`[llm] quick_tools`),
  so those turns skip the hand-off; with `quick_tools` off they go to OpenAI's model the way `<look-up>` turns do.
  TARS offers only for lasting facts and preferences, at most once a conversation, and never for anything said to
  someone else. The call doesn't hold up the reply: TARS speaks what the model said and saves in the background,
  without another round to the model.
- **Reading:** everything kept for the person speaking, plus the household's, goes into the prompt as a short
  "what you know" block. No search system: a household's memories fit in the prompt for a long time (with a cap
  and a warning well before they don't).
- **Only when sure who's talking.** Personal memories are used only when speaker ID's match is confident;
  otherwise only the household's. A wrong guess would put one person's memories in another's conversation, which
  is why step 1 checks speaker ID on the speakerphone first.
- **A Memory page in the web UI:** per person and for the household, every memory with where it came from (a
  link to the conversation), edit, delete, and add by hand. Deleting really deletes.
- **Personalization:** each person's settings on the same page, starting with humor (overriding `[llm] humor`).
  The system prompt splits in two: the persona (who TARS is, how it talks), editable there, with `config.toml`'s
  as the default; and the rules the code depends on (skipping overheard chatter, the send tool, memory), which stay
  in the code, so the page can't break them.

**Measure:** a set of scripted conversations (tell it a fact, ask about it days later, a second voice asking the
same, "forget that"), run through `--text` and through the speakerphone, checking what's kept, what's used, and that
one person's memory never reaches another's answer.

**Open question:** what TARS offers to remember. Too eager and it's a nag; the week of real use will show which
facts come up again.

## Planned: wishes that come true

**Goal:** Stacey asks for something TARS can't do yet. A week later TARS tells her, unprompted: "Remember you asked me
to read you the kindergarten's WhatsApp updates? I can now." In between, the only person who did anything was the one
who reviewed a pull request.

**What's there already:** the `tars` tool notes a real "can't" as a wish, with what stopped TARS and what would make
it possible ([architecture](architecture.md)); skills can run when TARS is updated, told what changed
([skills](skills.md)); and `changes` tells what's new from the checkout's own history.

**Design:**

1. **A wish goes to TARS's maintainers** (`[household] maintainers`: built, each wish is already posted to their
   chats as it's noted), in plain words, and asks: "Stacey asked me to read her the kindergarten's WhatsApp updates;
   I can't reach WhatsApp. Want me to have it built?" With a cost guess, since building isn't free. Nothing is built
   without that yes.
2. **On a yes, a coding agent builds it:** a cloud Claude Code session on the repository, started by TARS with the
   wish, what stopped it, the docs, and the rules the repository keeps (its tests, its docs, one pull request,
   household specifics only in `examples/`). It never gets `.env`, `config.local.toml` or the database: what it knows
   of the house is the wish's sentence. It opens a pull request and stops; it can't merge.
3. **TARS follows the pull request,** and tells the maintainer in their chat what it does, in plain words, when CI is
   green; a failing run is sent back to the session to fix, a few times at most.
4. **Merged is deployed:** until step 4's updates, a timer on the Pi pulls `main` and runs `deploy/install-pi.sh`
   between conversations when it moved, and `--check` decides whether it stays (or goes back).
5. **Whoever asked is told.** The wish is marked granted by the pull request that answered it; the next start, which
   is an update, runs the person's own "tell me what's new" or, without one, says it to them once, in their chat or
   aloud: what they can now ask for, never how.

**Limits held in code:** only the maintainer's yes starts a build; the session's repository access is the only
access it has (no keys, no house); nothing merges itself; the wish text sent out is a sentence, not the chat it came
from; a build that fails three times is reported, not retried forever; and a monthly cost cap.

**Measure:** a set of wishes of different sizes (a new feed source, a new device kind, a new tool), each run through
to a pull request on a branch, judged by whether it passes CI, follows the repository's rules, and does what was
wished. **Done when** a real wish from the household has gone from "I can't" to "I can now" with nobody touching the
code but the reviewer.

## Planned: noticing routines

**Goal:** TARS notices what the household does by habit and offers to take it over ("You put the boiler on most
weekdays around six. Want me to just do that?"), and notices what's out of the ordinary ("The door's been unlocked
for an hour and nobody's home"). Saying yes makes it a skill, invisibly.

**The data:** Home Assistant keeps six months of every state change (`recorder: purge_keep_days`, raised from its 10
days on 2026-11-09) and an hourly average, low and high of every numeric sensor for good (the boiler's power shows
when it was on, for as long as it's been there); who's home is in the event database; and so is every request
("boiler on", asked at 18:12 by Stacey). History starts on 2026-10-30; patterns need a few weeks of it.

**Design:**

- **Found by plain statistics, nightly, on the Pi.** For each device: its changes by weekday and time of day, and
  each one's nearest event before it (someone arriving or leaving, another device, sunset). A routine is the same
  change within half an hour on most days of a kind (weekdays, weekends) for three weeks or more, or within ten
  minutes of the same event most times. No model reads the history; one only words what's found.
- **Offered once, to whoever does it, at a good moment:** the next time they do it by hand, or in their chat; at
  most one offer a day for the household, never in quiet hours, and never again once declined (a declined pattern is
  kept as declined). Yes saves it as a skill, with what it was learned from.
- **Watched after:** a skill whose change someone undoes soon after is asked about ("You turned the boiler off right
  after I put it on. Stop doing that?"), and paused on a yes.
- **The unusual, from the same baselines:** a door unlocked with nobody home for longer than it ever has been; the
  boiler on far longer than usual; a room's temperature outside its usual range at night; a device that's stopped
  answering. Said to whoever's home, or in a chat, by how much it matters, with quiet hours.

**Limits held in code:** a routine that would unlock is never offered; what TARS learned stays on the Pi (the wording
step sends the model only the pattern found, in words); and the patterns found are listed for the maintainer with
what each came from, so a wrong one is easy to see.

**Measure:** the finder run over the recorded history, its routines checked by hand against what the household
says it does; offers made and declined, and skills undone. **Done when** three offered routines have run for two
weeks without being undone, and the unusual has been said at least once when it mattered, and never when it didn't.

## Planned: house commands on the Pi

**Goal:** "lock the door", "AC to 23", "turn off the TV" handled on the Pi itself: answered at once, nearly free, and
still working when the internet is down. Anything else goes on to the cloud as it does today. It's the house part
of [working offline](#later-working-offline), early and with no model.

**Why not a local LLM:** a Pi 4 has no GPU, so a model small enough to fit takes tens of seconds just to read TARS's
instructions, and the benchmark ([choosing the quick model](models.md)) shows how much less accurate small models
get; a confidently wrong lock is worse than a quarter of a cent. House commands are a fixed set, so they don't need
one.

**Design:**

- **Vosk with a list of phrases**, the same small offline recognizer and the same trick the wake double-check uses
  (`verify.py`): limited to a known list plus "[unk]", it's fast and reliable on a Pi. The list is built from
  templates over the `[home]` devices: verbs ("turn on", "switch on", "put on", "start"), the devices by every name
  they have, numbers for the AC's temperature and the volume, and optional politeness ("please", "can you", "TARS").
  That makes thousands of phrasings from a few dozen lines. Device names the model's vocabulary doesn't know are
  found when the list is built, so a name it can't hear fails loudly instead of quietly never matching.
- **Beside the cloud, hanging up early.** After the wake word, the audio goes to the cloud speech-to-text as now and
  to Vosk. As soon as Vosk is sure it's hearing a command, TARS stops the cloud stream (billed only for the second or
  two already sent) and acts; with "[unk]", the turn carries on exactly as today, no slower. A setting,
  `local_first`, sends nothing to the cloud until Vosk has heard the whole request: a command is then free, at the
  cost of every other request losing the streaming head start (to be measured). With no internet, it works that way
  by itself.
- **The same house tool, the same guards.** A matched command becomes the call the model would have made, so a
  change still counts as done only when the device itself reports it, unlocking stays as guarded as it is now, and
  the web UI shows the turn like any other, marked as handled on the Pi.
- **Replies in TARS's voice, a few of each, picked at random.** Every reply ("Door's locked.", "Locked. Fort Knox
  has nothing on us.", "AC on, 23 degrees.") is recorded once in the current voice and kept on disk, several per
  outcome, never the same one twice running; the jokes follow `[llm] humor`. A failure has its own ("The door didn't
  answer."). Numbers are recorded as pieces, so "23 degrees" doesn't need its own file for every temperature.

**Measure:** recorded commands from each voice in the house, said many ways, plus near-misses and ordinary
conversation: a command must match the right device and action, and conversation must never match a command (a
wrong match is the one failure that matters). Then time to the device changing, against today's, and the cloud
seconds saved. **Done when** a week of real house commands has no wrong match and the door locks with the internet
off.

## Later: working offline

**Goal:** when the internet or a provider is down, TARS still does the essentials at home: the lights, the door, the
AC, timers and reminders, said and heard, with no cloud. And anything someone would rather keep in the house can stay
there.

**Where it stands.** The wake word, its double-check, the speech detectors and speaker ID already run on the Pi, and
so does working the house: Home Assistant, the lock and the boiler through it, the IR blaster and the Android TV box
are all on the home network. Everything between hearing a request and saying the answer is a cloud service: speech
to text (Deepgram, OpenAI), the model (Cerebras, OpenAI) and the voice (Cartesia, Deepgram, OpenAI). When all
of a stage's services fail, TARS says it couldn't, and the Status page shows which ([web UI](web-ui.md#the-status-page)).

**Plan:**

1. **A local fallback for each stage,** used only when the cloud ones fail: speech to text with a small Whisper
   model, a voice from Piper (whose voices the wake word was already trained with), and a small local model for the
   brain, limited to the house tool, timers and reminders, with web search, sending and the browser turned off and
   said so. House commands may already work offline by then, from [house commands on the
   Pi](#planned-house-commands-on-the-pi).
2. **Where it runs.** A Pi 4 is too slow for a useful local model. Either a Pi 5 with an AI accelerator, or a small
   home server on the network (an old laptop, a mini PC), which the Pi calls the way it calls a provider today. The
   choice is measured: time to first sound and how often it picks the right device, on step 1's recorded requests.
3. **Private on purpose:** a setting that keeps chosen things local even when online (a person's requests, or
   anything about the house), at the cost of a slower, plainer answer.

**Measure:** the share of real requests from step 1 the local path answers right, and its time to first sound,
against the cloud path. **Done when** pulling the Pi's internet still leaves the lights, the door, timers and reminders
working by voice, in under three seconds to first sound.

## Later: a brain anywhere, a body at home

**Goal:** TARS doesn't live on the Pi. Its brain (the models, conversations, skills, reminders, memory, Telegram, the
web UI, the database) runs wherever is most reliable: a small cloud server, the Mac, or still the Pi. The Pi becomes
the body: the microphone and speaker in the room, and what only works from inside the home. If the Pi or its card
dies, the house goes quiet for a while, but TARS still chats, reminds and remembers; and a second room is a second
body.

**Where it stands.** TARS is already three processes sharing one folder: the voice, Telegram and the web UI. Telegram
runs without the voice process, and reaches it only for `/say`, the microphone's mute and recording sessions, over a
Unix socket (`control.py`). But all three assume the hardware and the home network are on their own machine.

**The split:**

| The body, at home | The brain, anywhere |
|---|---|
| Microphone and speaker; the wake word and its double-check, so no audio leaves until "hey TARS" is confirmed | The models, conversations and the rules of every tool (unlocking, a turn's go-ahead, nothing after a web search) |
| Knowing when someone's turn is over, which is latency-sensitive | Skills and their clock, reminders, wishes, settings, preferences |
| Buttons and the speakerphone's mute | Telegram and the web UI |
| What only answers at home: the Android TV over ADB, the IR blaster, Bluetooth devices, the routers (who's home) | The database, its backup, TARS's records and knowing itself |
| Home Assistant and Mosquitto, in Docker as now | Speech to text and the voice (cloud services either way) |

The body decides nothing: it does what it's asked and says what happened.

**Design:**

- **The body dials out.** One long-lived connection to the brain, over Tailscale or a WebSocket with a key at each
  end; nothing at home opens a port, as with Telegram today. Events go up (a wake confirmed, with the audio after it;
  a button; a phone coming home; a device changing), calls come down ("play Channel 12 on the TV"), each answered as
  the home tool's are now, and the reply's audio streams down to the speakerphone.
- **A voice turn:** the wake word and its check on the Pi, as now; the request streamed to speech to text, from the
  body itself to save a hop (the brain then gets words, not audio); the brain thinks and calls the house through the
  body; the voice's audio streams back down. A hop through the brain adds tens of milliseconds, which a server near
  the AI services may win back: it reaches them faster than a home connection does. Measured, not assumed.
- **Secrets stay where they're used.** The house's (Home Assistant's token, the IR blaster's key, the TV's ADB key)
  on the body; the models' and Telegram's on the brain.
- **Unlocking is checked twice.** The brain keeps its rules, and the body checks again, on its own: a request that
  started at home by voice, the speaker recognized on the Pi, or a trusted chat's own "unlock". So even a brain
  that's been taken over can't open the door.
- **When the brain can't be reached** (the internet, or the server, down): the body still hears its name, says it
  can't reach its brain, and does what it can on its own: [house commands on the
  Pi](#planned-house-commands-on-the-pi) from a fixed phrase list, and later [working offline](#later-working-offline).
  **When the body is down:** the brain carries on in chats, and the house tools say plainly that the house is
  offline.
- **More rooms, other homes.** Each room can have a body of its own (a Pi Zero with a microphone, or an old phone);
  the brain knows which room spoke and answers there, and a reminder can follow someone from room to room. A home is
  its bodies and a brain, which is what [TARS in other homes](#later-tars-in-other-homes) needs.

**Plan, each step shipped on its own, TARS working after each:**

1. **One boundary in the code.** Everything that touches hardware or the home network goes through a body interface,
   still in-process on the Pi. Nothing changes for anyone.
2. **Two processes on the Pi.** The body becomes its own service, and the brain talks to it over a socket, as
   `control.py` does now; the body's own unlock check lands here.
3. **The brain moves.** Pointed at a server, the two joined over Tailscale; the body's fallback when the brain can't
   be reached; the backup and `--check` follow the brain.
4. **A second body**, in another room, to prove the split holds.

**Open questions:** where the brain runs (a small server for a few dollars a month and always on; the Mac, which
sleeps; or still the Pi); whether speech to text goes straight from the body; how much the body does while the brain
is away; and whether Home Assistant stays on the body or moves to a box of its own.

**Measure:** time to first sound and the share of house commands done right, before and after each step, on the same
recorded requests. **Done when** the brain has run off the Pi for a week with no difference anyone noticed, pulling
the Pi's plug left Telegram, reminders and memory working, and a second room's body answered in that room.

## Later: TARS in other homes

**Goal:** someone who isn't a developer can run TARS at home: buy the parts, follow one guide, and have it working in
an evening, then keep it updated without SSH.

**Where it stands.** The repo is public and MIT-licensed (the trained wake models are CC BY-NC-SA). Setup is
`deploy/install-pi.sh` on a Pi plus written runbooks ([deployment](deployment.md), [the house](home.md)) that a coding
agent can follow over SSH, checking each step with `--check`. Every paid service is optional, and the household's own
devices are a plugin (`examples/my_house`). What's missing for anyone else: an image to flash, a setup that doesn't
assume a terminal, updates (step 4), sign-in for the web UI (step 2), and the keys, which each home brings itself.

**Plan:**

1. **Steps 2 and 4 first:** no one else's home should run an always-on mic without sign-in, granted plugins and
   signed updates.
2. **A Pi image** with TARS installed, and a first-run page on the home network: Wi-Fi, the speakerphone (found and
   tested), which services to use with links to get each key, and the house (Home Assistant found on the network, or
   set up), ending with `--check` in plain words.
3. **A tested parts list:** the Pi, a speakerphone that works full- or half-duplex as measured in step 3, a case,
   optional Bluetooth proxies; and what each service costs a month at typical use (from the [evals'](evals.md) usage
   profile).
4. **Plugins people can share:** a catalogue of declared, versioned plugins (step 4) for common devices, each with
   the grants it asks for shown before it's installed.
5. **Licensing settled:** the wake models' non-commercial license, and any provider terms, checked for what a
   shared image may include; what can't ship is downloaded on first run.

A home could also be only bodies, its brain hosted elsewhere ([a brain anywhere, a body at
home](#later-a-brain-anywhere-a-body-at-home)), which makes the setup at home a small device and the rest a sign-up.

**Done when** a second household, set up from the image and the guide without help, uses it for a week.

## Built: handing hard questions to a larger model

Qwen replies `<ponder>` for a question that needs real thinking (planning, comparing options, several steps), TARS
says "Let me think about that for a moment.", and `[llm] think_model` (default: `model`) answers at `think_effort`
(default high), with the same conversation, tools and web search, within `think_timeout_s` (45 s); see [who does
what](architecture.md#the-llms-tools). The web UI marks those turns. Each model is told, from one list, what it can do
itself, what it hands over, and what TARS can't do at all. The marker isn't `<think>`, because Qwen 3 writes its own
reasoning between those tags.

**Larger models as the quick one** were measured too ([choosing the quick model](models.md)): Haiku 5.5, Luna and
gpt-oss-120b score within three of Qwen's 149 of 156; Haiku and Luna start talking later, and gpt-oss-120b, the
fastest, is plain where Qwen is TARS, so Qwen at low reasoning stays the quick model. Jev as a router would make TARS
slower in front of it and save about 0.13 s beside it, so there's no router. The [scenario evals](evals.md) follow a
hand-off through: hard questions reach the thinking model, which carries on from what TARS already said, and the
follow-up comes back to Qwen. One case is left at 3 of 5: a hard question in the same request as a reminder, where
Qwen starts on the question itself instead of handing it over. How often it hands over in real use is in step 1's
checklist.

## Benched: learning from how conversations go

An offline job that reads the logged conversations and finds where TARS did badly: answers rated bad, transcripts
people corrected, "not meant for TARS", the same question asked again right after an answer, and interruptions
(after step 3). It groups them and suggests prompt changes, tested by replaying past conversations through the old
and the new prompt, the way new wake models are tested before they're installed. That's more telling than sentiment
scores on transcripts. **Benched** until there are a few hundred real conversations to learn from.
