# The house

TARS works the house by voice: "is the front door locked?", "lock it", "boiler on for 40 minutes", "turn the AC on,
22 degrees", "turn the volume up a bit", "play Toy Story on Stremio". The lock (a Nuki) and the boiler (a Switcher) go
through [Home Assistant](https://www.home-assistant.io) on the home network; the AC (an Electra central unit, with no
Wi-Fi module), the TV (a Samsung) and the soundbar (a Polk Signa S2) through a Tuya IR blaster that TARS sends codes to
directly; the Fire TV stick over ADB, also on the home network.

Everything here stays on the home network. Home Assistant, the MQTT broker the Nuki reports to, the blaster and the
Fire TV are all local, and nothing goes through Tuya's or Nuki's cloud. What the models see is the conversation, as for
any request: which device was asked about, and what the home tool answered ("the front door is locked"). A task on
the TV ("play Toy Story") is the exception: the model working it sees the TV's screen, as text and as screenshots.

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

### The TV, the soundbar and the Fire TV

The Samsung and the Polk need no recording: their codes are published, one fixed code per button, and TARS makes them
itself (`ir.py`): Samsung's from [IRDB](https://github.com/probonopd/irdb), Polk's RE9641 remote (the Signa S2's) from
[Flipper-IRDB](https://github.com/Lucaslhm/Flipper-IRDB). The soundbar does volume, mute, bass, voice, night and its
sound modes; the TV its inputs and menus, and on and off. Besides the power button, which switches it either way (what
the Smart Life app's TV remote sends), Samsung has codes that only turn it on and only turn it off, so "turn on the
TV" can never turn it off. Not every set takes them: `python -m voice_assistant.ir test TV` tries each on yours. If
yours ignores them, TARS uses the power button instead, but only when the Fire TV says the TV is the other way (it
sleeps and wakes with the TV, over HDMI-CEC). "Turn on the TV" also wakes the Fire TV, so they agree.

**The Fire TV** (`firetv.py`) is worked over ADB, which answers back: TARS knows whether it's awake, which app is in
front, what's playing (the app's media session: "playing: Toy Story"), and the text on screen (the app's accessibility
tree, the focused element marked), and can take a screenshot. So its changes are checked like the lock's: "open
Stremio" is done only when Stremio is in front, and still there three seconds later. Stremio sometimes crashes a moment
after it starts, the first time after the stick boots: then TARS stops it and opens it again, up to three times, and
the agent does the same whenever an app closes under it.

**A task that takes several steps** ("play Toy Story on Stremio", "find the news on YouTube") goes to the Fire TV's
agent: a model (`[home] tv_model`, OpenAI's `[llm] model` by default) that works the remote the way a person would.
Each step it chooses from what it sees (look, a screenshot), presses, types or opens an app, then looks again to check
it did what it meant, for up to 30 steps and two and a half minutes. It runs in the background: TARS answers "On it."
at once, and when the agent is done, TARS says how it went after a chime, like a reminder: "Toy Story is playing.",
or why not ("Stremio found no streams for Toy Story."). If it isn't clear which was meant, it asks ("Toy Story, or
Toy Story 4?"), and TARS listens for the answer without the wake word. Pressing anything on the TV through TARS stops
it. A task costs a few cents of OpenAI: every step is one request, with a screenshot when the app's text isn't
enough.

## Setting it up

About an hour, on the Pi. This is written to be driven by an agent (Claude Code, say) working on the Pi over SSH, with
the person beside it for what needs hands, a phone or an account; it works the same by hand.

**How to work through it (for the agent):**

- Work on the Pi, in `~/voice-assistant`, on the branch with the house in it, set up by `deploy/install-pi.sh`.
- Go in order: each step needs the ones before it. Start each with its **done when** check, and skip the step if it
  already passes: running this again after a stop picks up where it left off.
- **You do** is yours to run. **Ask the person** is one thing at a time, in the words given; then run **verify**
  yourself, rather than taking "done" on trust. If the check fails, say what it shows and ask again; don't move on.
- Commands marked *(the person's terminal)* prompt as they go: give the person the exact command to run in a terminal
  on the Pi (`ssh homepi.local`, then `cd ~/voice-assistant`), and wait for them to say it finished.
- Secrets go straight into `.env`, never into the conversation: the token, the blaster's key, the MQTT passwords. The
  one the person has to see is the Nuki's MQTT password, since they type it into the Nuki app.
- Ask before anything that needs `sudo`, and before restarting TARS. After every change to `config.toml`, run the
  config check below; it says what's wrong in one line.
- When something doesn't work, look before guessing: `uv run voice-assistant --check`,
  `docker logs homeassistant --tail 50`, `journalctl --user-unit voice-assistant -n 50`.

These are used throughout:

```bash
cd ~/voice-assistant
config_ok() { uv run python -c "from pathlib import Path; from voice_assistant.config import load_config; load_config(Path('config.toml')); print('config ok')"; }
env_set() { touch .env; grep -v "^$1=" .env > .env.tmp; printf '%s=%s\n' "$1" "$2" >> .env.tmp; mv .env.tmp .env; chmod 600 .env; }
HA=http://127.0.0.1:8123
ha() { curl -s -H "Authorization: Bearer $(grep '^HOME_ASSISTANT_TOKEN=' .env | cut -d= -f2-)" -H 'Content-Type: application/json' "$@"; }
entities() { ha "$HA/api/states" | python3 -c "import json,sys; [print(s['entity_id'], s['state']) for s in json.load(sys.stdin) if s['entity_id'].split('.')[0] in sys.argv[1:]]" "$@"; }
```

### 0. The Pi is ready

**Done when** `git branch --show-current` says `home-assistant` (or a branch with it merged),
`uv run python -c "import tinytuya, adb_shell"` prints nothing, and `df -h /` shows at least 4 GB free (Docker and
Home Assistant take about 3 GB; the card had 9 GB free after TARS).

**You do:** `git fetch && git checkout home-assistant && deploy/install-pi.sh`. It installs the extras (tinytuya,
adb-shell) and restarts TARS.

### 1. Home Assistant and Mosquitto

**Done when** `curl -s -o /dev/null -w '%{http_code}' $HA/api/` says `401` (running, wants a token), and
`docker ps --format '{{.Names}}'` lists `homeassistant` and `mosquitto`.

**You do** (ask before installing Docker: it needs `sudo`):

```bash
command -v docker || { curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER; }
# a new login picks up the docker group: reconnect over SSH (or put sudo before docker until then)
env_set MQTT_HA_PASSWORD "$(openssl rand -hex 12)"
env_set MQTT_NUKI_PASSWORD "$(openssl rand -hex 12)"
cd deploy/home-assistant
docker run --rm -v "$PWD/mosquitto:/m" eclipse-mosquitto:2 sh -c "touch /m/passwords && \
  mosquitto_passwd -b /m/passwords homeassistant $(grep '^MQTT_HA_PASSWORD=' ../../.env | cut -d= -f2) && \
  mosquitto_passwd -b /m/passwords nuki $(grep '^MQTT_NUKI_PASSWORD=' ../../.env | cut -d= -f2) && \
  chmod 600 /m/passwords && chown 1883:1883 /m/passwords"   # the broker's user (1883) must own it, and only it
test -f mosquitto/passwords || echo "no passwords file: compose would mount a folder in its place"
docker compose up -d
cd ../..
until [ "$(curl -s -o /dev/null -w '%{http_code}' $HA/api/)" = 401 ]; do sleep 5; done   # a minute or two
```

**Ask the person:** "Open http://homepi.local:8123 on your phone or laptop, make the owner account, and set the home's
location. Tell me when you see the Overview page." Then: "Open your profile (bottom left), Security, Long-lived
access tokens, Create token, name it TARS, and paste it here." Put it straight into `.env`, and don't repeat it:

```bash
env_set HOME_ASSISTANT_TOKEN '<the token>'
```

**Verify:** `ha $HA/api/` says `{"message":"API running."}`, and
`ha $HA/api/config | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])"` says `RUNNING`. Right after
the first start it can say `NOT_RUNNING` for a few minutes while Home Assistant installs what it needs: wait for
`RUNNING` before going on, since MQTT discovery (step 3) only starts then.

### 2. The MQTT broker in Home Assistant

**Done when** `ha $HA/api/config/config_entries/entry | grep -c '"domain":"mqtt"'` is at least 1.

**You do:** add it through Home Assistant's config flow API, then answer the form it returns:

```bash
FLOW=$(ha -X POST $HA/api/config/config_entries/flow -d '{"handler":"mqtt"}')
echo "$FLOW"    # a form: its step_id and the fields it wants (broker, port, username, password)
ID=$(echo "$FLOW" | python3 -c "import json,sys; print(json.load(sys.stdin)['flow_id'])")
PW=$(grep '^MQTT_HA_PASSWORD=' .env | cut -d= -f2)
ha -X POST $HA/api/config/config_entries/flow/$ID -d "{\"broker\":\"127.0.0.1\",\"port\":1883,\"username\":\"homeassistant\",\"password\":\"$PW\",\"other_settings\":{\"set_client_cert\":false,\"set_ca_cert\":\"off\",\"transport\":\"tcp\"}}"
```

The answer should say `"type":"create_entry"`. That's the form as Home Assistant 2026.10 has it (`other_settings`
holds the TLS and transport settings). If it answers the form again, read its `errors` and `data_schema` and send what
it asks for; if it says `cannot_connect`, check `docker logs mosquitto`; if it aborts, show the person what it said and do it in the browser instead: Settings >
Devices & services > Add integration > MQTT, broker `127.0.0.1`, port 1883, user `homeassistant`, and the password
from `.env`.

### 3. The Nuki

**Done when** `entities lock` lists a lock.

**You do:** find the Pi's address on the home network (`hostname -I | cut -d' ' -f1`), and the Nuki's MQTT password
(`grep '^MQTT_NUKI_PASSWORD=' .env | cut -d= -f2`).

**Ask the person:** "In the Nuki app, open the lock's settings and find MQTT (under Smart Home, or Features &
Configuration). Turn it on with: host `<the Pi's address>`, port 1883, user `nuki`, password `<the password>`, and
Home Assistant auto discovery on. Leave Wi-Fi power mode on Automatic. Also, in your router, give the Pi a fixed
address, so the lock can always find it."

**Verify:** wait for the lock to appear, for up to five minutes:
`until entities lock | grep -q .; do sleep 10; done; entities lock binary_sensor sensor | grep -i -E 'lock|nuki|door|batter'`.
Note the lock's entity, its door sensor if there is one (a `binary_sensor` with "door" in it) and its battery. If
nothing appears and the Nuki app says MQTT is connected: check Home Assistant says `RUNNING` (step 1), then reload the
MQTT integration, which reads what the lock announced again:
`ha -X POST $HA/api/config/config_entries/entry/$(ha $HA/api/config/config_entries/entry | python3 -c "import json,sys; print(next(e['entry_id'] for e in json.load(sys.stdin) if e['domain'] == 'mqtt'))")/reload`.
If the lock's id is taken (`lock.front_door_2`), that's fine: use the id it has.

### 4. The Switcher

**Done when** `ha $HA/api/config/config_entries/entry | grep -c '"domain":"switcher_kis"'` is at least 1, and
`entities switch` lists it.

**You do:** Home Assistant finds Switchers on the home network itself, in the flow:

```bash
FLOW=$(ha -X POST $HA/api/config/config_entries/flow -d '{"handler":"switcher_kis"}'); echo "$FLOW"
ID=$(echo "$FLOW" | python3 -c "import json,sys; print(json.load(sys.stdin)['flow_id'])")
ha -X POST $HA/api/config/config_entries/flow/$ID -d '{}'      # "create_entry", or "no_devices_found"
entities switch sensor | grep -i -E 'switcher|boiler|remaining'
```

If the answer is a `credentials` form, it's a newer Switcher that needs a token. **Ask the person** for the email of
their Switcher account, and to get the token for it from the page the Switcher integration's documentation links to
(https://www.home-assistant.io/integrations/switcher_kis), then send
`{"username": "<the email>", "token": "<the token>"}` to the same flow. If it finds none, **ask the person:** "Is the Switcher on the same Wi-Fi as the Pi, and does the Switcher app see
it?", then run the flow again.

### 5. TARS's [home], for the lock and the boiler

**Done when** `uv run voice-assistant --check` shows Home Assistant, the front door and the boiler with a ✓.

**Ask the person:** what to call each device (default: "front door", "boiler"), and whose voices may unlock the door
by voice: names from the web UI's Voices page (`uv run python -c "import numpy; print(list(numpy.load('voice_data/voiceprints.npz')))"`
lists them). Nobody is a fine answer: then the door unlocks only from the Nuki app.

**You do:** fill in `config.toml`'s `[home]` (it's the last section), with the entities from steps 3 and 4; leave out
`door` and `remaining` if there were none. Then `config_ok`.

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

**Verify:** `uv run voice-assistant --check`, and read the house lines: each device with what it says right now.

### 6. The IR blaster

**Done when** `--check` shows "IR blaster: answers at ...".

**You do:** find it on the home network: `uv run python -m tinytuya scan` (about 20 seconds) lists Tuya devices with
their id, address and version. The local key, which TARS needs too, is only in Tuya's cloud.

**Ask the person** (about ten minutes, once):

1. "At platform.tuya.com, make a free account, then Cloud, Create Cloud Project: any name, industry Smart Home,
   development method Smart Home, data center Central Europe (where a Smart Life account made in Israel is)."
2. "In the project: Devices, Link App Account, Add App Account, and scan the code with the Smart Life app (Me, then
   the scan icon at the top)."
