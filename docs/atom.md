# The Atom: a small body for a room

The [M5Stack Atom VoiceS3R](https://docs.m5stack.com/en/core/Atom%20VoiceS3R) is a microphone, a speaker and a button
on an ESP32-S3, the size of a matchbox. With TARS's firmware it's a [network body](bodies.md): it hears "hey TARS"
itself, sends what was said to the brain on the Pi, and plays the reply. The conversation, the double-check of the
name, and everything TARS knows stay on the brain.

**Where it stands:** working with a test brain on the Pi, tried with recordings played at it: woken, double-checked
on the brain, answered in 0.83 s to the first sound, a follow-up, a two-minute reply with nothing dropped, the brain
going away and coming back. Not yet measured: how often it wakes by mistake in a real room, and how far away it
hears.

## What it does

- **Hears the name on the device.** The same "hey TARS" model as the Pi ([wake word](wake-word.md)), in its 8-bit
  build, woken by one score of 0.47 or more, as on the Pi.
- **Leaves the double-check to the brain.** The chip is too small for it, so the Atom tells the brain `wake: model`
  and sends the 3.6 s before the wake with it. The brain runs the same double-check as for the Pi's own microphone,
  and sends the Atom back to sleep if it wasn't TARS that was called ([what that means for privacy](security.md#a-network-body-or-something-posing-as-one)).
- **Streams, then plays.** After a wake it streams the microphone in 80 ms blocks until the brain says the
  conversation's over (or 120 s, its own cap), and plays the reply from a 64 s buffer. A longer reply slows the
  brain's sending rather than losing any.
- **Cancels its own echo**, so its speaker doesn't wake it.
- **The button:** press to talk without saying the name; press while TARS is speaking to stop it.
- **Can't reach the brain:** called meanwhile, it says the same line as other bodies ("I can't reach my brain right
  now..."), and makes the connection again by itself.

It has no light, so it shows nothing while it streams: for a body without a screen or a light, TARS's own "Yes?" is
the sign it's listening ([the rules a body keeps](bodies.md#rules-a-body-keeps-whatever-the-brain-says)).

## Setting one up

You need [ESPHome](https://esphome.io) (`uv tool install esphome`), a USB-C cable, and a brain with `[bodies]` on
([running one](bodies.md#running-one)).

1. On the brain, pair it: `uv run voice-assistant --body-add Bedroom` prints its key, once.
2. Copy `firmware/atom-voices3r/secrets.example.yaml` to `secrets.yaml` (never committed) and fill it in: the Wi-Fi
   (2.4 GHz only), the key, the brain's address (`ws://homepi.local:8766`), and a long random password for updates.
3. Plug it in. The first time, hold its side button for about 2 s until it's in download mode (on a Mac, allow the
   accessory), then:

   ```bash
   esphome run firmware/atom-voices3r/tars-atom.yaml
   ```

4. From then on it updates over Wi-Fi: `esphome upload firmware/atom-voices3r/tars-atom.yaml --device tars-atom.local`.

`--body-list` on the brain says when it last connected. To go back to M5Stack's own firmware, flash a backup or
their configuration; nothing on the Atom is changed for good.

## Inside

[`firmware/atom-voices3r/tars-atom.yaml`](../firmware/atom-voices3r/tars-atom.yaml) is the whole device: the board's
pins and audio settings (taken from M5Stack's configuration), the echo canceller, the wake word and the link. None
of Home Assistant's voice satellite is in it.

[`firmware/components/tars_body`](../firmware/components/tars_body) is the body's half of [the protocol](bodies.md#the-protocol-1)
for ESPHome, held to the same rules as the Python body's `BodyProtocol`. The connection's own task only takes in
what the brain sends: texts queued in order, a reply's audio straight into the buffer. Everything else, sending
included, happens in ESPHome's main loop. The device's key signs the brain's challenge with
[Monocypher](https://monocypher.org)'s Ed25519 (ESPHome's own libsodium can only check signatures).
[`firmware/components/micro_wake_word`](../firmware/components/micro_wake_word) is ESPHome's, keeping the last
detection's score for the wake message.

Its log goes to the board's serial pins, not USB: over USB, a log line waits while a computer is plugged in but
nobody reads it, which stalls the link. To read it over USB while debugging, build with
`esphome -s log_port USB_SERIAL_JTAG run firmware/atom-voices3r/tars-atom.yaml` and keep the log open (opening the
USB port restarts the Atom).

## Measured

| | |
|---|---|
| Recorded "hey TARS" takes played at it | 4 of 4 woke it; the scores matched the Pi's model on the same takes |
| Owner, live, at the trial's lenient setting (0.15, averaged) | 5 of 5, and a false wake in normal talk, before the brain's double-check |
| A turn, by recordings | 0.83 s from the end of the question to the first sound |
| A two-minute reply | played whole; the brain's sending slowed to the Atom's pace |
| The brain stopped and started | the "can't reach" line, then connected again within seconds |
