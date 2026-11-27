# Calling TARS

TARS can have a phone number. A call is one conversation, awake until someone hangs up, with the same brain, memory and
voice as at home. It can be talked over, and remembers only what was heard of what it was saying.

## How it works

```text
phone → Twilio → tunnel → phone.py → (body link) → calls.py in the voice process
```

- [`phone.py`](../src/voice_assistant/phone.py) is a small web server on the brain's machine: it checks who's calling
  and carries the audio. It has no models and makes no decisions about the conversation.
- [`calls.py`](../src/voice_assistant/calls.py) holds the conversation. It needs `[stt] provider = "flux"`: Flux hears
  the whole call on one stream, and says when the caller starts, may be done, carries on and is done.

**Taking turns.** A reply is started when the caller may be done, and played once they are. What it would do in the
house waits until then.

**Being talked over.** Flux reports a turn once it has heard words, so a cough doesn't count. Neither do "mm-hm" and
"okay" while TARS talks (unless it just asked something), nor its own words coming back from a car's speakers. Anything
else pauses it at once. Two words, or one Flux is sure of, and the rest is dropped; if nothing comes of it (a word from
the radio), TARS goes on from the word it was on. The phone says how far it has played every 0.4 s and the voice says
when each word ends, so the conversation keeps the words that were heard. What the reply set or sent is kept if any of
it was heard.

**Something slow.** "One moment" after 2.5 s without a sound. Talk over a search and it goes on by itself, unless you
call it off ("never mind", "wait", "no"): a second brain with the same conversation takes what you say, and what the
first found is said at the next quiet moment, or sent to the TARS page (and the chat) if you've hung up.

**The voice** goes on from one sentence into the next in one breath with Cartesia; with another voice, or if that
fails, a sentence at a time.

**How it went** is in the voice process's log after each call: the typical and slow-end time to the first sound, and
how many answers were interrupted, resumed, ignored, started early, or finished later.

**Ending.** "Bye" or "hang up", two quiet minutes, or half an hour. The star key stops TARS talking.

## Who may call

1. Twilio's signature on every request, and only numbers in `TARS_PHONE_ALLOWED`. A caller's number can be faked, so:
2. a PIN of at least 6 digits. Five wrong ones refuse every call for an hour.
3. The audio needs a token from the right PIN: good once, for 30 s, for that call.

A call enters as [the Talk page](bodies.md#the-web-page) does, by a one-time ticket. It's never at home: nothing said on
one unlocks a door, and reminders aren't said on it. Calls aren't
recorded: their words are kept in the conversations, not their audio. The audio passes through Twilio and Deepgram.

### What waits for a tap

A call's caller is known by a PIN, so it does less than a voice at home. What a call may do straight away is listed
([`approvals.py`](../src/voice_assistant/approvals.py)): work the house's devices, feeds, look things up, send
things to the TARS page, the caller's own timers and reminders, remember something, read about TARS. Anything else
(TARS's settings and wishes, automations, changes through Home Assistant, forgetting something, a message or reminder
for someone else, any new tool) is posted to the caller's Telegram chat with Approve and Cancel, shown whole, and done
when they tap, for 10 minutes, on the call or after it. With no chat, it's refused. The browser's own approval before
an order is the same as ever.

## Setting it up

1. A Twilio number with voice, and a tunnel from a public `https://` name to `http://127.0.0.1:8790` on the brain's
   machine, with no sign-in page in front of it.
2. `[bodies] enabled = true`, and in `.env`: `TWILIO_AUTH_TOKEN`, `TARS_PHONE_URL`, `TARS_PHONE_ALLOWED` (`+` and the
   country code), `TARS_PHONE_PIN`, and `TARS_PHONE_PERSON` (their sign-in name).
3. The number's "a call comes in" webhook: `<TARS_PHONE_URL>/voice`, by POST.
4. `deploy/install-pi.sh` sets up `voice-assistant-phone`, which starts at boot. Check: `curl -X POST <TARS_PHONE_URL>/voice` is rejected.
