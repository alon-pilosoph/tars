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
| "Play Toy Story on Stremio" | "On it.", and the TV's agent works it out on screen; "Toy Story is playing." after a chime |
| "Is everything off?" | every device's status at once |

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

### Unlocking

Unlocking is the one change that needs more than a request. When someone says "unlock the front door":

1. Speech to text writes down what was said, and speaker ID works out who said it, from the same recording.
2. The model calls `unlock`, at once, with no question first.
3. The tool checks four things before anything is sent:
   - **by voice**: the request came through the microphone, not a typed `--text` question or a Telegram chat;
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
| `sensor`, `binary_sensor` | status | |

Any of them can have `details`, other entities read out with its status (`["sensor.boiler_time_left"]`). The device
names are what people call them, and the model gets each one's name and what it can do.

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
4?"), and TARS listens for the answer without the wake word. Asking the TV for anything else stops the agent, and waits
for the step it's on (an app opening, a wait) to end first, so the two never fight over the screen. A press is never
sent twice: one the box may have had before it stopped answering isn't tried again. A task costs a few cents of OpenAI:
each step is one request, with a screenshot when the app's text isn't enough, and only the latest screenshot is sent
again.

### TARS's own speaker

With `[home] own_speaker = true`, TARS's own speaker is one of the devices, for the voice: "TARS's speaker", which
"you", "yourself" and "your volume" are about (`volume.py`). Its status is the volume; `set` takes a level, a percent;
`press` takes volume up or down (10% a press) or unmute. Only the voice's processes have it: a chat has no speaker of
TARS's to turn.

- **On the Pi** it's the playback volume of the sound card TARS plays to, through Linux's sound mixer (`amixer`, in
  `alsa-utils`). The card is the one in the device's name as `--list-devices` shows it ("Anker PowerConf: USB Audio
  (hw:2,0)" is card 2), so `[audio] output_device` must name the speakerphone: the system's "default" doesn't say
  which card it is, and TARS leaves its speaker out rather than guess. The control is one with a playback volume,
  never the microphone's, set as the percent alsamixer shows.
- **On a Mac** it's the system's output volume, so only when TARS plays to the default output.

It's never muted or set below 10%: a TARS that can't be heard can't say a timer is up, or that it's turned itself
back up. Asked to mute or be quiet, it goes down to 10% and says that's as low as it goes; anything it sets unmutes it.
It reads the level back after setting it and says "done" only when that's what it set. A speakerphone's own volume
buttons may change its volume without telling the computer (not yet checked on the PowerConf), so after those, the
level TARS says may be off until it's next set by asking. At startup, and in `--check`, a speaker it can't turn is
said with why.

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

    def what(self) -> str:                      # one line for the model
        return "a kettle: status, on, off"

    def status(self) -> str:                    # starting with its name
        return f"{self.name}: cold"

    def call(self, action, args, request) -> str:
        ...                                     # do it, then say what the device reports
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
  device made so far (Home Assistant's and the Android TVs first). The TV agent knows no app of its own:
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

## Setting it up

About half an hour, on the Pi (or the Mac while you try it).

1. **Home Assistant.** Already running somewhere on the home network? Use that. Otherwise, on the Pi:
   `cd deploy/home-assistant && docker compose up -d` (Docker first: `curl -fsSL https://get.docker.com | sudo sh`).
   Open `http://<the Pi>:8123`, make the owner account, set the home's location, and add your devices' integrations
   (Settings, Devices & services). For devices that report over MQTT, start the broker too: make
   `mosquitto/passwords` with `mosquitto_passwd` (one user for Home Assistant, one for each device), then
   `docker compose --profile mqtt up -d`, and add the MQTT integration.
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

## What leaves the house

Home Assistant, the devices and the TV box are all on the home network; TARS talks to them directly. What the models
see is the conversation, as for any request: which device was asked about, and what the home tool answered
("front door reports locked"). A task on the TV is the exception: the model working it sees the TV's screen, as text
and as screenshots, while it works. A plugin may reach out too, and says so: `examples/my_house` looks titles up on
IMDb.

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