3. *(the person's terminal)* "Run `cd ~/voice-assistant && uv run python -m tinytuya wizard`, and give it the
   project's Access ID and Access Secret (the project's Overview page), and region `eu`. Answer yes to scanning."

The wizard writes `devices.json` in `~/voice-assistant`, with every device's local key (git ignores it). Find the
blaster: the entry whose category is `wnykq` (a universal remote), not the TV, Air and Audio remotes, which live only
in Tuya's cloud (the wizard gives them a `parent`, sometimes empty, and the blaster's key):

```bash
python3 -c "import json; [print(d.get('name'), d['id'], d.get('ip',''), d.get('version',''), d.get('category','')) for d in json.load(open('devices.json')) if 'parent' not in d]"
env_set TUYA_LOCAL_KEY "$(python3 -c "import json; print(next(d['key'] for d in json.load(open('devices.json')) if d['id'] == '<the blaster id>'))")"
```

Then in `config.toml`'s `[home]` (before the first `[[home.devices]]`), with the address from the scan, and
`config_ok`:

```toml
blaster = { id = "<the blaster id>", address = "<its address>", version = "3.3" }
```

**Ask the person:** "In your router, give the blaster a fixed address." **Verify:** `--check`. The key changes if the
blaster is ever removed from Smart Life and added again: then run the wizard again.

