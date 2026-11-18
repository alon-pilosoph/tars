# Bodies: TARS in another room

TARS's **body** is what it hears and speaks through: on the Pi, the speakerphone. A **network body** is another
machine with a microphone and a speaker (a second Pi, a laptop, an ESP32 board, a web page) that does the same over
the home network, while the conversation itself stays on the **brain**, the machine TARS runs on.

A body only moves audio. It listens for "hey TARS" (or a button), and when someone wants to talk it streams its
microphone to the brain; the brain sends back the reply's audio and a few words of control. Everything about taking
turns (when someone's done, the answer started during a pause, follow-ups, who's speaking) is the brain's, the same
code as for the Pi's own speakerphone. That keeps a body small enough for an ESP32, and keeps one way of holding a
conversation, not one per device.

**Where it stands:** tried across real machines: the home Mac as a body for the Pi, two bodies holding
conversations at once ([rooms](#rooms)), and an ESP32 board, [the Atom](atom.md), that hears the name itself and
leaves the double-check to the brain.

## Running one

On the brain (the Pi, or any machine running TARS):

```toml
# config.local.toml
[bodies]
enabled = true
room = "living room"   # this machine's own speakerphone's room; [audio] enabled = false for a brain with none
```

`uv run voice-assistant --body-add Kitchen` prints the body's key. Then `voice-assistant` (the voice process) listens
on port 8766, waits for the body, holds conversations through it, and waits again whenever it goes: a body that
drops is not a fault. The Status page expects the voice process with `[bodies]` on.

On the body (a second Pi, a laptop: anything with a microphone, a speaker and TARS's repository installed with the
voice extra):

```bash
echo 'TARS_BODY_KEY=tars-body-1:kitchen-...' >> .env    # the key --body-add printed
uv run voice-assistant --body ws://homepi.local:8766
```

It uses its own `[audio]` devices and `[wake]` word, with the same double-check as the Pi, so it says `wake:
verified` and nothing leaves the room before its name is confirmed there. It needs no model keys and no database.
It makes the connection again when it drops (waiting 1, 2, 4... up to 30 s between tries), and when someone calls
TARS while the brain can't be reached, it says one line it has with it: "I can't reach my brain right now. Give me
a minute, then try again." `deploy/voice-assistant-body.service` runs it at boot, sandboxed like the voice
assistant but tighter. Its protocol handling is [`bodyclient.py`](../src/voice_assistant/bodyclient.py)'s
`BodyProtocol`, a small state machine with no devices in it: the ESP32 and the web page are held to the same
behaviour.

An ESP32 board runs TARS's own firmware instead: [the Atom](atom.md), set up with ESPHome.

## Pairing

```
uv run voice-assistant --body-add Kitchen      # prints the kitchen body's key, once
uv run voice-assistant --body-list             # which are paired, and when each last connected
uv run voice-assistant --body-remove Kitchen   # its key is refused from then on
```

A key is one line, `tars-body-1:<device>:<key>`, given to the body and kept like a password: the device's id and an
Ed25519 private key ([`bodykeys.py`](../src/voice_assistant/bodykeys.py)). TARS keeps only its public half, in the
event database's `bodies` table, so a copy of the database (a backup, an SD card) can't be used to pose as a body.
One body per room; to change one, remove it and add it again. `[bodies]` in `config.toml` turns listening on (off by
default: nothing listens), and sets the port (8766). `--check` says whether bodies may connect, and has a line for
each one paired with when it last connected. What a key lets someone do, and what it doesn't:
[security](security.md#a-network-body-or-something-posing-as-one).

## Rooms

Each body is a room of its own, and so is the brain's own speakerphone ([`rooms.py`](../src/voice_assistant/rooms.py)).
Every room has its own assistant, on its own thread, with its own conversation (its own brain and journal), so
someone in the bedroom and someone in the living room can talk to TARS at the same time; a conversation stays in the
room it started in. What they share: the voice, the speech to text, who's who, the reminders and skills, and the
house, whose changes go one at a time.

What TARS says by itself goes to one room, and is said once:

- **A reminder, or a skill's line, for someone** goes to the room where they were last heard (by their voice, in the
  last 2 hours); otherwise to the **main room**. Only a room's own hearing counts: the routers know who's home, not
  which room they're in.
- **What's for nobody in particular** (an announcement, a household skill) goes to the main room.
- **A room whose body is gone** is given nothing; what it had taken and not yet said goes back, and to another
  room. A muted room is passed over for one that isn't, but a home where every room is muted still hears its
  reminders, as a muted TARS always has.
- Two rooms can't both take the same thing: a room takes it under the desk's lock, and it's that room's until it's
  said. A take that's never finished (a room that stopped) is free again after 2 minutes.

The main room is `[bodies] main_room`, or this machine's room (`[bodies] room`), or, with no microphone of its own,
the room paired first. Each conversation is kept with its room. The brain is told which room a request came from
(`[Room: bedroom]`, and `Request.room`), so it knows; choosing "the light" by the room comes later. With `[bodies]`
off, there's one room and nothing about TARS changes.

The web UI shows the room under each conversation's time and in its last line, and the Status page lists the bodies:
each one's room, and whether it's connected (the voice process writes `bodies.json` beside the event log as they come
and go); `--check` says the same. A wake heard by a body that runs only the wake word is kept in the wake log with its
room and audio, as the Pi's are, under the body's own wake model (`atom:<version>`), so Review shows it;
the Pi's buttons and the control socket act on its own room only.

## The web page

A browser can be a body too, for as long as a page is open: [the Talk page](web-ui.md#talk). It asks the web UI for
a ticket, opens the body link with it (`wss://` on `[bodies] secure_port` from a page over HTTPS, with the web UI's
certificate; `ws://` on `port` otherwise), and talks by a tap: a `wake` with `how: button`.

- **Who's speaking** is whoever is signed in on the page, in place of speaker ID. With sign-in off, nobody is known
  from the page, and the voice is told as anywhere.
- **A room only while it's open:** "Stacey on the web", each page its own. Nothing TARS says by itself goes there,
  and talking on it doesn't count as being heard in a room, so reminders still follow the voice.
- **Never at home:** a request made on a page never unlocks the front door, wherever the browser is, as a chat
  without a trusted person can't.

## How fast

Measured on the household's Pi 4 with the home Mac as the body over Wi-Fi (about 5 ms round trip), the same eight
recorded questions each way, 24 turns: from the end of speech to the reply's first sound, a median of 1.23 s over the
network against 1.07 s on the Pi's own speakerphone. Five of the eight questions came out within 0.06 s of each
other; three about 0.2 s slower, still to look into. The answer started during a pause at the same moment both
ways (0.24 s median). Ten minutes of streaming lost nothing: every frame arrived, in order, 99% within 21 ms.

## The protocol (1)

One WebSocket per body, opened by the body: the brain never calls a body, so a body works the same whether the brain
is on the home network or not. Text frames are JSON objects with a `type`; binary frames are audio.

- **Audio up:** 16 kHz, mono, signed 16-bit little-endian, in frames of any length (the brain cuts them into 80 ms
  blocks).
- **Audio down:** mono signed 16-bit little-endian, at the rate `welcome` says (the voice's own, like 24 kHz).

### Letting a body in

1. The brain sends `{"type": "challenge", "nonce": "..."}`.
2. The body answers within 10 s with `hello`: `protocol` (1), `device` (its id), `proof`, and what it is: `kind`
   (`pi`, `atom`, `web`...), `wake` (below), `echo_cancel` (whether it takes its own sound out of what it hears),
   `buttons`, `version`. `proof` is the nonce (its UTF-8 bytes) signed with the device's Ed25519 key, in hex: the key
   itself never crosses the wire.
3. The brain sends `welcome`: `room`, `play_rate`, `max_listen_s`.

Anything wrong (an unknown device, a wrong proof, another protocol, audio before `hello`, no `hello` in time) closes
the socket without saying what. An address that keeps failing waits a second longer each time (up to 30 s) before
its next try is even looked at. A body that connects again replaces its older connection.

A web page has no key. The web UI gives a signed-in page a one-time ticket (`POST /api/talk/ticket`), good for a
minute, and the page's `hello` carries `ticket` in place of `device` and `proof`. It's let in as a body of its own
(`kind: web`, `wake: none`, a fresh id), for the person the ticket was made for: who's speaking comes from the web
UI's session, never from anything the page says. See [the web page](#the-web-page).

`wake` says what the body checks itself before it says someone wants to talk:

- `verified`: the wake word and its double-check, as the Pi does. The brain trusts it, and still keeps the pre-roll
  for knowing who's speaking.
- `model`: only the wake word (an ESP32). The brain runs the double-check on the pre-roll, as for the Pi's own, and
  sends `sleep` if it isn't TARS that was called.
- `none`: no wake word (a web page): every wake is a button.

### Messages

| From the body | Fields | Means |
|---|---|---|
| `wake` | `how` (`word`, `button`), `score`, `preroll_ms` | Someone wants to talk. Audio follows at once: first the pre-roll (the last `preroll_ms`, which had the name in it; up to 5 s, in whole 80 ms blocks), then live |
| `played` | `id`, `cut` | That playback has finished leaving the speaker, or was cut short there |
| `button` | `name` (`stop`, `mute`, `unmute`) | A button other than talk |
| `gone_quiet` | | The body stopped streaming at its own limit |
| `ping` | | The brain answers `pong` |

| From the brain | Fields | Means |
|---|---|---|
| `challenge`, `welcome` | | Above |
| `listen` | `on` | Stop streaming while TARS speaks (`false`), and start again after (`true`) |
| `play` | `id`, `listening` | The reply's audio follows, until `play_end`. `listening`: keep streaming meanwhile (only a body that cancels its own echo) |
| `play_end` | `id` | No more audio for this playback |
| `stop` | | Stop what's playing, and drop what's buffered |
| `sleep` | | Back to waiting for the name: the conversation is over, or the wake wasn't TARS |
| `mute` | `muted` | Stop or start listening for the name |
| `pong` | | |

A turn, pressed with a button ([the transcript the tests check](../tests/fixtures/bodylink/button-turn.json)):
`sleep` (the brain waits) → the body's `wake` and its audio → `listen off` once the question's heard → `play`, the
reply's audio, `play_end` → the body's `played` → `listen on` (a follow-up may come) → `sleep` when nobody speaks.

### Rules the brain keeps

- **Audio is only taken while a body is awake:** from its `wake` until the brain sends it to `sleep`. What comes
  otherwise is dropped as it arrives, so the pre-roll after a wake is never mixed with anything before it. A wake
  that crosses the brain's `sleep` is dropped too: the body went to sleep on it.
- **The reply is sent as it's made**, faster than it plays: a body must be able to hold **at least 30 seconds** of
  it, and says `played` once the last of it has left the speaker. A body that never does (3 s past the audio's own
  length) is taken to be gone.
- **Limits:** a frame over 256 KiB, an odd-length audio frame, text that isn't a JSON object, or a `wake` with an
  unknown `how` closes the connection. Audio waiting unread is kept to its newest 30 s. Fields a body sends that the
  brain doesn't know are ignored, so newer bodies work with older brains.
- **A body that goes away is not a fault:** it raises `BodyGone` in whatever was waiting on it, which ends that
  body's conversation, never TARS (a microphone the Pi loses ends the process so systemd starts it again; a laptop
  closing its lid mustn't). Neither does one that sends nothing for 3 s while it should be streaming.

### Rules a body keeps, whatever the brain says

These hold even if the brain were compromised; they're the body's, in its own code.

- No audio leaves before its own wake or a button press. `listen on` is honoured only inside a conversation the
  body itself opened, never from sleep.
- A conversation has a hard cap (`max_listen_s`, and the body's own, whichever is lower); past it, the body stops
  streaming and sends `gone_quiet`.
- It shows that it's streaming, where it has a way to: a light, a visible state on a page. A body with neither (the
  Atom) shows nothing; TARS's own "Yes?" after the wake is the sign it's listening.
- Muted means the wake word isn't even running.

With `wake: model`, a few seconds of audio leave the room before the wake is confirmed, since the double-check runs on
the brain: on the home network, that audio goes only to the Pi.
