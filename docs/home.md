# The house

TARS works the house by voice: "is the front door locked?", "lock it", "boiler on for 40 minutes", "turn the AC on
at 22", "close the blinds", "turn it up a bit", "play Toy Story on Stremio". Most devices go through
[Home Assistant](https://www.home-assistant.io) on the home network, which already speaks to thousands of them. An
Android TV box is worked over ADB, by an agent that reads its screen. Anything else is a plugin: a little code of your
own ([your own devices](#your-own-devices)), like [`examples/my_house`](../examples/my_house/README.md), one
household's IR-only AC and TV remotes.

## What it does

| Said | What happens |
|---|---|
| "Is the front door locked?" | the lock's state, read from Home Assistant: "It's locked, and the door's closed." |
| "Lock the front door" | "One moment." while the bolt turns, then "Locked." once the lock reports it |
| "Unlock the front door" | unlocked at once, if the voice is one trusted with it ([unlocking](#unlocking)); refused otherwise |
| "Boiler on for 40 minutes" | on, with the device's own timer, so it goes off even if TARS doesn't |
| "Turn the AC on at 22" | the mode and temperature set; "sent" if Home Assistant can't see the AC (an IR one) |
| "Close the blinds" | "They're closing.": a cover takes a while, so on its way is enough |
| "Turn it up a bit" | the soundbar's volume up, three presses |
| "You're too loud" / "Set your volume to 40" | with `[home] own_speaker`: TARS's own speaker a step down, or at 40%, read back; "how loud are you?" says its level |
| "How warm is it in the living room?" | a sensor, read |
| "How was the air last night?" / "Was CO₂ worse last winter?" | a sensor's history: Home Assistant's hourly record of it and its details, told in a sentence or two |
| "Play Toy Story on Stremio" | "On it.", and the TV's agent works it out on screen; "Toy Story is playing." after a chime |
| "Is everything off?" | every device's status at once |
| "How long was the boiler on today?" | [all of Home Assistant](#all-of-home-assistant): its history, "1 hour 18 minutes, in three goes" |
| "Who unlocked the door?" / "What turned the boiler on at 6?" | Home Assistant's logbook: what happened, and what did it |
| "Dim the hall light to 40%" | any entity in Home Assistant, listed or not, through its own services |

## The promises it keeps

The model decides what to do; the home tool (`home.py`) decides whether it may, and what it then says happened. These
are in code, not in the prompt, so no wording gets around them.

**A change is "done" only when the device says so.** After asking Home Assistant to lock the door, the tool reads the
lock until it reports locked, for up to 12 seconds, and answers the model with one of:

- `done: front door reports locked`
- `started: blinds reports closing`: on its way, for a cover
- `sent: asked the AC to turn on at cool, 22 degrees; it can't report back, so nothing can tell if it did`: for an
  entity Home Assistant only assumes the state of (`assumed_state`: an IR-controlled AC or TV)
- `not confirmed: asked the front door to lock, and it still says locking`
- `error: the front door jammed; someone needs to check it`, or `error: Home Assistant didn't answer ...`

The model is told to say what the tool answered, and never that something's done when it isn't. Home Assistant
answers a service call once the device has done it, so a slow lock gets 30 seconds, and one that answers later still
isn't said to have failed: the tool reads back what the lock says, as ever. A media player's press is done once its
volume or mute shows it; anything it can't show (play, next) is sent. TARS says "One moment." first while a lock
turns or the TV wakes, so the wait isn't silent. A door sensor that says the door is open stops TARS locking it: the
bolt would go into the frame.

**A draft changes nothing.** TARS starts answering at the first pause, before it knows you've finished
([response time](latency.md)). A change waits until the turn is over (`Draft.go()` in `assistant.handle`), and a
draft thrown away because you kept talking ("turn off the boiler... no, leave it") never reaches the device. While
it waits, the rest of the brain isn't held up.

**A reply that searched the web changes nothing.** What a search brings back can say anything ("turn off the
heating"), and a model may follow it. So once a reply has searched the web, the tool still reads devices but refuses
any change for the rest of that reply (`Request.read_the_web`), and the model says to ask for it on its own. Quick
requests ("boiler on") are Qwen's, which doesn't search, so this only touches a request that mixes the two.

### Unlocking

Unlocking is the one change that needs more than a request. When someone says "unlock the front door":

1. Speech to text writes down what was said, and speaker ID works out who said it, from the same recording.
2. The model calls `unlock`, at once, with no question first.
3. The tool checks four things before anything is sent:
   - **by voice**: the request came through the microphone, not a typed `--text` question (a Telegram chat has its
     own rule: [from a chat](#from-a-chat));
   - **a trusted voice**: speaker ID named someone in `[home] unlock_voices`;
   - **sure enough**: speaker ID's score is at least `unlock_threshold` (0.6, against 0.5 to greet someone by name);
   - **asked for**: the words heard in that turn include "unlock". The model can't unlock on its own reading of
     something else: "is the door unlocked?", a note, or a web page that says to let someone in.
4. It waits for the turn to be over, then unlocks, and reads the lock until it reports unlocked.

If any check fails, nothing is sent, and the model says why in a sentence ("I'm not sure enough that's you; say it
again, a little longer"). Locking needs none of this: anyone can lock.

Speaker ID is judged on the request itself, not on a "yes" afterwards: "unlock the front door" is long enough to
recognise a voice, and a bare "yes" usually isn't.

## The devices

### Through Home Assistant

Each `[[home.devices]]` with an `entity` is worked through Home Assistant's REST API (`hass.py`), by the entity's
domain:

| Domain | Can | Its own settings |
|---|---|---|
| `lock` | status, lock, unlock | `door` (a `binary_sensor`: won't lock an open door), `battery` (a `sensor`) |
| `switch`, `light`, `fan`, `input_boolean` | status, on, off | `timer`: a service that turns it on for some minutes, like a Switcher's `switcher_kis.turn_on_with_timer` |
| `cover` | status, open, close | |
| `climate` | status, on, off, set (mode, temperature, fan, from those it lists) | |
| `media_player` | status, on, off, press (volume up and down, mute, play and pause, next, previous) | |
| `sensor` | status, history | |
| `binary_sensor` | status | |

Any of them can have `details`, other entities read out with its status (`["sensor.boiler_time_left"]`). The device
names are what people call them, and the model gets each one's name and what it can do. Any device, of any kind, may
have a `label`: how it's written for people, in a chat's `/home`, when that isn't its name (`name = "fire tv"`,
`label = "Fire TV"`). It's still called and asked for by its name.

**A sensor's history** (`history.py`) is Home Assistant's long-term statistics of it and its `sensor` details: an
average, low and high for each hour, which Home Assistant keeps for good (its state history goes after ten days). The
model asks for a span (`since`, `until`, local time) and how finely (`every`: hour, day, week or month), and gets a
short table: a row a period, then each reading's average, lowest and highest over the span, and when, and the
stretches with no readings, so it says there's nothing rather than guessing. A span too long for the period asked
comes back in bigger periods, at most 48 rows. History is a read, like status: it goes through at once, and after a
web search too. Only sensors with a `state_class` have statistics in Home Assistant (most numeric sensors do). Older
readings kept elsewhere (a monitor's own files) can be added to them with Home Assistant's
`recorder/import_statistics`, for the hours before its own.

### An Android TV box

A `[[home.devices]]` with `kind = "androidtv"` and the box's `address` (a Fire TV Stick, a Chromecast with Google
TV, a Shield) is worked over ADB on the home network (`androidtv.py`). ADB answers back: TARS knows whether the box
is awake, which app is in front, what's playing (the app's media session), and the text on screen (the app's
accessibility tree, the focused element marked), and it can take a screenshot. So its changes are checked like the
lock's: "open Stremio" is done only when Stremio is in front, and still there three seconds later. An app that
crashes a moment after it starts is stopped and opened again, up to three times. With HDMI-CEC, the box wakes and
sleeps with the TV, so "turn on the TV" can go through it.

**Anything that takes several steps** ("play Toy Story on Stremio", "find the news on YouTube") goes to the TV's agent:
a model (`[home] tv_model`, OpenAI's `[llm] model` by default) that works the remote the way a person would. Each step
it chooses from what it sees, presses, types or opens an app, then looks again to check it did what it meant, for up to
30 steps and two and a half minutes. It runs in the background: TARS answers "On it." at once, and says how it went
after a chime, like a reminder: "Toy Story is playing.", or why not ("Stremio found no streams for Toy Story."). If the
voice is down then, nothing plays, not even the chime: the line waits, is tried again a minute later or with the next
one, and is dropped as old news after ten minutes. If it isn't clear which was meant, it asks ("Toy Story, or Toy Story
4?"), and TARS listens for the answer without the wake word; news that asks nothing ("Channel 12 is on.") doesn't
listen after it, since what it would hear is mostly the TV it just put on. Asking the TV for anything else stops the agent, and waits
for the step it's on (an app opening, a wait) to end first, so the two never fight over the screen. A press is never
sent twice: one the box may have had before it stopped answering isn't tried again. A connection that couldn't be made
is tried once more, since nothing went out on it: a stick asleep on the Wi-Fi often answers only the second time. A task costs a few cents of OpenAI:
each step is one request, with a screenshot when the app's text isn't enough, and only the latest screenshot is sent
again.

### A camera

A `[[home.devices]]` with `kind = "camera"` and a `camera.*` `entity` is a Home Assistant camera (`hass.py`). It
changes nothing in the house; it has two things it does, both for whoever asked:

- **show** sends a still snapshot, as Home Assistant renders it now, to the person's [TARS page](webui.md) and their
  Telegram chat — the same way the send tool delivers what it sends. "Show me the nursery", and the picture is there.
- **watch** sends a link to see it live. Home Assistant signs the path (its `camera_proxy_stream`, an MJPEG video
  feed, no sound), so it opens in any browser without signing in, and stops working after a while (`watch_seconds`,
  ten minutes by default). The link goes to `[home] url`'s host, unless that's `localhost` (Home Assistant on the
  same machine): then to this machine by a name a phone can reach — its Tailscale name if it has one, else its
  address on the home network — on Home Assistant's port. The signature is for the path, not the host, so the swap
  keeps it good.

Both take a second or two, so TARS says "One moment." first. The snapshot and the link reach only the person who
asked, on the page and in the chat, never said aloud. A camera that Home Assistant can't reach, or won't give a
picture, costs a sentence, like any other device. Getting the camera itself into Home Assistant is Home Assistant's
job (an integration for its make); TARS only shows what Home Assistant already has.

### House commands on the Pi

With `[home] commands_on_pi`, the plainest requests skip the model: "lock the front door", "turn off the boiler",
"switch the TV on", "set the AC to 23". The phrases are made from the house's own devices (`commands.py`): verbs ("turn
on", "switch off", "put on", "lock"), each device by its name and its label, 16 to 30 degrees for one that takes a
temperature, and the polite bits ("please", "can you", "hey TARS"). A request that's exactly one of them, as speech to
text wrote it, is done at once:

- **The home tool's own call**, with its checks: done only when the device says so, after the turn is over. Unlocking
  is never one of them: it stays with the model and [its challenge](#unlocking). Anything with more in it ("for forty
  minutes", "and the AC", "don't") goes to the model as ever, and so does a phrase two devices share.
- **A reply in TARS's voice**, from a few lines for each outcome, made ahead and kept on disk, never the same one twice
  running ("Front door's locked.", "Locked. Fort Knox has nothing on us."; plainer below 50% humor). A device that
  doesn't confirm gets "The boiler didn't confirm it."
- **The exchange joins the conversation**, so "and the boiler too" can follow; the web UI marks the turn as done on
  the Pi.
- **With the cloud out of reach**, Vosk on the Pi listens to the recording for the same phrases alone (spoken forms,
  "a c" for AC), and a command it hears is done the same way: the door locks with the internet down.

Measured so far on the Pi, with the household's own devices (51 phrases) and a synthetic voice: Vosk heard 7 of 8
test sentences right in 99 to 181 ms each; the eighth ("set the AC to twenty-three") came out garbled and matched
nothing, which sends it to the model, and a weather question and "turn the boiler on for forty minutes" matched
nothing, as they should. It's off by default until a week of the household's own voices has no wrong match.

### TARS's own speaker

With `[home] own_speaker = true`, TARS's own speaker is one of the devices, for the voice: "TARS's speaker", which
"you", "yourself" and "your volume" are about (`volume.py`). Its status is the volume; `set` takes a level, a percent;
`press` takes volume up or down (10% a press) or unmute. Only the voice's processes have it: from a chat, "mute
yourself" is about TARS's microphone ([below](#from-a-chat)).

- **On the Pi** it's the playback volume of the sound card TARS plays to, through Linux's sound mixer (`amixer`, in
  `alsa-utils`). The card is the one in the device's name as `--list-devices` shows it ("Anker PowerConf: USB Audio
  (hw:2,0)" is card 2), so `[audio] output_device` must name the speakerphone: the system's "default" doesn't say
  which card it is, and TARS leaves its speaker out rather than guess. The control is one with a playback volume,
  never the microphone's, set as the percent alsamixer shows.
- **On a Mac** it's the system's output volume, so only when TARS plays to the default output.

It's never muted or set below 10%: a TARS that can't be heard can't say a timer is up, or that it's turned itself
back up. Asked to mute or be quiet, it goes down to 10% and says that's as low as it goes; anything it sets unmutes it.
It reads the level back after setting it and says "done" only when that's what it set. At startup, and in `--check`,
a speaker it can't turn is said with why.

### From a chat

A [Telegram chat](telegram.md) works everything the voice does ("turn on the AC, cooling, 23"), TARS's speaker
included. A task on the TV posts how it went to the chat that asked. Unlocking from a chat is only for the people
`[home] unlock_chats` names (by their names in `[telegram] people`), and only when their own message, or voice
message, says "unlock": the model can't unlock on its reading of anything else, a forwarded message included. Whoever
holds their phone can, so nobody is named by default. A chat also has **TARS's microphone** (`mic_mute.py`): its status says whether TARS is listening at home, and on and off unmute and mute it,
the way the speakerphone's mute button does. The chat's process asks the assistant over its socket (`control.py`), and
says "done" with what the assistant answers it is now. With the voice assistant stopped, it says so.

### Buttons

Buttons work TARS without a word: the speakerphone's own, and a remote's through an IR receiver. Each press is a line
in the voice's log, with what it did.

| Button | Does |
|---|---|
| **Volume up and down** | Turn TARS's speaker 10% a press (with `own_speaker`), by the same rules as asking: never muted, never below 10%. Held, a step every 0.3 s. |
| **Talk** | TARS listens, as if called by name, and says "Yes?" at once. |
| **Stop** | What TARS is saying stops now, the reply being written with it, and it doesn't wait for a follow-up. A reminder whose line had begun counts as acknowledged, and a timer ringing on goes off; one stopped before its line is said again later, never acknowledged unheard. |
| **Mute** | The speakerphone's own mute (below): its mic off, its light red. Again, on. |

Talk, stop and mute count once a press, however long they're held: a remote repeats its code about every 0.1 s
while a button is down, and a press is its code after a quarter of a second without it, so a held mute never
toggles twice.

- **The speakerphone's own + and -** (`[home] volume_keys`, on by default). The PowerConf doesn't change its volume
  itself when they're pressed: they're volume keys sent to the computer, and on a Pi with no desktop nothing acted on
  them before. Any keyboard's volume keys count the same.
- **The speakerphone's own mute** (`[home] speakerphone_mute`, on by default). Its mute button mutes its mic in the
  speakerphone itself, and TARS follows: while it's muted TARS doesn't listen for its name at all (the wake model
  idles and the mic's audio is thrown away, so the Pi does next to nothing), nor for the talk button, and says
  what's due all the same. The remote's mute works the same mute, so the two never disagree. TARS reads the
  speakerphone's buttons and lights from what it says it has (its HID report descriptor: a phone-mute button, mute
  and off-hook lights). The PowerConf takes the computer's mute only off-hook (in a call), so TARS keeps it off-hook,
  which no light shows, and sets the mute. Without one, the remote's mute is TARS's alone: it stops listening, and
  the mic stays on. Either mute outlasts a restart: muted when TARS stopped, it starts muted (`voice_data/muted` is
  there while it's muted). Muted, what's due is still said, after a chime; a reminder waiting for "got it", or
  anything TARS says on its own that asks a question (a skill at its time, the TV's "Toy Story, or Toy Story 4?"),
  unmutes it just for the answer (the light shows it): six seconds of it at most, replied to, and then muted again,
  with no listening for more. The room was muted for a reason.
- **A remote** (`[home] remote`), through an IR receiver: the one in an Argon ONE case, or any wired to a GPIO pin.
  Any of `volume_up`, `volume_down`, `talk`, `stop` and `mute`; a button not named does nothing. The remote's power
  button is the case's own (it turns the Pi on, and shuts it down cleanly).

Linux only. TARS reads the buttons from `/dev/input`, which TARS's user may read in the `input` group (it is on
Raspberry Pi OS), and the speakerphone's mute from its `/dev/hidraw*`, which `deploy/99-tars-speakerphone.rules`
lets the `input` group use (the PowerConf's; another speakerphone's USB ids go beside it). It acts on the volume keys,
the remote's codes and the mute button, and nothing else it reads is kept. Setting up a remote, on the Pi:

1. **Turn the receiver on.** Add `dtoverlay=gpio-ir,gpio_pin=23` to `/boot/firmware/config.txt` (23 is the Argon
   ONE's pin; another receiver says its own), and reboot. `install-pi.sh` installs `deploy/99-tars-ir.rules`, which
   has the receiver decode NEC, what most remotes (the Argon's among them) send: by default it decodes only RC-6.
2. **Read the remote's codes.** `uv run python -m voice_assistant.buttons` prints what each button sends (Ctrl-C to
   stop). Hold each button a second, one at a time, pointed at the receiver: a quick press can be missed.
3. **Name them** in `config.local.toml`, and restart TARS:

   ```toml
   [home]
   own_speaker = true
   # The Argon remote's: OK talks, back stops, menu mutes.
   remote = { volume_up = 0x80, volume_down = 0x81, talk = 0xce, stop = 0x90, mute = 0x9d }
   ```

`--check` says what the buttons do, and what's missing: a receiver that isn't on, devices TARS may not read, or a
speakerphone whose mute TARS can't follow.

### Your own devices

A device Home Assistant doesn't work is a plugin's: a Python module named in `[home.plugins]`, found from the
project's folder (`examples.my_house`) or among those installed.

```toml
[home.plugins.my_house]
module = "examples.my_house"
blaster = { id = "bf0123456789abcdef", address = "192.168.1.50" }   # anything else here is the plugin's own

[[home.devices]]
name = "AC"
kind = "my_house.ac"          # the plugin's name, and one of its kinds
```

The module has `setup(settings, context)`, which returns its kinds of device, each a function that makes one from
its `[[home.devices]]` entry:

```python
from voice_assistant.home import Check, Context, Device, ON, OFF, STATUS


class Kettle(Device):
    actions = (STATUS, ON, OFF)

    def what(self) -> str:  # one line for the model
        return "a kettle: status, on, off"

    def status(self) -> str:  # starting with its name
        return f"{self.name}: cold"

    def call(self, action, args, request) -> str:
        ...  # do it, then say what the device reports
        return f"done: {self.name} reports boiling"


def setup(settings: dict, context: Context) -> dict:
    return {"kettle": lambda spec: Kettle(spec["name"])}
```

- **`call`** answers in the tool's words: `done:` only when the device itself reports it, `sent:` when nothing can
  tell, `started:`, `not confirmed:` or `error:` otherwise. Raising `HomeError` is said as an error. It's called only
  once the turn is over, one device at a time.
- **`actions`** may include the plugin's own ("boil"); the tool offers them. **`rules()`** adds anything the model
  needs to use the kind well, once however many there are. **`slow()`** says "One moment." first. **`check()`** says
  what `--check` should, as `Check(status, detail, fix)` lines.
- **`context`** has the project's folder (`root`, for files under `voice_data/`), `secret(name)` for a key in `.env`,
  Home Assistant (`hass`), `announce(line)` to say something later on its own, `tv_agent(box)`, and `devices`, every
  device made so far (Home Assistant's and the Android TVs first). `tell(text)` tells TARS's maintainers something in
  their chats (a device that's stopped answering), and `background` is true only in the voice assistant's process,
  the one that runs a device's background work (a watch), so nothing is watched, or told, twice. The TV agent knows no app of its own:
  `tv_agent(box, extra_rules=...)` tells it how one of the household's works (my_house: how Stremio lists streams).
- **`stop()`** ends anything the device still does in the background, as TARS shuts down or someone takes over; the
  base class's does nothing.
- **`button(args)`** and **`presses(args)`** read a press as every device does: "volume up" is `volume_up`, and
  `times` is 1 to 15.
- A plugin that won't load, or a device it can't make (raise `ValueError` saying why), costs only those devices:
  TARS starts without them, and `--check` says why.
- **`BACKUP_FILES`**, beside `setup`: its own files and folders under the project's folder, like
  `("voice_data/ac", "voice_data/ir")`, for what it learned that someone would have to teach it again. The
  [backup](backup.md)'s snapshot keeps them, and a restore puts them back. A plugin that won't load costs only its
  own files there too.
- Its tests sit beside it and run with TARS's (`pytest` collects `examples/`), and CI lints it.

[`examples/my_house`](../examples/my_house/README.md) is a real one: an Electra AC with no Wi-Fi of its own, taught
from its remote, and a Samsung TV and a Polk soundbar from their published IR codes, all through a Tuya IR blaster,
and a Fire TV that plays a title on Stremio straight from its link.

### Spotify

Spotify, played through Spotify Connect on an official Spotify app in the house: "play some jazz", "the Beatles", a
song, an album, a mood, "something I like", a Spotify link; then pause, next, previous, the volume, and "what's
playing?". It needs Premium. The model says what kind of thing was asked for, and code finds it:

- **A song or an album:** Spotify gives an app like this no popularity, so it's the artist with the most versions of
  it (an original has its live ones and remixes, a cover one), the first of theirs as Spotify ranks them. A remaster
  is the same recording; a live one or a remix plays only when asked for. When two artists come level ("Hello"), the
  one they play themselves wins; otherwise Spotify's first, with the others said, so "the Adele one" switches.
- **An artist:** that artist. **A playlist:** their own first; two of theirs with the name are asked about.
- **A genre or a mood:** a playlist of it with enough songs in it. Spotify's own playlists are closed to an app like
  this one.
- **"Something I like":** a shuffled mix of their most played and liked songs. Spotify's own picks for them are closed
  too. **"More like this":** more of the artist playing.

It's said to play only once Spotify says so.

```toml
[[home.devices]]
name = "spotify"
kind = "spotify"
play_on = "Fire TV"                   # the Spotify device to play on, or part of its name, as Spotify shows it
wake = "fire tv"                      # optional: the device that opens its app, when it isn't on Spotify till then
```

A TV's Spotify app shows up as a device only while it's open, so `wake` names the device that opens it: an Android TV
box, whose "open" turns the TV on too where it can. Play on an official Spotify app or speaker: Spotify refuses the
audio of unofficial players (librespot, and the raspotify and spotifyd built on it) on some accounts, which then skip
every song in silence while Spotify says they play.

To sign in, once:

1. Make an app at developer.spotify.com (Dashboard, Create app; on a phone it may need the desktop site), for the Web
   API, with the redirect URI `http://127.0.0.1:8888/callback`, and put its client id in .env as `SPOTIFY_CLIENT_ID`.
   There's no secret to keep: the sign-in uses PKCE. Spotify's development mode allows one such app per person, with
   Premium, and five accounts on it.
2. Send `/spotify` to TARS on Telegram. It sends a link to sign in with, then a one-time page, as a login for the
   browser is added (docs/browser.md): sign in and agree, and the browser goes on to an address that won't load. Copy
   it from the address bar and paste it on the page, which works once, for ten minutes. Without Telegram,
   `uv run python -m voice_assistant.spotify login` does the same in a terminal.

The sign-in is kept in `voice_data/spotify.json`, readable by TARS's user alone, and Spotify swaps it for a new one as
it's used. It isn't in the backup, so a restored TARS signs in again.

### Skills

Skills, named things to do, some at set times, are taught by talking to TARS, and can use the house like any other
request: [skills and preferences](skills.md).

### All of Home Assistant

With `[home] url` and its token, TARS also has the `home_assistant` tool (`ha_tool.py`): everything in Home
Assistant, not just the devices listed, the way a person with its app has it. The listed devices stay the home tool's,
which checks a change took and knows each one's quirks; this is for the rest, and for the past.

- **find**: entities by words in their name, room or id, each with its state. **state**: one entity, attributes and
  all.
- **history**: an entity's changes over a span, and the time it spent in each state: "how long was the boiler on
  today?" Home Assistant keeps about ten days of this. **statistics**: a number's hourly averages, kept for good (the
  same table as a sensor's history).
- **logbook**: what happened and what did it ("turned on, by switch.turn_on", or nothing, when it was the device's own
  button).
- **services**: what a domain can do, with its fields. **call**: any of them, on any entity: "done" with the new
  state Home Assistant answers, or "sent" when the state hasn't moved yet.

The same promises hold, in code: a change waits for the turn to go ahead; a reply that searched the web changes
nothing, and doesn't read the house's past either (history, statistics and the logbook say who was home and when, and a
page could ask for that); and unlocking follows [the same rules](#unlocking), whether it's the lock's own service,
another service on the lock, a button on the lock's own device (a Nuki's "unlatch" and "Lock 'n' Go" open the door
without its lock entity) or anything else named for unlocking or unlatching, or a script, automation or scene that
names any of these (one whose setup can't be read counts as if it did). A call's data can't aim it at a whole device,
room or label, which could take the lock in without naming it. It won't administer Home Assistant itself: restarting or updating it, its integrations, users,
backups and recorder, or running code (`ADMIN` in `ha_tool.py`); that's for a person, in Home Assistant.

## Setting it up

About half an hour, on the Pi (or the Mac while you try it).

1. **Home Assistant.** Already running somewhere on the home network? Use that. Otherwise, on the Pi:
   `cd deploy/home-assistant && docker compose up -d` (Docker first: `curl -fsSL https://get.docker.com | sudo sh`).
   Open `http://<the Pi>:8123`, make the owner account, set the home's location, and add your devices' integrations
   (Settings, Devices & services). For devices that report over MQTT, start the broker too. First its passwords, one
   user for Home Assistant and one for each device, made in Mosquitto's own image so the file ends up owned by
   Mosquitto (user 1883) and readable by nobody else, as it requires (it warns about anything looser now, and later
   versions refuse to start):

   ```bash
   cd deploy/home-assistant
   docker run --rm -it -v "$PWD/mosquitto:/m" eclipse-mosquitto:2 sh -c 'touch /m/passwords &&
     chown root:root /m/passwords && chmod 600 /m/passwords &&
     for user in "$@"; do mosquitto_passwd /m/passwords "$user" || exit 1; done &&
     chown 1883:1883 /m/passwords' -- homeassistant nuki
   ```

   It asks for each one's password. A device added later: the same command with just its user, then
   `docker restart mosquitto`. Then `docker compose --profile mqtt up -d`, and add the MQTT integration.
2. **A token for TARS.** In Home Assistant: your profile, Security, Long-lived access tokens, Create token. Put it in
   `.env` as `HOME_ASSISTANT_TOKEN`. A separate Home Assistant user for TARS, not an administrator, limits what the
   token can do.
3. **Say what TARS may work.** In `config.local.toml` (this machine's own settings, never committed): `[home] url`,
   who may unlock, and a `[[home.devices]]` for each device, by the name people call it, with its entity id (Settings,
   Devices & services, Entities). `config.toml`'s `[home]` has every setting, commented.

   ```toml
   [home]
   url = "http://127.0.0.1:8123"
   unlock_voices = ["alon"]          # names from the web UI's Voices page; empty: nobody unlocks by voice
   unlock_chats = ["Alon"]           # names from [telegram] people; empty: nobody unlocks from a chat

   [[home.devices]]
   name = "front door"
   entity = "lock.front_door"
   door = "binary_sensor.front_door_door"
   ```

4. **An Android TV box, if there is one.** On the TV: turn on ADB debugging (on a Fire TV: Settings, My Fire TV,
   About, click the device name seven times, then Developer Options), and check HDMI-CEC is on. Add it with
   `kind = "androidtv"` and its `address` today; it needn't be fixed in the router. TARS keeps where it last found
   the box and its hardware (MAC) address in `voice_data/found.json`, and if the router gives it another address,
   finds it again by knocking on the ADB port across the home network and matching that address. Then run
   `uv run python -m voice_assistant.androidtv pair` and tick "Always allow" on the TV. `... androidtv look` shows
   what TARS sees; `... androidtv do "play Toy Story on Stremio"` tries the agent (the core one, without a plugin's
   rules).
5. **Check it.** `uv run voice-assistant --check` has a line for Home Assistant and one for each device: what it
   reads, whether it answers, and what to do if not. Then restart TARS and try each device aloud;
   `journalctl --user-unit voice-assistant | grep -E 'home:|tv:'` shows every call and what the device answered,
   the devices TARS started with, any that isn't connected and why, and a box or blaster found at a new address.

### Home Assistant's own setup

With `[home] config_dir` (on the Pi, `"deploy/home-assistant/config"`), Home Assistant's setup is kept as a git
history on this machine (`haconfig.py`), so any change to it can be seen and undone: its YAML (automations, scripts,
scenes), its custom integrations, and `.storage`, where it keeps its integrations, devices, entities and areas. Its
logs, its history database and the files it rewrites every few seconds stay out (a `.gitignore` TARS writes).

- **Every backup pass** (every 15 minutes) commits whatever changed since the last, "Changes made in Home Assistant":
  what someone did in Home Assistant's own app is in the history too. The first pass starts it. Passes come from
  [the backup](backup.md)'s timer, so `[backup]` has to be on; `--check` says when it isn't.
- **`uv run voice-assistant --ha-config`** lists the latest changes, newest first, with the files each touched.
- **`uv run voice-assistant --ha-config-restore <change>`** puts the setup back as it was at that change: it stops Home
  Assistant (which writes `.storage` as it stops, and would undo the restore), puts the files back, keeps that as one
  more change, and starts it again. Nothing in the history is rewritten, so a restore can itself be undone.
- **In the backup, sealed.** `.storage` holds the integrations' passwords and tokens, and the keys Home Assistant signs
  its logins with: whoever has those has the house, its lock included. So the history never leaves this machine as it
  is. `uv run voice-assistant --ha-config-key` puts a new `HA_CONFIG_KEY` in `.env` (it doesn't print it: copy it
  from `.env` into a password manager yourself), and each pass after seals the history with it (AES-GCM) for the
  backup's next snapshot. Without the key, nothing goes to the bucket, and `--check` says so. A new Pi opens it with
  `--ha-config-from-backup` ([backup.md](backup.md)); without the key it can't, so keep that copy.

Home Assistant runs as root in its container and writes some files only root may read, so every git command runs as
root in a throwaway container of Home Assistant's own image, which has git: nothing else to install, and it works
whether Home Assistant is running or not. What a change did outside Home Assistant, a Bluetooth device unpaired or a
cloud login revoked, isn't in these files, and a restore can't bring it back: the device may need pairing again.

### Changing Home Assistant's setup

The home_assistant tool also works Home Assistant's setup (`ha_setup.py`), for TARS's maintainers (`[household]
maintainers`; none named: nobody; looking is for anyone):

| Said | What happens |
|---|---|
| "Did Home Assistant find anything new?" | what it found on the network and waits to add: "your Samsung TV, as a media player" |
| "Add the TV" | its setup, step by step: a step that only asks to confirm is confirmed once they say yes |
| "Add a Shelly" | an integration started by name, its questions asked in conversation |
| "Call the Switcher 'boiler' and put it in the bathroom" | the device renamed, and put in that room |
| "Remove the AirVisual integration" | what would go said first, with any device TARS works that would stop; done on a later "yes" |
| "Undo that" | the setup put back as it was before a change, after a "yes": Home Assistant restarts |

- **No secret through a chat.** A step that asks for a password, a token or a code, or a sign-in in a browser, isn't
  filled in by TARS: the reply is a link to that step on Home Assistant's own page (at an address a phone can reach,
  as a camera's live link is), and a password someone types to TARS anyway isn't passed on.
- **Twice for what's hard to take back.** Removing an integration, or undoing a change, takes two turns: the first
  says what would happen, and only a later message from the same person, within ten minutes, does it.
- **Every change kept.** With `[home] config_dir`, each change is committed to [Home Assistant's setup
  history](#home-assistants-own-setup), before and after, and the reply says it can be undone; "undo" restores the
  commit before it.
- **Only what Home Assistant has.** An integration is added by its domain, from those Home Assistant ships with or
  already has installed. Installing new code into it (HACS, a custom integration) isn't something TARS does: that's
  someone else's code running in the house's hub, and it's for a person to read first.

## What leaves the house

Home Assistant, the devices and the TV box are all on the home network; TARS talks to them directly. What the models
see is the conversation, as for any request: which device was asked about, and what the home tool answered
("front door reports locked"). A task on the TV is the exception: the model working it sees the TV's screen, as text
and as screenshots, while it works. A plugin may reach out too, and says so: `examples/my_house` looks titles up on
IMDb. A camera's snapshot or live link goes to the person who asked: the snapshot to their TARS page (kept on the
machine) and their Telegram chat (to Telegram, like any file it sends), and the live link is to Home Assistant on the
home network by a name their phone can reach (its Tailscale name if it has one), signed so it opens without the token
and expires on its own. The models never see the picture.

## Things to know

- **The token is a key to the house.** It lives in `.env`, which never leaves the machine.
- **The web UI has no accounts**, and TARS can lock the door and run the boiler from what it hears. Unlocking is the
  one change it won't make without a voice it knows, sure, asking for it.
- **"Sent" is honest, and an IR device's status is a guess.** An IR device can't report back: TARS says it sent the
  command, and its status says it's Home Assistant's guess. So asked to change one, TARS does even when the status
  says it's already that way ("turn off the AC" when it says off): someone may have used its remote since. Asked to
  set an AC Home Assistant has as off, it turns it on first, since an IR one may send nothing while it's "off". And
  someone can say "the AC is actually off" when it didn't take.
- **ADB is a key to the TV.** Anyone on the home network whose key the TV accepted can work it; TARS's key is in
  `voice_data/androidtv/`. The TV's "Allow USB debugging?" is what lets a new one in.
- **A device that doesn't answer costs a sentence, not the reply.** Home Assistant down, a box off the Wi-Fi: TARS
  says what didn't work, and everything else goes on.
- **Who's home** is a device too, `who's home`, with only a status, when [presence](presence.md) is on: "who's home?"
  is answered from whose phone is on the home Wi-Fi. It's the one device a Telegram chat has.
- **Not yet:** working the house from Telegram (a chat can't unlock, but could check and change the rest), scenes
  ("going to bed"), evals of the TV's agent on a real TV, and a saved run of the house's scenario evals
  ([evals](evals.md#method)).
