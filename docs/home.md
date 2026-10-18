# The house

TARS works the house by voice: "is the front door locked?", "lock it", "boiler on for 40 minutes", "turn the AC on,
22 degrees", "turn everything off". The lock (a Nuki) and the boiler (a Switcher) go through
[Home Assistant](https://www.home-assistant.io) on the home network; the AC (an Electra central unit, with no Wi-Fi
module) goes through a Tuya IR blaster that TARS sends codes to directly.

Everything here stays on the home network. Home Assistant, the MQTT broker the Nuki reports to and the blaster are all
local, and nothing goes through Tuya's or Nuki's cloud. What the models see is the conversation, as for any request:
which device was asked about, and what the home tool answered ("the front door is locked").

## How it works

The brain has one more tool, `home` (`home.py`), with a device, an action and the settings an action needs. Qwen uses
it itself when `[llm] quick_tools` is on, and hands the turn to OpenAI's model otherwise, like reminders.

**A change is only "done" when the device says so.** After asking Home Assistant to lock the door, the tool reads the
lock's state until it reports locked, for up to 12 seconds, and answers one of:

- `done: the front door is locked (it says so itself)`,
- `not confirmed: asked the front door to lock, and it still says locking`,
- `error: the front door jammed; someone needs to check it`, or `error: Home Assistant didn't answer`.

The model is told to say what the tool answered, never that something's done when it isn't. TARS says "One moment."
first while a lock turns, so the wait isn't silent. A door sensor that says the door is open stops TARS locking it:
the bolt would go into the frame.

**A draft changes nothing.** TARS starts answering at the first pause, before it knows you've finished
([response time](latency.md)). A change waits until the turn is over (`Draft.go()` in `assistant.handle`), and a
draft thrown away because you kept talking ("turn off the boiler... no, leave it") never reaches the device.

**Unlocking has its own rules, in code, not in the prompt,** so no wording gets around them:

- only by voice, in the house: never from a typed message (`--text`) or anything else that isn't the microphone;
- only for the names in `[home] unlock_voices`, and only when speaker ID is surer than `unlock_threshold` (0.6, against
  0.5 to greet someone by name);
- only after TARS asked "Unlock the front door?" and heard a yes in the next turn, from the same voice, within a minute.
  A model that answers its own question in the same turn gets "ask first" back.

Locking needs none of that: anyone can lock.

**The boiler** turns on, off, or on for some minutes with the Switcher's own timer (1 to 150), so it goes off even if
TARS doesn't. Its status says how long it has left.

### The AC

An AC remote sends its whole screen with every press: mode, temperature, fan, swing. Electra's also sends power as a
toggle: one bit that means "switch over". The same press turns the AC on when it's off and off when it's on, and a
press without that bit changes the settings and leaves the power alone. That's why the Smart Life app's on button
sometimes did nothing: its "on" was a settings-only code, and its "off" was the toggle.

