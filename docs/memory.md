# What TARS knows about each person

TARS gets to know the people it lives with: what they tell it, and what it picks up by itself. Their words for
things, how they like to be answered, the people around them, what's coming up. Then it answers like someone who
knows them, without saying so: "Ari's room" is the nursery, mornings get short answers, and the day after the
interview it asks how it went.

## What it keeps

Each memory is a short note in plain words, whose it is (a person, or the household), what it's about, and how it's
known. Five kinds:

| Kind | What | Like |
|---|---|---|
| style | how they talk, and want to be answered: length, tone, humor, directness; their words for things; when to just do it and when to ask first | "Short answers in the morning"; "'warm' means 24°"; "just does the AC, asks first for the TV" |
| like | what they like or don't, in anything | "No esports in the gaming news" |
| fact | about them or their life | "Works from home on Fridays"; "the car is a 2024 Ioniq 5" |
| person | someone around them, and who they are | "Yossi: the electrician" |
| thread | something coming up, with when | "Job interview at Wix, 2026-11-20 10:00" |

Each says whether they said it or TARS worked it out (a guess, and told to the model as one), where it came from (a
turn of a conversation, by voice, in a chat or on the Talk page, or the look back), and when. Dates are written out,
never relative: "born in March 2026", not "eight months old", so nothing goes stale. A correction keeps the old one
as history ("the car was a 2019 Corolla until 2026-12-19"), so what was true when stays known; forgetting deletes it,
and its history, for good. A thread is asked about once after it happens, and stops mattering a week later unless it
says otherwise.

## How it learns

- **When asked.** "Remember that the wifi is TARSNET", "forget that", "no, Yossi's the plumber": the memory tool, in
  the reply, and a few words back ("Got it."). Only someone TARS knows can change what it knows: their chat, their
  signed-in Talk page, or a voice speaker ID recognizes.
- **By itself, after the conversation.** Once a conversation has been quiet for `[memory] look_back_minutes` (10),
  TARS looks back on it ([`reflect.py`](../src/voice_assistant/reflect.py)): one request to `[llm] model` reads it
  and what's kept about the people in it, and says what to remember, correct, retire (no longer so) or mark asked.
  So a reply never waits on learning. A chat that goes on later is looked back on again, for what's new. Code checks
  each change before it's made:
  - Only what was said to TARS: overheard turns (TARS stayed quiet) are never read, and turns by a voice it couldn't
    tell are only context, never kept from.
  - A change is for someone who spoke in it, or the household. It's "said" only when that person's own turn says it;
    anything else is a guess.
  - Never kept: one-off requests, small talk, codes and passwords, and anything about health, money, relationships,
    religion or politics that the person didn't plainly say about themselves.
  - At most 8 changes a conversation; most need none. Each one is logged.

  It costs about a fifth of a cent a conversation with Luna. Off with `[memory] look_back = false`; without an
  OpenAI key it doesn't run. One process does it: the voice's, or in a chat-only home, Telegram's.

- **Once a night, each person's whole picture.** After 4:00, for everyone who said something to TARS in the last 30
  days: what's kept about them and what they said (with TARS's replies, shortened), in one request, for what no single
  conversation shows: the same thing asked the same way again and again ("the news" is always Channel 12), what they
  keep correcting or coming back to, guesses since contradicted, two memories that say the same thing (merged). The
  same checks, at most 12 changes. About a cent a person a night.

A memory is dated when it was said, not when TARS looked back on it.

## How it's used

Every request is told what's kept about whoever is asking, and the household's ([`memory.py`](../src/voice_assistant/memory.py)
`note()`): how they like answers first, then likes, facts, people and what's coming up, each numbered, guesses
marked. Up to 60 of a person's and 40 of the household's; past that, the model looks the rest up with the tool's
recall, history included. The model is told to:

- use it unprompted whenever it makes the answer better, and never say it knows, how, or that it's adapting ("as you
  told me", "I noticed", "I'll keep that in mind"); asked how it knows, it says plainly;
- change how it says things, never what's true: what it knows of someone's views never makes it agree with them;
- ask about a thread that's due once, when the conversation allows;
- answer "what do you know about me?" honestly, guesses as guesses.

**Who hears what.** A person's own only when TARS knows who's asking; otherwise only the household's. The household
hears each other's: what Stacey told TARS may come up when Alon asks, aloud. Something someone asks to keep private
(a surprise) is only ever in their own chat.

## Where it's kept

The `memories` table of the event database, backed up with it, and `looked_back` (how far each conversation has been
looked back on). The Skills page shows each person's likes, and forgets one; a page for all of it, with where each
came from, is next.

## Not yet

- **A page for all of it**: every memory, why TARS thinks so (a link to the turn), edit, delete, "never learn this".
- **Voices recognized in old conversations**: turns from before someone's voiceprint aren't theirs yet.
- **Measuring it**: scripted conversations over days (told, used later, a second voice, forgotten, a style signal,
  a thread) checked for what's kept, what's used, and that nothing crosses between people.