### 7. The AC

**Done when** `--check` shows "AC: profile: ..." with a ✓ (not "never tried on the AC").

**You do:** add the AC to `config.toml`, `config_ok`, then `uv run python -m voice_assistant.ac_onboard check`.

```toml
[[home.devices]]
name = "AC"
kind = "ac"
```

**Ask the person:** "Have the Electra remote with you, the AC in view and off, and the blaster where the remote can
reach it from about 30 cm." Then *(the person's terminal)*:
"Run `uv run python -m voice_assistant.ac_onboard capture`." It asks for one button at a time (temperature, fan, mode,
swing, then power twice) and, after each, what the remote's screen shows; Enter takes its guess. Between presses it
asks them to set the remote some way, pointed away from the blaster: those presses aren't recorded. Buttons other than
POWER shouldn't turn the AC on: if one did, tell `fit` what happened. A press that didn't come through is asked for
again; `capture --again "mode 3"` redoes one.

**You do:** `uv run python -m voice_assistant.ac_onboard fit`. It prints what it found and writes
`voice_data/ac/profile.json` only if it can make every press again bit for bit:

```text
- read 19 presses: manchester, 34 bits, sent 3 times
- power: bits 0..0, press=1, none=0
- temperature: bits 11..14, 16 to 30 degrees
- fan: bits 4..5, auto=11, low=00, medium=01, high=10
- swing: bits 8..8, on=1, off=0
- mode: bits 1..3, cool=001, heat=010, ...
- made 19 of 19 presses again, bit for bit
```

