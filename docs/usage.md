# What TARS uses, and what it costs

Every call TARS makes to a paid service is counted as it ends: the model's tokens (in, out, reasoning, cached) and
web searches, the seconds of speech sent to be transcribed, the characters sent to be spoken. Each call is a row in
the `usage` table of the event database, with the time, the service and model, and whose turn it was part of: the
person (when TARS knows them) and the channel they asked by.

```
uv run voice-assistant --usage                  # this month
uv run voice-assistant --usage --month 2026-10  # an earlier one
```

prints the month by service and model, then by person, with what it cost.

## Whose it is

A turn is counted for whoever asked, by how they asked:

| Channel | Whose |
|---|---|
| `voice` | the speaker, once their voice is known; nobody's if it never is. A draft started before they finished, and the sentences spoken after, count for them too |
| `telegram` | the chat's person |
| `errand` | whoever asked for the errand, for all of the browser's steps |
| `skill` | a person's skill is theirs; a household skill is nobody's |
| `background` | anything outside a turn: the made-ahead phrases, the feeds, the self-learning |

## The cost

The cost is worked out when it's read, from [`src/voice_assistant/prices.toml`](../src/voice_assistant/prices.toml)
(the same prices the [evals](evals.md) use), so a corrected price corrects the past too. It's an estimate, not a
bill: the providers' dashboards are the truth, and the prices are only as fresh as the date the file says they were
checked. Two things are said rather than hidden:

- **No price:** a model or voice that isn't in `prices.toml` is counted but not priced, and the report says how many
  calls that was. Add its price to fix it.
- **Not reported:** a stream cut off before its end (you interrupted TARS, the connection dropped) never got the
  provider's count. It's kept as a call that used nothing, and the report says how many there were.

Not every provider says everything. Where Cerebras or Groq don't say how much of the input was cached, it's
priced as fresh. The seconds of streamed speech (Deepgram's Nova and Flux) are worked out from the audio TARS sent,
not read from Deepgram. Spoken characters are what TARS sent to be said.

Speech is priced by the minute of audio sent, which is what Deepgram and OpenAI bill. Voices are priced per million
characters, by the start of the voice's name (`aura-2`, `flux-`, `sonic`).

## A monthly cap

`[usage] monthly_cap_usd` in `config.toml` caps a month's estimated cost (0, the default, is no cap). The maintainers
(`[household]`) are told in their Telegram chats once when the month reaches 80% of it, and once when it reaches the
cap. Past it, until the month ends:

- **Said aloud,** TARS answers "I've used up this month's budget for the services I think with, so that has to wait
  for next month." The recording isn't sent anywhere. The line was made ahead while there was budget, so saying it
  costs nothing. A house command it can hear on the Pi itself (`[home] commands_on_pi`) is still done.
- **In a chat,** the same line is the answer. The commands done by code (`/home`, `/volume`, `/mute`) still work;
  `/say` doesn't, since its voice is paid for.
- **Skills** don't run: a one-off is tried again or given up, as when its service fails.
- **Reminders already set are still said**, from lines made ahead.

The cap is checked as a turn starts, never part-way through a reply, so a month can end a little over it. What's been
spent is added up again at most once a minute. `voice-assistant --check` has a line for it: a warning from 80%.

## Kept

Rows are kept for 400 days, so this month can be set against the same month last year; older ones are deleted when
the metering starts. A row holds no words of what was said, only the amounts, the model and the person's name.
Writing a row can fail without costing a reply: it's done on a thread of its own, and a row that couldn't be kept is
said in the log.
