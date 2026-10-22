# Who's home

TARS knows who's home from whose phone is on the home Wi-Fi, as the routers see it. No app on anyone's phone: the
routers already know which phones are connected, and TARS signs into them and asks, every 30 seconds.

| What | What happens |
|---|---|
| "Who's home?" (aloud or in a chat) | "Alon's home, and Stacey's been out since about two." |
| A reminder due for Stacey while she's out | it goes to her Telegram chat instead of being said to an empty room |
| "Tell Stacey dinner's at eight when she's back" | said soon after her phone comes home, or as soon as she next speaks to TARS, whichever is first |
| `voice-assistant --check` | signs into each router and says whose phone it sees |
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
the page's own scripts, byte for byte for the Technicolor's SRP ([`tests/presence_data/srp_vectors.json`](../tests/presence_data/srp_vectors.json)
are checked against the gateway's script, run in a browser). A Homeware gateway locks sign-in out for a while after
wrong passwords, so a refused password isn't tried again for 15 minutes.

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

## Where it's used

**"Who's home?"** is a device in the [home tool](home.md), `who's home`, with only a status: the model asks it, and
nothing about anyone's whereabouts is in the instructions of every turn. In a Telegram chat it's the one device
there is: a chat works nothing in the house.

**Reminders for someone who's out** ([reminders](reminders.md)): when a reminder or message for someone comes due
aloud, and they're out and on [Telegram](telegram.md), it's posted to their chat instead, once, like one set there.
A timer, or one for whoever's there, rings at home as ever, and someone not known to be out hears theirs aloud.

**Messages waiting until someone's back** are said a minute after their phone comes home (the phone joins the Wi-Fi a
little before they're through the door), aloud and waiting for "got it" like any reminder; or as soon as they speak to
TARS, if that's first. Someone TARS knows only by their phone, with no voiceprint yet, can be left one too.

## Design

**One process asks the routers.** The assistant does, and writes who's home to the event database (a `presence`
table, and how each router last answered in `meta`). The Telegram service and the web UI read it there. Two processes
signing in as the same admin would end each other's sessions, and the Status page asking the routers every time it's
opened would be the same again.

**No app, no cloud.** The Home Assistant Companion app on each phone would know more (where someone is when they're
out) and so would Google's or Apple's location sharing; the routers know only home or not, which is what TARS needs,
and they're already there. Nothing leaves the house.

**Next**: a door sensor (a Shelly BLU Door/Window, read by Home Assistant over Bluetooth) to tell a phone that went
to sleep from someone who left: the door opening, then a phone dropping off, is someone leaving now, not ten minutes
later. And the Nuki lock's own events, which say who unlocked it, so TARS can greet them by name as they come in.
