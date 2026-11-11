# Who's home

TARS knows who's home from whose phone is on the home Wi-Fi, as the routers see it. No app on anyone's phone: the
routers already know which phones are connected, and TARS signs into them and asks, every 30 seconds.

| What | What happens |
|---|---|
| "Who's home?" (aloud or in a chat) | "Alon's home, and Stacey's been out since about two." |
| A reminder due for Stacey while she's out | it goes to her Telegram chat instead of being said to an empty room |
| "Tell Stacey dinner's at eight when she's back" | said soon after her phone comes home, or as soon as she next speaks to TARS, whichever is first |
| Stacey's phone opens the Nuki by Auto Unlock | she's home at once, and a skill waiting for her is told "Stacey just came home, opening the front door" ([the front door](#the-front-door)) |
| `voice-assistant --check` | signs into each router and says whose phone it sees, and connects to the locks' MQTT broker |
| The web UI's Status page | how each router last answered, and who's home |

## Setting it up

1. **Turn off Private Wi-Fi Address for the home networks** on each phone. On an iPhone: Settings > Wi-Fi > (i)
   next to the network > Private Wi-Fi Address: Off, for each home network name (a 2.4 GHz and a 5 GHz network are
   two names). The phone then uses its own hardware address at home, the same on every band and every router. iOS
   shows a "Privacy Warning" under the network afterwards: a private address keeps networks you don't control from
   following the phone, which on your own network there's nothing to hide from. (Fixed works too, but each network
   name then has its own address, and every one of them has to be listed; a forgotten and re-joined network gets a
   new one.)
2. **Read each phone's address**: the same (i) page, Wi-Fi Address. A router also lists the phone under the name it
   gives itself ("Alons-iPhone"), which can be listed as well, in case it drifts onto a network it has a private
   address on.
3. **Say whose phone is whose, and which routers to ask**, in `config.local.toml` (this machine's own settings, never
   committed):

   ```toml
   [presence]
   enabled = true

   [presence.people]
   Alon = ["88:1e:5a:00:00:01", "Alons-iPhone"]
   Stacey = ["28:49:e9:00:00:02", "Staceys-iphone"]

   [[presence.routers]]
   kind = "technicolor"
   address = "192.168.1.1"
   user = "admin"
   secret = "ROUTER_PASSWORD"

   [[presence.routers]]
   kind = "xiaomi"
   address = "192.168.1.160"
   secret = "XIAOMI_PASSWORD"
   ```

   The names are the ones TARS knows people by: their voices on the web UI, and `[telegram] people`.
4. **Put each router's admin password in `.env`**, under the name its `secret` gives: `ROUTER_PASSWORD=...`.
5. **Check it**: `uv run voice-assistant --check` signs into each router and says how many devices are connected and
   whose phone it sees. Then restart TARS; its log says `(presence: asking 2 router(s) every 30 s)`, and
   `(presence: Stacey came home)` as it happens.

It runs in the assistant, and keeps who's home in the event log, so it needs `[learning] log_events` (on by
default). The Telegram service and the web UI read it there.

## The routers

| `kind` | What it is | How TARS asks it |
|---|---|---|
| `technicolor` | A Technicolor (Vantiva) Homeware gateway, as ISPs hand out | Signs in with SRP, as the gateway's own sign-in page does, and reads `/modals/device-modal.lp` |
| `xiaomi` | A Xiaomi router or access point (MiWiFi, like the AX3000T) | Signs in with the hashed password its own page sends, for a token, and reads `/api/misystem/devicelist` |

Ask every router a phone can be connected to. A second router that's a router in its own right (with its own network,
like 192.168.31.x) keeps its phones out of the first one's list and answers only on its own network: switch it to
access-point mode, with the same network names and password as the main one, and phones move between the two as one
network.

Neither router has an official API for this. Each is signed into the way its own web page does it, worked out from
the page's own scripts, byte for byte for the Technicolor's SRP: the
[test vectors](../tests/presence_data/srp_vectors.json) were checked against the gateway's own script, run in a
browser. A Homeware gateway locks sign-in out for a while after wrong passwords, so a refused password isn't tried
again for 15 minutes.

Another kind of router is a class in [`routers.py`](../src/voice_assistant/routers.py) with `_sign_in` and `_read`,
returning the devices connected now.

## The rules

They lean towards "still home", never towards a false "gone":

- **Seen by any router: home, at once.** A phone joins the Wi-Fi about when someone reaches the door.
- **Unseen for `away_after_min` (10 minutes), while every router answered the whole time: out**, since about when
  the phone was last seen. A phone asleep on a table drops off the Wi-Fi for a few minutes now and then; ten minutes
  covers it.
- **A router that doesn't answer says nothing about anyone.** Someone's phone may be on it, so the ten minutes start
  again once every router answers.
- **Before the routers have answered, nobody is known**, and someone already home when TARS starts hasn't "come
  home". A message waits for a real arrival.
- **What the assistant hasn't written for five minutes isn't known now**, so a Telegram chat or the Status page never
  says someone's home from a record that stopped being kept.

So it goes by phones: someone home without theirs shows as out, and someone who just left shows as home for up to ten
minutes. The model is told so, and says it's going by their phone when that matters.

## The front door

