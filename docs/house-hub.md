# The house hub: working the house from a brain that isn't at home

**Status: the design is agreed; step 2 has started.** The house tools now reach every listed device through the
hub's boundary, in one process (`hub.py`): each is a `HubDevice` that asks a `LocalHub`, which holds the devices
`hub.build` made, and the `home_assistant` tool reaches Home Assistant through `HubHomeAssistant`, one request of
`HA_REQUESTS` at a time. Still to go: the loops on the home network (who's home, the doors, auto-lock), whose events
reach the brain through the functions they're handed. It answers how a brain that isn't on the home network (a
hosted one, for an invited home) can still work the house: Home Assistant, the TV, the IR blaster, the doors, and
knowing who's home. It follows [a brain anywhere, a body at home](roadmap.md#later-a-brain-anywhere-a-body-at-home),
and uses the [body link](bodies.md)'s transport and device keys. The owner's own home doesn't need it while its brain
is on the Pi; an invited home with devices does, before [hosting](roadmap.md#later-tars-in-other-homes) can serve it.

The part that matters most is [unlocking](#unlocking-checked-twice): a brain that's been taken over must not be able
to open the front door. Everything else is plumbing.

## The split

A **hub** at home does what the brain asks and reports what happened, over a connection it opens. It decides
nothing, except the second unlock check.

| The hub, at home | The brain, anywhere |
|---|---|
| The devices, built from the home's own `[home]` (`home.build`): Home Assistant's, the Android TVs (`androidtv.py`), plugins (`examples/my_house`: the IR blaster, Bluetooth), Spotify | The house tools the model calls (`HomeTools`): the tool and its description, every call's rules (a turn's go-ahead, nothing after a web search, nothing a door's lock set off), the unlock challenge's conversation |
| The house's secrets: Home Assistant's token, the TV's ADB key, the IR blaster's key, Spotify's | The models' keys, Telegram's, the database |
| What runs on the home network: who's home from the routers (`presence.py`, `routers.py`), the doors over MQTT (`doors.py`, `mosquitto.py`), auto-lock (`autolock.py`) | What those feed: reminders said on coming home, skills set off by a door or a person, announcements |
| The second unlock check, with the trusted voiceprints and its own recognizer | The conversation, speaker ID for everything else, skills, reminders |
| House commands from the fixed phrase list (`commands.py`) when the brain can't be reached | |

## Where the boundary goes: at `Device`

Two places could take the cut.

| | At `Device` (proposed) | At `HomeTools` |
|---|---|---|
| What crosses | One device's `status`, `call`, `history`, `past`, `glance`, `check`, `stop` | The model's whole tool call |
| Where the rules run | On the brain, as now: `HomeTools._change` checks the go-ahead, the web search and the door's lock before anything goes down | On the hub, which would need the turn's state (the go-ahead is a promise the brain's conversation keeps) |
| What the hub needs | The devices, and the unlock check | The devices, the rules, and a copy of the turn |
| Change to today's code | `HomeTools` gets a `RemoteDevice` per device in place of the real ones; nothing else about it changes | `HomeTools` moves whole |

At `Device`, the rules stay on the brain where the roadmap wants them, the hub stays small, and `HomeTools` keeps
its tests. The Home Assistant tool (`ha_tool.HAAccess`) crosses the same way, as one pseudo-device whose `call` is a
Home Assistant service call: its own checks (what a script, scene or automation could unlock) run on the brain as now,
and [again on the hub](#what-the-hub-checks).

## What the brain knows about devices it can't see

The tool description the model gets is built from each device's name, label, kind, actions, rules (`Device.rules`),
what it is (`Device.what`), and whether an action is slow. The hub sends these as a **description** when it connects,
and again whenever its devices change (a plugin reloaded, `[home]` edited); the brain rebuilds the tool from it,
exactly as `home_tool(devices)` does today, and keeps the last one so a hub that's away still has a tool that says
the house is offline. `glance()` (the short state in each turn's context) is asked each turn, with a short timeout,
and left out when it doesn't answer in time.

## The link

The hub opens its own WebSocket to the brain, like a body, with the same handshake: a challenge, signed by the hub's
Ed25519 device key (`--hub-add` pairs one, as `--body-add` does a body). It's a separate connection, not a body's: a
home can have a hub and no body (a chat-only home with devices), or bodies and no hub, and on the Pi the two would
share a process but not a connection. Text frames are JSON; there's no audio on it.

| From the brain | Fields | Means |
|---|---|---|
| `call` | `id`, `device`, `action`, `args`, `request` (who asked, how, which room), `evidence` (for an unlock) | Do this; answer with what happened |
| `stop` | `device` | Stop what the device is doing (the TV agent) |
| `ask` | `id`, `what`: `status`, `history`, `past`, `glance`, `check` | A read |
| `verify` | `id`, `evidence` | [The second check](#unlocking-checked-twice) for an unlock, before the call |

| From the hub | Fields | Means |
|---|---|---|
| `description` | the devices | What there is to work, and how |
| `answer` | `id`, `text` | What happened, as `Device.call` says it today ("done: the AC is on at 22", "error: ...") |
| `event` | `kind`, fields | Something at home happened (below) |
| `challenge` | `id`, `digits` | An unlock's digits, made by the hub |

**Slow calls.** The TV agent plays something step by step and can take a minute. Its `call` is answered at once with
what it's started ("On it."), as the slow path does now (`Device.slow`, `ONE_MOMENT`), and what it finally did comes up
as an `event` (`said`), which the brain says in the room or the chat it came from, as `announce` does today.

## Events

The loops that only work from inside the home move to the hub whole, and send up only what they decided:

| Event | From | The brain then |
|---|---|---|
| `home` {person, came/went} | presence, the routers asked every 30 s | Says what was waiting for them, runs a skill set off by them coming home, keeps who's home for reminders |
| `door` {name, opened/shut, key's person} | doors, MQTT | Runs a skill set off by the door (a welcome home), with `Request.by_door` as now |
| `changed` {device, state} | Home Assistant's events, for devices a skill watches | Runs that skill |
| `said` {text, room or chat} | the TV agent, auto-lock telling what it did | Says it, or posts it |
| `down` / `back` {device, why} | a device's own checks (`Device.check`) | Tells the maintainers, as now |

Auto-lock stays on the hub: it only ever locks, it holds the change lock while it does, and it needs the door's
sensor at once. The brain is told when it locked, never asked.

## Unlocking, checked twice

Today `HomeTools._may_unlock` decides, on the Pi. It allows an unlock by voice only from a voice in `[home]
unlock_voices`, recognized surely (`unlock_threshold`), saying "unlock", then three random digits TARS says back; and
from a chat only for the people in `unlock_chats`, from their own message saying "unlock". Every path is held to it:
the home tool's locks, the Home Assistant tool (any service, script, scene or automation that could open a door,
worked out from Home Assistant's setup, `ha_tool.Doors`), and a plugin granted unlock.

With the brain elsewhere, the brain keeps those rules, and the hub checks again on its own.

### What it must stand against

**A brain that's been taken over**: the server broken into, its keys stolen, its code changed. It can send the hub
anything, say anything was heard, and claim any person and score. The hub must not open the door on its word.

Not in scope, as today: someone in the room with a recording or a cloned voice of a trusted person ([security](security.md#someone-in-the-room-or-a-recorded-or-cloned-voice));
a compromised hub, which holds Home Assistant's token and is the house's own trust, as Home Assistant is; and a
trusted person's Telegram account in someone else's hands.

### By voice: the body vouches for the audio, and the hub listens itself

The hub needs evidence the brain can't make: audio a body at home really heard, fresh, in a trusted voice, saying
digits only the hub knew.

1. The brain, its own rules passed, asks the hub for a challenge. **The hub makes the three digits** and sends them
   up; the brain has TARS say them, as now. The brain can't choose them.
2. Each body signs what it streams: at the end of a turn, a **receipt** with the room, when, and the hash of the audio,
   signed with a second key of the body's, its **home key**, whose public half only the hub holds (made when the body
   is paired at home, through the hub). The brain relays receipts and audio, and can't make either.
3. For the unlock, the brain sends `verify` with the answer's audio and its receipt. The hub checks, all by itself:
   - the receipt's signature, by a body paired at home, and that it's fresh (made in the last 30 s);
   - the audio's hash is the receipt's;
   - **its own recognizer hears its digits** in it (Vosk, already on the Pi for the double-check, held to the digits);
   - **its own speaker ID** finds a voice from `unlock_voices`, surely, with the household's voiceprints, kept on the
     hub too.
4. Only then does the hub take the `call` that unlocks, once, within a few seconds.

| What the taken-over brain tries | Why it fails |
|---|---|
| "Alon said unlock" | The hub asks for audio with a body's receipt |
| Makes up audio | No body's home key to sign it |
| Replays a real recording of Alon unlocking last week | The digits are new each time, and the receipt is minutes old |
| Has TARS say digits of its own choosing | The hub listens for its digits, not the brain's |
| Sends the right audio, then calls unlock twice | One check opens one call, once |
| Unlocks through a Home Assistant script | [The hub checks every call](#what-the-hub-checks), whatever it's named |

**Cost:** a second recognizer pass and a speaker ID on the hub per unlock (to be measured on a Pi 4), and the
household's voiceprints in two places. A home whose hub can't run speaker ID (too small a device) doesn't unlock by
voice; it says so.

### From a chat: the hub asks the person itself

A chat's messages reach the brain, which holds Telegram's bot token; the hub can't tell a real "unlock" from one the
brain made up. So for a chat the hub asks the person itself, on a channel the brain doesn't hold:

- **The door bot (decided).** The hub has a small Telegram bot of its own (its token on the hub only). An unlock from a
  chat, past the brain's rules, makes the hub send the person, in its own chat, "Unlock the front door? [Unlock]
  [No]", with what was asked and when. Only their tap opens it, within a minute. The brain can't send that button,
  and can't press it.
- **Instead: a passkey.** The person approves on a page with the passkey on their phone. Stronger against a stolen
  Telegram account, but the page must be served from the hub's own name, which isn't reachable from outside home,
  so it works only at home, where voice already does.
- **Or not at all.** A hosted home doesn't unlock from chats; by voice at home only.

The door bot is the one that keeps today's "unlock from my chat on the way in", and it's off unless a home sets it up.

### What the hub checks

The hub doesn't trust the brain to say which calls unlock. It runs the same analysis the brain does, with its own
Home Assistant: the lock services that let someone in (`unlock`, `open`), and any script, scene, automation or group
that could reach a lock or a door (`ha_tool.Doors`, moved to code both sides share). Any call that could unlock is
held until a `verify` for it passed; anything that can't be seen into counts as one that would. Locking is never
held back.

## When one side can't be reached

- **The brain away** (the internet down, the server down): the hub keeps presence, doors and auto-lock running on
  their own, keeps what happened for when the brain's back, and, with a body of its own, hears its name and runs
  the fixed house commands (`commands.py`), saying it can't reach its brain for anything else. No unlocking by
  voice: the brain's half of the check isn't there.
- **The hub away:** the house tools answer "the house is offline right now" from the last description, and the
  brain carries on in chats, with reminders and memory. Reminders for when someone comes home wait.

## Installing it

- **On the owner's Pi** (step 3 below): the hub is a second service beside the voice assistant, with the house's
  config and secrets; the brain reaches it on localhost. Nothing to pair by hand: `--hub-add` runs once at install.
- **In an invited home:** a small device that also runs Home Assistant (a Pi with Home Assistant OS, or the box
  their Home Assistant already runs on), with the hub installed, paired by a code shown in the web UI, as a body is.

## The steps, each shipped on its own

1. **This design**, agreed, the unlock part above all.
2. **One boundary, in one process:** everything on the home network behind the hub's interface, still in the voice
   process on the Pi. Nothing changes for anyone. Done when no module outside the hub's side imports a device
   client, the tests pass unchanged, and a week on the Pi shows no difference.
3. **Two processes on the Pi:** the hub as its own service, the brain talking to it over the link on localhost, and
   the hub's own unlock check. Done when stopping the hub makes the house tools say the house is offline while chats
   and reminders carry on, and an unlock the brain alone asks for is refused by the hub.
4. **The brain elsewhere:** a chat-only brain on another machine works the house through the hub. Done when the house
   commands are done right as often, and the device changes as soon, on the same recorded requests
   (`tools/commands_bench.py`, `evals/house.py`), before and after.

## Decided

- **A hub at home, not all in the cloud** (the owner, 2026-11-18). A home whose devices are all in Home Assistant could
  be worked from a server through Home Assistant's own remote access, with no TARS box at home, but it would lose
  what only answers at home (an Android TV, an IR blaster, the routers for who's home) and the second unlock check,
  which means nothing on the same server as the brain. Every home with devices gets a hub.
- **From a chat, the door bot.** An unlock asked in a chat, past the brain's rules, is confirmed by the person
  tapping Unlock on the hub's own Telegram bot, whose token only the hub holds. It's off unless a home sets it up.
- **The trusted voices' prints are kept on the hub too**, so it can recognize them itself.
- **A hub that can't run speaker ID doesn't unlock by voice.** There's no weaker fallback that takes the brain's word
  for who spoke.