A mode that writes some setting its own way (fan mode often blanks the temperature) is left out rather than sent
wrong, and the rest is fitted again without its presses, so nothing in the profile was learned from them. If `fit` writes nothing, read what it says, and the presses in `voice_data/ac/captures.json`: a typo in a screen
reading shows as "the same screen sent different codes", and redoing that step fixes it. A layout it can't explain
is for a person to look at, with those two things.

**Ask the person** *(the person's terminal)*: "Run `uv run python -m voice_assistant.ac_onboard try`, watching the
AC." It sends four codes and asks what the AC did each time: settings without the toggle (it should stay off), the
toggle (on), 26 degrees without the toggle (still on, now 26), the toggle (off). Four yeses mark the profile checked,
and TARS starts out knowing the AC is off.

### 8. The TV and the soundbar

**Done when** `voice_data/ir/tv.json` exists, and the person heard the soundbar's volume change.

**You do:** add both to `config.toml`, `config_ok`, then press one soundbar button:

```toml
[[home.devices]]
name = "TV"
kind = "ir"
codes = "samsung-tv"

[[home.devices]]
name = "soundbar"
kind = "ir"
codes = "polk-re9641"
```

```bash
uv run python -m voice_assistant.ir press soundbar volume_up
```

**Ask the person:** "Did the soundbar get louder?" If not: "What does the back of its remote say? These codes are
for the RE9641." Then *(the person's terminal)*: "With the TV on, run `uv run python -m voice_assistant.ir test TV`:
it sends off, on and the power button, and asks what the TV did each time." It saves what worked, and TARS uses that.

### 9. The Fire TV

**Done when** `--check` shows "Fire TV: answers".

**Ask the person:** "On the Fire TV: Settings, My Fire TV, About. Does it say Fire OS? Click the device name seven
times, go back, and in Developer Options turn ADB debugging on. About, Network shows its IP address: what is it? Then
give it a fixed address in the router. Is the stick powered from its own plug, not the TV's USB port? On the Samsung,
is Anynet+ (HDMI-CEC) on, under Settings, General, External Device Manager?" If it says Vega OS, stop here: it has no
ADB.

**You do:** add it to `config.toml` with that address, and `config_ok`:

```toml
[[home.devices]]
name = "fire tv"
kind = "firetv"
address = "<its address>"
```

Then run `uv run python -m voice_assistant.firetv pair`, and at the same time **ask the person:** "The TV is asking
'Allow USB debugging?': tick Always allow, then OK." `pair` waits a minute for it.

**Verify:** `uv run python -m voice_assistant.firetv look`: the app in front, what's playing, and the text on screen.
Then **ask the person** to open Stremio with the Fire TV remote, run `look` again, and see whether it shows Stremio's
text: if it shows none, the agent will work from screenshots, which is slower. Then try a task, and ask the person
whether the TV did what it says:

```bash
uv run python -m voice_assistant.firetv do "play Toy Story on Stremio"
```

### 10. TARS, with the house

**Done when** `--check` has no ✗ in the house lines, and the person heard TARS work each device.

**You do:** `uv run voice-assistant --check`, then, asking first, `systemctl --user restart voice-assistant`.

**Ask the person** to try each aloud, and say what happened: "Hey TARS, is the front door locked?", "turn the boiler on
for ten minutes", "turn on the AC at 24", "turn the volume up a bit", "play Toy Story on Stremio", and, from a voice in
`unlock_voices`, "unlock the front door" (TARS asks first; a yes unlocks it). `journalctl --user-unit voice-assistant
-n 100 | grep -E 'home:|tv:'` shows each call and what the device answered.

**Then:** tidy up: `rm -f devices.json tinytuya.json snapshot.json tuya-raw.json` (the keys TARS needs are in `.env`).

## Things to know

- **The token is a key to the house.** A Home Assistant token can do anything Home Assistant can; it lives in `.env`,
  which never leaves the Pi. A separate Home Assistant user for TARS, not an administrator, limits it further.
- **The web UI has no accounts**, and TARS can lock the door and run the boiler from what it hears. Unlocking is the
  one thing it won't do without a voice it knows, sure, asking first.
- **IR needs line of sight.** The blaster has to see the AC's receiver; one press missed is one TARS can't know about
  without the light sensor.
- **A device that doesn't answer costs a sentence, not the reply.** Home Assistant down, the blaster off the Wi-Fi: TARS
  says what didn't work, and everything else goes on.
- **ADB is a key to the Fire TV.** Anyone on the home network whose key the TV accepted can work it; TARS's key is
  in `voice_data/firetv/`. The TV's "Allow USB debugging?" is what lets a new one in.
- **Not yet:** scenes ("going to bed"), and checking the AC from a temperature sensor at a vent.