A phone joins the Wi-Fi somewhere near the door, and not always first. A Nuki smart lock knows exactly: with its MQTT
API turned on, it publishes every lock action as `action,trigger,auth id,code id,auto unlock` to
`<topic>/lockActionEvent`, saying which app's authorization (the auth id) or keypad code (the code id) did it, and
whether the phone's Auto Unlock did. TARS listens (`doors.py`), keeps each action in the `door_events` table, and:

- **Someone who opens the door from outside is home at once**, before their phone has found the Wi-Fi, and comes
  home "by the front door": "who's home?" says so, and a skill waiting for them is told "Stacey just came home,
  opening the front door". From outside means Auto Unlock or the keypad. Opening it in the app doesn't count, since
  that can be done from anywhere; nor do the lock's own button and knob, which are inside, or TARS and Home Assistant
  opening it over MQTT. Leaving is still by the phone: a door that's opened from inside says nothing about who left.
- **Whose key is whose is learned.** A key nobody's named, that opens the door from outside while exactly one
  person's phone comes home (within ten minutes either way), is a guess that it's theirs; two such guesses, and none
  for anyone else, and it is. The maintainers are told in their chats ("I can tell when Stacey opens the front door
  now..."). Two people coming home together guess nothing. Keys can also be named in `[presence] people`, beside the
  phone: `"key:<auth id>"` for an app, `"code:<code id>"` for a keypad code. The ids are in `door_events`, and in
  Nuki Web's list of authorizations.

Setting it up:

1. In the Nuki app, turn on MQTT for the lock (Settings > Features & Configuration > MQTT) and point it at the
   household's broker (`deploy/home-assistant` runs Mosquitto for Home Assistant). Home Assistant finds it as the
   lock it works.
2. Give TARS its own user on the broker, `tars`, the way [the house](home.md) adds a device's (the `docker run ...
   mosquitto_passwd` command, with just `tars`, then `docker restart mosquitto`), and put its password in `.env` as
   `MQTT_PASSWORD`.
3. Say which door and which broker, in `config.local.toml`:

   ```toml
   [[presence.doors]]
   name = "front door"
   topic = "nuki/4224E764"     # the lock's ID in hex, as the Nuki app and Home Assistant show it

   [presence.mqtt]
   address = "localhost"
   user = "tars"
   secret = "MQTT_PASSWORD"
   ```

4. Install the door extra (`uv sync --extra door`; `deploy/install-pi.sh` installs every extra), and check it:
   `--check` connects to the broker, and TARS's log says `(doors: listening to the front door ...)`, then
   `(doors: the front door unlocked by Auto Unlock, by Stacey)` as it happens.

## Where it's used

**"Who's home?"** is a device in the [home tool](home.md), `who's home`, with only a status: the model asks it, and
nothing about anyone's whereabouts is in the instructions of every turn. A Telegram chat has it too, with the rest
of the house ([home.md](home.md#from-a-chat)).

**Reminders for someone who's out** ([reminders](reminders.md)): when a reminder or message for someone comes due
aloud, and they're out and on [Telegram](telegram.md), it's posted to their chat instead, once, like one set there.
A timer, or one for whoever's there, rings at home as ever, and someone not known to be out hears theirs aloud.

**Messages waiting until someone's back** are said a minute after their phone comes home (the phone joins the Wi-Fi a
little before they're through the door), aloud and waiting for "got it" like any reminder; or as soon as they speak to
TARS, if that's first. Someone TARS knows only by their phone, with no voiceprint yet, can be left one too, by voice,
in a chat or on the web UI, as long as a router can be asked. One left for someone who's home already is said in a
minute: waiting for them to go out and come back would be waiting for nothing. A reminder Telegram can't post (their
chat blocked the bot) is said aloud and stays aloud, rather than going back and forth.

## Design

**One process asks the routers.** The assistant does, and writes who's home to the event database (a `presence`
table, and how each router last answered in `meta`). The Telegram service and the web UI read it there. Two processes
signing in as the same admin would end each other's sessions, and the Status page asking the routers every time it's
opened would be the same again.

**No app, no cloud.** The Home Assistant Companion app on each phone would know more (where someone is when they're
out) and so would Google's or Apple's location sharing; the routers know only home or not, which is what TARS needs,
and they're already there. Nothing leaves the house.

**Detected, not how.** The rules (`Tracker`) take the set of people detected in a round, not router answers, and a
door's lock feeds them too (`Watcher.at_door`, from the MQTT client's thread, under the same lock as the rounds). Minutes are counted on a monotonic clock: a Pi has no clock of its own
while it's off, and its time of day jumps when it sets itself after a boot. After a restart, the rules start from what
was last written, if it's fresh, so someone out when TARS restarted who comes home soon after has come home.

**A router that can't be read is a router that didn't answer.** Whatever goes wrong (no answer, a refused password, a
page this doesn't understand, even a bug in one router's code) is that router not answering for the round: nobody is
marked out for it, and the watching goes on. A device page with no devices on it at all is one of those, never
"nobody is home": a real one always lists some.

## Not yet

- **A door sensor** (a Shelly BLU Door/Window, read by Home Assistant over Bluetooth) to tell a phone that went to
  sleep from someone who left: the door opening, then a phone dropping off, is someone leaving now, not ten minutes
  later.
- **Leaving by the door.** The lock's actions from inside, then a phone dropping off, could say someone left now;
  for now it's the phone's ten minutes.
- **Phones on a second access point show on the main router as wired devices**, counted present while it gives them
  an IP address. It's been seen to clear that for a device that's gone, but not yet watched as someone leaves.