So TARS doesn't use the app's codes. `ac_onboard` records the AC's own remote, works out which bits are the power
toggle, the mode, the temperature, the fan and the swing, and checks the result against every press it heard, bit for
bit ([teaching it the remote](#the-ac-teaching-tars-the-remote)). From that profile TARS makes any setting the remote
could, and decides itself whether to send the toggle:

- **on**: the toggle only if TARS thinks the AC is off; otherwise only the settings.
- **off**: the toggle only if TARS thinks it's on; otherwise nothing is sent.
- **set** (a temperature, a mode, a fan speed): only the settings, never the power.
- **is_on**, **is_off**: someone says "the AC is on", and TARS corrects what it thinks, sending nothing.

IR goes one way: TARS knows what it sent, not what the AC did. It keeps what it believes in
`voice_data/ac/state.json`, says "sent", not "it's on", and is corrected when someone used the wall controller. A
light sensor on the AC's own indicator (the `state` setting: any `binary_sensor` in Home Assistant, on when the light
is) makes it sure: TARS reads the light before deciding to toggle, checks it changed, and sends the toggle once more
if the first was missed.

## Setting it up

About an hour, on the Pi, in this order. Home Assistant runs beside TARS in Docker; nothing else on the Pi changes.

### 1. Home Assistant and Mosquitto

```bash
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER   # then log out and back in
cd ~/voice-assistant/deploy/home-assistant
docker run --rm -v "$PWD/mosquitto:/m" eclipse-mosquitto:2 sh -c \
  'touch /m/passwords && mosquitto_passwd -b /m/passwords homeassistant PICK-ONE && mosquitto_passwd -b /m/passwords nuki PICK-ANOTHER'
docker compose up -d
```

Open `http://<the Pi>.local:8123`, make the owner account, and set the home's location. Then add the MQTT broker:
**Settings > Devices & services > Add integration > MQTT**, broker `127.0.0.1`, port 1883, user `homeassistant` and
its password.

### 2. The Nuki

In the Nuki app, open the lock's settings and find **MQTT** (under Smart Home, or Features & Configuration,
depending on the app's version). Turn it on with:

- host: the Pi's address on the home network (give it a fixed one in the router),
- port 1883, user `nuki` and its password,
- **Home Assistant auto discovery** on.

The lock appears in Home Assistant by itself, as `lock.<its name>`, with a battery sensor and, with the door sensor,
a door one. Leave the lock's Wi-Fi power mode on Automatic.

### 3. The Switcher

**Settings > Devices & services**: Home Assistant usually finds the Switcher on its own; otherwise **Add integration >
Switcher**. Its entities are `switch.<its name>` and `sensor.<its name>_remaining_time`.

### 4. TARS and Home Assistant

In Home Assistant, open your profile (bottom left), then **Security > Long-lived access tokens > Create token**, and
put it in TARS's `.env`:

```bash
HOME_ASSISTANT_TOKEN=eyJ...
```

Then in `config.toml`'s `[home]`, with the entity ids from **Settings > Devices & services > Entities**:

```toml
[home]
url = "http://127.0.0.1:8123"
unlock_voices = ["alon"]

[[home.devices]]
name = "front door"
kind = "lock"
entity = "lock.front_door"
door = "binary_sensor.front_door_door_sensor"

[[home.devices]]
name = "boiler"
kind = "boiler"
entity = "switch.boiler"
remaining = "sensor.boiler_remaining_time"
```

`uv run voice-assistant --check` now shows Home Assistant and each device, with what it says right now.

### 5. The AC: the blaster's key

TARS talks to the blaster on the home network with [tinytuya](https://github.com/jasonacox/tinytuya), which needs
the blaster's id and its local key. The key is in Tuya's cloud, read once:

1. At [platform.tuya.com](https://platform.tuya.com), make a free account and a cloud project (Smart Home; the data
   center your Smart Life account is in: Central Europe for an account made in Israel).
2. In the project, **Devices > Link App Account**, and scan the code with the Smart Life app (Me > the scan icon).
3. On the Pi, in `~/voice-assistant`: `uv run python -m tinytuya wizard`, with the project's Access ID and Secret. It
   lists every device with its id and local key. Then `uv run python -m tinytuya scan` gives the blaster's address
   and protocol version. Give it a fixed address in the router.

Put the key in `.env` and the blaster in `config.toml`:

```bash
TUYA_LOCAL_KEY=0123456789abcdef
```

```toml
[[home.devices]]
name = "AC"
kind = "ac"
blaster = "bf0123456789abcdef"
address = "192.168.1.50"
version = "3.3"
```

The key changes if the blaster is ever removed from Smart Life and added again; then read it again.

### 6. The AC: teaching TARS the remote

Have the Electra remote and the AC in view, the AC off, and the blaster where the remote can reach it from about 30
cm. Then:

```bash
uv run python -m voice_assistant.ac_onboard check     # the blaster answers
uv run python -m voice_assistant.ac_onboard capture   # 19 presses, about five minutes
uv run python -m voice_assistant.ac_onboard fit       # works out the code, checks it against every press
uv run python -m voice_assistant.ac_onboard try       # four codes from TARS; you say what the AC did
```

**capture** asks for one button at a time (temperature up and down, fan, mode, swing, then power twice) and, after
each, what the remote's screen shows; Enter takes its guess. Between captures it asks you to set the remote some way
first, pointed away from the blaster: those presses aren't recorded. Buttons other than POWER shouldn't turn the AC on:
if one does, the power bit isn't a toggle, and `fit` will say what it found instead. A press that didn't come through
whole is asked for again; `capture --again "mode 3"` redoes one later. Everything is kept in
`voice_data/ac/captures.json`.

**fit** prints what it found, like:

```text
- read 19 presses: manchester, 34 bits, sent 3 times
- power: bits 0..0, press=1, none=0
- temperature: bits 11..14, 16 to 30 degrees
- fan: bits 4..5, auto=11, low=00, medium=01, high=10
- swing: bits 8..8, on=1, off=0
- mode: bits 1..3, cool=001, heat=010, ...
- made 19 of 19 presses again, bit for bit
```

It writes `voice_data/ac/profile.json` only when it can make the presses again exactly. A mode that writes some
setting its own way (fan mode often blanks the temperature) is left out rather than sent wrong. If nothing can be
trusted, it says why and writes nothing: send `captures.json` and what it printed.

**try** sends four codes: settings without the toggle (the AC should stay off), the toggle (on), a new temperature
without the toggle (still on, now 26), the toggle (off). Four yeses mark the profile checked, and TARS starts out
knowing the AC is off. `send "cool 22 auto" --toggle` sends any one code by hand.

### 7. Try it

```bash
uv run voice-assistant --check          # Home Assistant, the devices, the AC
uv run voice-assistant --text           # "is the door locked?", "boiler on for 20 minutes", "AC on, 23"
systemctl --user restart voice-assistant
```

Typed questions can do everything but unlock. Said aloud, "hey TARS, unlock the front door" gets "Unlock the front
door?", and a yes unlocks it.

## Things to know

- **The token is a key to the house.** A Home Assistant token can do anything Home Assistant can; it lives in `.env`,
  which never leaves the Pi. A separate Home Assistant user for TARS, not an administrator, limits it further.
- **The web UI has no accounts**, and TARS can lock the door and run the boiler from what it hears. Unlocking is the
  one thing it won't do without a voice it knows, sure, asking first.
- **IR needs line of sight.** The blaster has to see the AC's receiver; one press missed is one TARS can't know about
  without the light sensor.
- **A device that doesn't answer costs a sentence, not the reply.** Home Assistant down, the blaster off the Wi-Fi: TARS
  says what didn't work, and everything else goes on.
- **Not yet:** the TV (a Fire Stick, through Home Assistant's Android TV integration, with screenshots to check what's
  on screen), scenes ("going to bed"), and checking the AC from a temperature sensor at a vent.
