# One household's own devices

This is how one house plugs into TARS: a [plugin](../../docs/home.md#your-own-devices) for what Home Assistant
doesn't work there, and the Home Assistant setup for what it does. It's an example, not something to follow: your
devices will differ, and most of them need no plugin at all.

| Device | How | Here |
|---|---|---|
| Nuki smart lock | Home Assistant, over MQTT | `lock.front_door` in config |
| Switcher water heater | Home Assistant, with its own timer | `switch.boiler` in config |
| Electra AC, no Wi-Fi of its own | the plugin: a Tuya IR blaster, codes learned from its remote | `ac.py`, `ac_onboard.py` |
| Samsung TV, Polk Signa S2 soundbar | the plugin: the same blaster, their published IR codes | `ir.py` |
| Fire TV Stick | TARS's Android TV box, with "play" going straight to Stremio | `direct_play.py` |

The blaster (`blaster.py`) is shared: one Tuya "Smart IR" on the home network, worked locally with
[tinytuya](https://github.com/jasonacox/tinytuya), never through Tuya's cloud.

## The config

In `config.local.toml`, beside `config.toml`:

```toml
[home]
url = "http://127.0.0.1:8123"
unlock_voices = ["alon"]

[home.plugins.my_house]
module = "examples.my_house"
blaster = { id = "bf0123456789abcdef", address = "192.168.1.50", version = "3.5", control_type = 2 }

[[home.devices]]
name = "front door"
entity = "lock.front_door"
battery = "sensor.front_door_battery"

[[home.devices]]
name = "boiler"
entity = "switch.boiler"
details = ["sensor.boiler_remaining_time"]
timer = { service = "switcher_kis.turn_on_with_timer", field = "timer_minutes", max = 150 }

[[home.devices]]
name = "AC"
kind = "my_house.ac"
state = "binary_sensor.ac_light"      # optional: a light sensor on the AC's own indicator, in Home Assistant

[[home.devices]]
name = "TV"
kind = "my_house.remote"
codes = "samsung-tv"
awake_from = "fire tv"                # the Fire TV sleeps and wakes with the TV (HDMI-CEC): it says if the TV is on

[[home.devices]]
name = "soundbar"
kind = "my_house.remote"
codes = "polk-re9641"

[[home.devices]]
name = "fire tv"
kind = "my_house.firetv"
address = "192.168.1.60"
```

and in `.env`, beside `HOME_ASSISTANT_TOKEN`, the blaster's local key as `TUYA_LOCAL_KEY`. `uv sync --all-extras`
installs tinytuya (the `my-house` extra) and adb-shell (`tv`).

## The AC

An AC remote sends its whole screen with every press: mode, temperature, fan, swing. Electra's also sends power as a
toggle: one bit that means "switch over". The same press turns the AC on when it's off and off when it's on, and a
press without that bit changes the settings and leaves the power alone. That's why the Smart Life app's on button
sometimes did nothing: its "on" was a settings-only code, and its "off" was the toggle.

So TARS makes its own codes. It starts from the one published for Electra's remotes
([IRelectra](https://github.com/barakwei/IRelectra): 34 bits, Manchester, power as a toggle), and if the AC doesn't
answer to it, `ac_onboard` records the AC's own remote, works out which bits are the toggle, the mode, the
temperature, the fan and the swing, and checks the result against every press it heard, bit for bit. From that
profile (`voice_data/ac/profile.json`) TARS makes any setting the remote could, and decides itself whether to send
the toggle:

- **on**: the toggle only if TARS thinks the AC is off; otherwise only the settings.
- **off**: the toggle only if TARS thinks it's on; otherwise nothing is sent.
- **set** (a temperature, a mode, a fan speed): only the settings, never the power.
- **is_on**, **is_off**: someone says "the AC is on", and TARS corrects what it thinks, sending nothing.

IR goes one way: TARS knows what it sent, not what the AC did. It keeps what it believes in
`voice_data/ac/state.json`, says "sent", not "it's on", and is corrected when someone used the wall controller. A
light sensor on the AC's indicator (`state`: any `binary_sensor` in Home Assistant, on when the light is) makes it
sure: TARS reads the light before deciding to toggle, checks it changed, and sends the toggle once more if the first
was missed, but never on a slow sensor's old reading, which would turn the AC straight back (`ac.py` says how).

## The TV and the soundbar

Their codes are published, one fixed code per button (`ir.py`): Samsung's from
[IRDB](https://github.com/probonopd/irdb), Polk's RE9641 remote (the Signa S2's) from
[Flipper-IRDB](https://github.com/Lucaslhm/Flipper-IRDB). The soundbar does volume, mute, bass, voice, night and its
sound modes; the TV its inputs and menus, and on and off. Besides the power button, which switches it either way,
Samsung has codes that only turn it on and only off, so "turn on the TV" can never turn it off. Not every set takes
them: `python -m examples.my_house.ir test TV` tries each on yours. If yours ignores them, TARS uses the power button
instead, but only when the Fire TV says the TV is the other way.

## The Fire TV

It's TARS's own Android TV box ([the house](../../docs/home.md#an-android-tv-box)), with one change: "play Ratatouille"
goes straight to Stremio, in seconds and with no model working the screen. The title is looked up on IMDb's public
search suggestions (Stremio's add-ons go by IMDb's ids), its page opened by link
(`stremio:///detail/movie/tt0382932/tt0382932`, or a series' `.../tt13443470:1:3` for season 1 episode 3), the first
stream chosen, and it's said to be playing only once the stick's media session says so. A series without an episode
opens its page, and TARS asks which. Another app, or a title IMDb doesn't find, goes to the agent as usual, told how
Stremio lists streams (the plugin's own rule for it). The title asked for is all that goes to IMDb.

## Setting it up

The house's Home Assistant runs on the Pi ([setting it up](../../docs/home.md#setting-it-up)), with the MQTT broker
for the Nuki (`docker compose --profile mqtt up -d`).

1. **The Nuki.** In the Nuki app, the lock's settings, MQTT (under Smart Home, or Features & Configuration): host the
   Pi's address, port 1883, user `nuki` with its Mosquitto password, and Home Assistant auto discovery on. Leave
   Wi-Fi power mode on Automatic. The lock connects to the Pi, so the Pi's address mustn't change: use its name on the
   home network if the app takes one (the router's, like `homepi.lan`), or fix the address, in the router or, if the
   router can't, on the Pi itself (an address outside the router's range). The lock, its door sensor and its
   battery appear in Home Assistant within a few minutes; if not, reload the MQTT integration. Use the ids they have
   (`lock.front_door_2` is fine).
2. **The Switcher.** Home Assistant finds it on the home network: add the Switcher integration. A newer Switcher asks
   for the account's email and a token, from the page the
   [integration's documentation](https://www.home-assistant.io/integrations/switcher_kis) links to.
3. **The blaster.** `uv run python -m tinytuya scan` lists Tuya devices on the network with their id, address and
   version. The local key is only in Tuya's cloud, once: at platform.tuya.com, make a free account and a Cloud
   project (Smart Home, data center Central Europe for a Smart Life account made in Israel); in it, Devices, Link App
   Account, and scan the code with the Smart Life app; then `uv run python -m tinytuya wizard` with the project's
   Access ID and Secret. It writes `devices.json` (git ignores it): the blaster is the entry whose category is
   `wnykq`. Put its key in `.env` as `TUYA_LOCAL_KEY`, its id, address and version in the plugin's `blaster`, and
   delete `devices.json`, `tinytuya.json`, `snapshot.json` and `tuya-raw.json`. Its address needn't be fixed: if the
   router gives it another, TARS finds it again by its id, which it announces on the home network every few seconds
   (`voice_data/found.json` keeps where it is now).
   If `--check` says the blaster "didn't say which IR protocol it speaks" (a v3.5 Smart IR doesn't), set
   `control_type`: 2 for the newer protocol, or 1; the one the TV obeys with `python -m examples.my_house.ir press TV
   power` is it.
4. **The AC.** `uv run python -m examples.my_house.ac_onboard published` writes Electra's published profile, then
   `... ac_onboard try`, watching the AC: it sends four codes and asks what the AC did each time (settings without
   the toggle, the toggle, 26 degrees without it, the toggle again). Four yeses mark the profile checked. If the
   second didn't turn the AC on, record its remote instead: with the remote 30 cm from the blaster,
   `... ac_onboard capture` asks for one button at a time and what the remote's screen shows after it; then
   `... ac_onboard fit`, which writes the profile only if it makes every press again bit for bit, and `try`.
5. **The TV and the soundbar.** `uv run python -m examples.my_house.ir press soundbar volume_up` should make it
   louder, and `... ir test TV`, with the TV on, tries its on, off and power codes and saves what worked.
6. **The Fire TV.** As any Android TV box ([setting it up](../../docs/home.md#setting-it-up), step 4).

`uv run voice-assistant --check` then has a line for each: the blaster answers, the AC's profile was tried on the
AC, the TV's codes were tried on it, and the Fire TV answers.

## Moving from the first version

Before this was a plugin, all of it was part of TARS (the `house-v1` tag). To move a house set up then:

- `mv voice_data/firetv voice_data/androidtv`: the Fire TV's ADB key, so it isn't paired again.
- In `config.local.toml`: `[home] blaster` moves to `[home.plugins.my_house] blaster`, beside
  `module = "examples.my_house"`; the AC's `kind = "ac"` becomes `"my_house.ac"`; an `ir` device becomes
  `kind = "my_house.remote"` (and the TV gains `awake_from = "fire tv"`); the Fire TV becomes `"my_house.firetv"`;
  the lock and the boiler drop `kind`, and the boiler's `remaining` becomes `details = [...]` with its `timer`.
- `voice_data/ac/` and `voice_data/ir/` stay as they are: the AC's profile, what TARS thinks of it, and which TV codes
  worked.
