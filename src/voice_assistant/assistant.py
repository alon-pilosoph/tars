"""The conversation loop: wait for the wake word, listen, answer out loud, and keep listening for follow-ups.

Each request is transcribed while it's spoken, an answer is drafted at the first pause, and the reply is spoken
sentence by sentence as the brain writes it. Short lines ("Yes?", "Did you call me?", the error lines) are made ahead
and kept on disk, so they play at once, even offline.
"""

import random
import re
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Collection, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import numpy as np

from . import usage
from .audio import AudioDeviceError, chime
from .body import NO_WAKE, Body, BodyGone, Wake
from .commands import ON_PI, Command, HouseCommands, sentences
from .config import GREETS, LLMConfig, RemindersConfig, SpeakerConfig
from .control import Op, Refused
from .conversations import LLM, STT, SentItem
from .draft import Draft
from .echo import without_echo
from .errors import SERVICE_FAILURES
from .home import ONE_MOMENT, Request
from .journal import Journal
from .llm import (
    ASKED_TAG,
    FILLERS,
    FOLLOW_UP_TAG,
    PARDON,
    REMINDER_TAG,
    SKIP,
    THINKING,
    Brain,
    Reply,
)
from .phrases import Audio, Phrases
from .recording import NO_SPEECH, SILENCE, TAP
from .reminders import VOICE, Change, Clock, Reminders, ReminderTools, held_line, late, says_line
from .reminders import line as reminder_line
from .skills import ALOUD, Answers, happened, on_its_own
from .skills import Clock as SkillClock
from .speaker import SpeakerID
from .speech import StreamedReply, failed_at, mark_failed_at
from .stt import BufferedSession, Session, Transcriber
from .takes import MUTED, Take, TakeDesk, level
from .tts import Voice
from .verify import ASK
from .wake import DUE

CAPPED_LINE = usage.CAPPED_LINE
ASK_PHRASE = "Did you call me?"
ASK_TIMEOUT_S = 5.0  # how long to wait for an answer to it
# Made ahead and kept on disk like the greetings, so they play even when the voice service itself failed.
ERROR_LINES = {
    "dry": [
        "That didn't work. Not my finest moment. Try again.",
        "I lost that one somewhere between here and the server. Ask me again.",
        "Something broke. I'd blame the cloud, but I'm a little bit the cloud. Try again.",
    ],
    "plain": ["Something went wrong. Please try again.", "That didn't work. Please ask again."],
}
DRY_FROM_HUMOR = 50  # percent
PAUSE, ALWAYS, NEVER = GREETS  # when to say "Yes, <name>?" after the wake word
# Muted, and unmuted only to hear the answer to what TARS asked: the longest that answer is recorded. "No, I'm
# good" and "channel twelve" take two seconds; what's said after is the room's, not TARS's.
ANSWER_ONLY_S = 6.0
MUTED_POLL_S = 0.25  # muted, how often what's due is looked for (unmuting ends the wait at once)
ERROR_LINE_WAIT_S = 1.0
AFTER_NAME_WAIT_S = 1.0  # the longest the first request waits to know what followed the name (about 0.25 s)
PHRASE_WAIT_S = 3.0  # the longest a short line keeps a conversation waiting while it's made ("Yes, Alon?")
UNPROMPTED_WAIT_S = 10.0  # the same for what TARS says on its own, which may wait behind lines being made ahead
VOICE_RETRY_S = 60.0  # a reminder or announcement the voice couldn't make is tried again after this
ANNOUNCEMENT_KEEP_S = 600.0  # how the TV went is old news after this, and isn't said any more
MOST_SAID = 1000  # characters a chat's /say has said aloud: a minute or so
REMINDERS_REST_S = 30.0  # after the reminders table fails, nothing is said for this long
HELD_POLL_S = 0.1  # held by a recording session, how often what's due is looked for between takes
TAKE_PAD_BLOCKS = 6  # room kept before and after a take's speech (6 blocks of 80 ms)
TAKE_START_S = 6.0  # after a take's beep, how long it waits for someone to speak


def greeting(name: str | None) -> str:
    return f"Yes, {name.capitalize()}?" if name else "Yes?"


class Identified(NamedTuple):
    name: str | None
    score: float | None
    embedding: np.ndarray | None


NOBODY = Identified(None, None, None)


def asks(lines: list[str]) -> bool:
    """Whether what TARS said on its own asks something, so it waits for an answer: even muted, it's heard."""
    return any("?" in line for line in lines)


# fmt: off
ONES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
        "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen")
TENS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
# Not choices, though they stand next to "or": "the kitchen or the bedroom?", "is that all, or is there more?"
NOT_CHOICES = {"a", "an", "the", "is", "are", "was", "do", "does", "did", "you", "i", "it", "that", "this", "there",
               "my", "your", "me", "not", "something", "else"}
# fmt: on
MOST_EXPECTED = 12


def _spoken(number: int) -> str:
    if number < 20:
        return ONES[number]
    tens, ones = divmod(number, 10)
    return TENS[tens - 2] + (f"-{ONES[ones]}" if ones else "")


def offered(said: str) -> tuple[str, ...]:
    """The choices TARS's last question offered, for speech to text to lean towards: an answer to it comes quickly,
    and its first sound is easily lost ("twelve" heard as "well"). "Which channel, 11 or 12?" -> 11, eleven, 12,
    twelve. Empty unless it ended on an either-or."""
    question = re.search(r"[^.?!]*\?\s*$", said)
    if not question or not re.search(r"\bor\b", question[0], re.IGNORECASE):
        return ()
    words = re.findall(r"[\w'-]+", question[0])
    found: list[str] = []
    for i, word in enumerate(words):
        if word.lower() != "or":
            continue
        before = [w for w in words[:i] if w.lower() not in NOT_CHOICES][-1:]
        after = [w for w in words[i + 1 :] if w.lower() not in NOT_CHOICES][:1]
        found += before + after
    found += [w for w in words if w.isdigit()]  # "11, 12 or 13": the numbers before the last "or" too
    terms = []
    for word in found:
        terms.append(word)
        if word.isdigit() and int(word) < 100:
            terms.append(_spoken(int(word)))
    return tuple(dict.fromkeys(terms))[:MOST_EXPECTED]


class Announcements:
    """What TARS says on its own once it's free, from another thread: how a task on the TV went. Kept in order; said
    after a chime, between conversations, like a reminder. A line not said within ANNOUNCEMENT_KEEP_S of being added
    is dropped. `clock`: seconds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._lines: deque[tuple[float, str]] = deque()  # (when it was added, the line)
        self._taken: dict[str, float] = {}  # the lines last taken, by line: when each was added
        self._lock = threading.Lock()
        self._clock = clock
        self._not_before = 0.0

    def add(self, line: str) -> None:
        if line.strip():
            with self._lock:
                self._lines.append((self._clock(), line.strip()))
                self._not_before = 0.0

    def waiting(self) -> bool:
        return bool(self._lines) and self._clock() >= self._not_before

    def take(self) -> list[str]:
        with self._lock:
            taken, self._lines = list(self._lines), deque()
            self._taken = {line: at for at, line in taken}
        fresh = [line for at, line in taken if self._clock() - at < ANNOUNCEMENT_KEEP_S]
        if len(fresh) < len(taken):
            print(f"(dropped {len(taken) - len(fresh)} announcement(s) too old to say)")
        return fresh

    def put_back(self, lines: list[str], retry_in_s: float) -> None:
        """Lines taken but not said (the voice is down): first in line again, in their order, tried again in
        `retry_in_s`, or as soon as a new line is added, which may find the voice back."""
        with self._lock:
            now = self._clock()
            self._lines.extendleft(reversed([(self._taken.get(line, now), line) for line in lines]))
            self._not_before = now + retry_in_s


@dataclass
class Said:
    text: str
    sent: list[SentItem]
    answered_by: str | None = None
    quick_service: str | None = None


@dataclass
class Utterance:
    pcm: bytes
    session: Session  # transcribing it, fed while it was recorded
    draft: Draft[Answer]
    silence_s: float  # how long ago they stopped talking when the recording ended
    follow_up: bool = False
    echo_of: str | None = None  # what TARS was saying, with the mic open, while this was recorded


@dataclass
class Answer:
    """A request, transcribed, and the reply being made to it, not yet heard by anyone."""

    text: str
    who: Identified
    stt_s: float
    t_asked: float = 0.0
    written: Reply | None = None  # the brain's reply, as it's written; None: the brain wasn't asked
    reply: StreamedReply | None = None  # its audio; None: nothing to say (no words, or not meant for TARS)
    spoken: list[str] = field(default_factory=list)
    error: Exception | None = None  # the brain failed to start the reply; raised once what was heard is written down
    sentences: list[tuple[float, str]] = field(default_factory=list)  # (when it was written, the sentence)
    on_pi: bool = False  # a house command, done without the model (commands.py)


class _MadeAhead:
    """The voice, but a sentence made ahead (a house command's reply) plays from what was kept: at once, and with
    the network down."""

    def __init__(self, voice: Voice, phrases: Phrases):
        self._voice, self._phrases = voice, phrases
        self.sample_rate = voice.sample_rate

    def stream(self, text: str) -> Iterator[bytes]:
        """The thing is done by now: a line that can't be said is logged, never an error that ends the turn."""
        if text.strip() in self._phrases:
            try:
                audio = self._phrases.get(text.strip()).result(timeout=PHRASE_WAIT_S)
            except Exception as e:  # noqa: BLE001 - said live instead
                print(f"(the made-ahead line {text.strip()!r} isn't there ({e!r}); saying it live)")
            else:
                yield from audio
                return
        try:
            yield from self._voice.stream(text)
        except Exception as e:  # noqa: BLE001 - see above
            print(f"(couldn't say {text.strip()!r} after a house command: {e!r})")

    def warm(self) -> None:
        self._voice.warm()


class Assistant:
    """The conversation, with a body (body.py) to hear and speak with. Typed questions (answer_text) need only a body
    that plays, the brain and the voice: the transcriber may be None."""

    def __init__(
        self,
        body: Body,
        transcriber: Transcriber | None,
        brain: Brain,
        voice: Voice,
        speaker_id: SpeakerID | None = None,
        name_threshold: float = SpeakerConfig.wake_threshold,
        journal: Journal | None = None,
        humor: int = LLMConfig.humor,
        phrase_folder: Path | None = None,
        reminders: ReminderTools | None = None,
        ack_window_s: float = RemindersConfig.ack_window_s,
        announcements: Announcements | None = None,
        clock: Callable[[], float] = time.monotonic,
        elsewhere: Callable[[dict], bool] | None = None,
        skill_clock: SkillClock | None = None,
        commands: HouseCommands | None = None,
    ):
        """`phrase_folder`: where the short lines' audio is kept for this voice, so they survive an offline restart.
        `clock`: seconds, for when a line that failed to make is tried again. `elsewhere(reminder)`: True if a
        reminder due aloud went somewhere else instead (reminders.ChatWhenOut: its person is out). `skill_clock`: the
        skills due to run by themselves (skills.py)."""
        self.body = body
        self.transcriber = transcriber
        self.brain = brain
        self.voice = voice
        self.speaker_id = speaker_id
        self.name_threshold = name_threshold
        self.journal = journal or Journal()
        self.error_lines = ERROR_LINES["dry" if humor >= DRY_FROM_HUMOR else "plain"]
        self.timings: dict[str, float] = {}  # the last answer's latency by stage, in seconds
        self._woke_at: float | None = None  # when the wake word was confirmed, for how soon TARS greeted it
        self._greet_s: float | None = None
        self._only_echo = False  # the last request heard was nothing but a trace of TARS's own greeting
        self._last_reply = ""  # what TARS last answered in this conversation
        self._identifying = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speaker-id")
        self._thinking = threading.Lock()  # drafts take turns
        self.phrases = Phrases(voice, phrase_folder, clock)
        self.ack_window_s = ack_window_s
        self.announcements = announcements
        self._elsewhere = elsewhere
        self._skill_clock = skill_clock
        self.commands = commands
        self.reminder_tools: ReminderTools | None = None
        self._reminder_clock: Clock | None = None
        self.use_reminders(reminders)
        self._just_said: list[int] = []  # reminders just said, waiting for an acknowledgement
        self._last_who: str | None = None
        # The buttons (buttons.py), from their own thread: talk wakes it, stop ends what it's doing, and while muted
        # (the speakerphone's own mute, or TARS's when it has none) it doesn't wake.
        self._talk = threading.Event()
        self._stop = threading.Event()
        self.muted = False
        self._unmuted = threading.Event()  # set while not muted: what a muted wait waits for
        self._unmuted.set()
        # A recording session (recording.py) holds TARS and asks for takes from the control thread (control.py).
        self.takes = TakeDesk()

    def use_reminders(self, tools: ReminderTools | None) -> None:
        self.reminder_tools = tools
        self._reminder_clock = Clock(tools.reminders, on_read=self._prepare_reminders) if tools else None

    @property
    def reminders(self) -> Reminders | None:
        return self.reminder_tools.reminders if self.reminder_tools else None

    def talk(self) -> str:
        """The talk button: TARS listens, as if called by name. Not while muted, or while it's busy already."""
        if self.muted:
            return "ignored: muted (unmute first)"
        if self.takes.held():
            return "ignored: a recording session is open"
        self._talk.set()
        return "listening"

    def stop(self) -> str:
        """The stop button: what TARS is saying stops now, the reply being written with it; it doesn't wait for a
        follow-up; and a reminder it just said counts as acknowledged."""
        self._stop.set()
        self.body.stop()
        self.brain.interrupt()
        return "stopped"

    def _cut_short(self, cut: bool) -> None:
        """A play the body cut short itself, not for the stop button (someone said "stop" over TARS): taken as the
        stop button."""
        if cut and not self._stop.is_set():
            self.stop()

    def mute(self, muted: bool) -> None:
        """While muted, TARS doesn't wake: not to its name, nor to the talk button. It doesn't even listen for its
        name: the wake model is idle and the mic's audio thrown away (_wait_muted). What's due is still said."""
        self.muted = muted
        (self._unmuted.clear if muted else self._unmuted.set)()
        print("(muted: not listening)" if muted else "(unmuted: listening again)")

    def _woken(self) -> bool:
        """Asked while waiting for the wake word: the talk button, something due, or muted (stop listening)."""
        return self._talk.is_set() or self.muted or self.takes.held() or self._due()

    def _wait_muted(self) -> str | None:
        """While muted, instead of the wake word: no wake model and no double-check, only the mic's audio thrown
        away (its stream stays open, so unmuting is instant), and what's due looked for. DUE when something is;
        None once unmuted. A mic that's gone raises, as it does while listening."""
        while self.muted:
            self.body.discard()
            if self._due():
                return DUE
            self._unmuted.wait(MUTED_POLL_S)
        self.body.clear()  # what it heard while muted is old
        return None

    def control_ops(self, mute: Callable[[bool], None] | None = None) -> dict[str, Op]:
        """What TARS's other processes may ask for over control.py: the web UI, for a recording session, to hold TARS,
        let it go, and the takes; a chat, to have something said aloud, word for word, as an announcement is; and with
        `mute` (how the buttons mute it), to mute or unmute it."""

        def holder(r: dict) -> str:  # the session asking: each holds TARS on its own
            return str(r.get("session", ""))

        def record(r: dict) -> dict:
            if self.muted:
                raise Refused(MUTED)
            return self.takes.state(self.takes.ask(holder(r)))

        def hold(r: dict) -> dict:
            self.takes.hold(holder(r))
            return {"ok": True, "muted": self.muted}

        def release(r: dict) -> dict:
            self.takes.release(holder(r))
            return {"ok": True}

        def say(r: dict) -> dict:  # {"text": ...}: after a chime, between conversations, even while muted
            text = r.get("text")
            if not (isinstance(text, str) and text.strip()) or self.announcements is None:
                raise Refused("unknown")
            self.announcements.add(text[:MOST_SAID])
            return {"ok": True}

        def muted(r: dict) -> dict:  # {"muted": true or false} sets it; without, only says
            if isinstance(r.get("muted"), bool):
                mute(r["muted"])
            return {"ok": True, "muted": self.muted}

        return {
            "hold": hold,
            "release": release,
            "record": record,
            "status": lambda r: self.takes.state(self.takes.find(int(r["id"]))),
            "stop": lambda r: self.takes.finish(int(r["id"])),
            "say": say,
        } | ({"mute": muted} if mute else {})

    def _wait_held(self) -> str | None:
        """While a recording session holds TARS, instead of the wake word: the takes it asks for, and what's due in
        between. DUE when something is; None once the session lets go (or its hold lapses)."""
        while self.takes.held():
            if self._due():
                return DUE
            take = self.takes.next(HELD_POLL_S)
            if take:
                self._take(take)
            else:
                self.body.discard()
        self.body.clear()
        return None

    def _take(self, take: Take) -> None:
        """The beep, then what's said after it, until a pause ends it (the recorder's own rule) or a tap stops it."""
        self._chime()  # with the mic paused until it has left the speaker
        if not self.takes.recording(take):
            return
        heard: list[bytes] = []

        def hearing(pcm: bytes) -> None:
            heard.append(pcm)
            take.level = level(pcm)

        recorded = self.body.record(
            preroll_blocks=TAKE_PAD_BLOCKS,
            tail_blocks=TAKE_PAD_BLOCKS,
            start_timeout_s=TAKE_START_S,
            on_audio=hearing,
            stop=take.stop.is_set,
        )
        if recorded is not None:
            pcm, stopped = recorded.pcm, SILENCE
        elif heard:  # tapped while speaking: what was said so far is the take
            pcm, stopped = b"".join(heard), TAP
        else:
            pcm, stopped = None, TAP if take.stop.is_set() else NO_SPEECH
        self.takes.ended(take, pcm, stopped)

    def run_forever(
        self, idle_message: str, follow_up_s: float = 0.0, greet_after_s: float = 0.0, greet: str = PAUSE
    ) -> None:
        self.prepare_phrases()
        if self._reminder_clock:
            self._reminder_clock.start()
        wake = NO_WAKE  # the last wait's
        while True:
            print(f"\n{idle_message}")
            self._talk.clear()
            if self.takes.held():
                woke = self._wait_held()
                if woke is None:  # the session ended: back to listening for its name
                    continue
            elif self.muted:
                woke = self._wait_muted()
                if woke is None:  # unmuted: back to listening for its name
                    continue
            else:
                wake = self.body.wait(self._woken)
                woke = wake.how
            self._stop.clear()  # pressed while idle, it stops nothing
            if self._talk.is_set():
                self._talk.clear()
                if not self.muted:
                    self._woke_at, self._greet_s = time.monotonic(), None
                    print("Listening...  (the talk button)")
                    # A press carries no voice: the wake's audio is the last time its name was said, maybe by someone
                    # else, so it's greeted without a name; who's asking is told from what's said next.
                    self.converse(follow_up_s, greet=ALWAYS)
                if woke != DUE:
                    continue
            if self.muted and woke != DUE:
                print("(muted: not answering)")
                continue
            if woke == DUE:
                if self.announcements and self.announcements.waiting():
                    self.say_announcements(follow_up_s)
                if self._reminder_clock and self._reminder_clock.is_due():
                    self.say_reminders(follow_up_s)
                if self._skill_clock and self._skill_clock.is_due():
                    self.run_skills(follow_up_s)
                continue
            if woke == ASK:
                self.ask_if_called(follow_up_s)
                continue
            self._woke_at, self._greet_s = time.monotonic(), None
            print("Listening...")
            self.converse(follow_up_s, greet_after_s=greet_after_s, greet=greet, wake=wake)

    def prepare_phrases(self) -> None:
        """Synthesizes the short lines ahead so they play instantly. Called again on each wake, to add greetings for
        people enrolled since and retry lines that failed."""
        names = list(self.speaker_id.voiceprints) if self.speaker_id else []
        lines = [ASK_PHRASE, PARDON, greeting(None), *self.error_lines, *FILLERS, THINKING, ONE_MOMENT]
        if usage.has_cap():  # made while there's budget, so it's said for free once there isn't
            lines.append(CAPPED_LINE)
        for line in lines + [greeting(n) for n in names]:
            self.phrases.get(line, ahead=True)
        for line in self.commands.lines() if self.commands else []:
            self.phrases.get(line, ahead=True)

    def say(
        self,
        text: str,
        keep: bool = True,
        on_first_audio: Callable[[], None] | None = None,
        wait_s: float = PHRASE_WAIT_S,
    ) -> bool:
        """Plays a short line, made ahead if possible (`keep`: one of the fixed lines), waiting at most `wait_s` for
        it to be made. False if it couldn't be said."""
        try:
            audio = self.phrases.get(text, keep).result(timeout=wait_s)
        except Exception as e:  # noqa: BLE001 - a line TARS can't say is skipped, never fatal
            print(f"(couldn't say {text!r}: {e!r})")
            return False
        self._play(audio, on_first_audio)
        return True

    def _play(self, audio: list[bytes], on_first_audio: Callable[[], None] | None = None) -> None:
        self._cut_short(self.body.play(iter(audio), self.voice.sample_rate, on_first_audio))

    def _made_now(self, lines: list[str]) -> dict[str, list[bytes]]:
        """The audio of each of `lines` that could be made within UNPROMPTED_WAIT_S."""
        made: dict[str, Audio] = {line: self.phrases.get(line, keep=False) for line in lines}
        wait(made.values(), UNPROMPTED_WAIT_S)
        ready = {line: audio.result() for line, audio in made.items() if audio.done() and not audio.exception()}
        for line, audio in made.items():
            if line not in ready:
                why = repr(audio.exception()) if audio.done() else "not made in time"
                print(f"(couldn't say {line!r}: {why})")
        return ready

    def _chime(self) -> None:
        self._play([chime(self.voice.sample_rate)])

    def say_error(self) -> None:
        # One still being made gets a moment: it's quicker than asking again.
        made = {line: audio for line in self.error_lines if (audio := self.phrases.made_ahead(line))}
        wait(made.values(), ERROR_LINE_WAIT_S, FIRST_COMPLETED)
        ready = [line for line, audio in made.items() if audio.done() and not audio.exception()]
        if not ready:  # the voice was never reachable, so there's nothing to say it with
            print("(couldn't say what went wrong: the voice isn't reachable either)")
            return
        line = random.choice(ready)
        print(f"Bot:  {line}")
        self.say(line)

    def say_capped(self) -> None:
        """That the month's cap is reached, from the line made ahead; with none, nothing is made now to say it."""
        print(f"Bot:  {CAPPED_LINE}  (past the month's cap)")
        if self.phrases.made_ahead(CAPPED_LINE):
            self.say(CAPPED_LINE)
        else:
            print("(couldn't say it: the line wasn't made ahead)")

    def _prepare_reminders(self, active: list[dict]) -> None:
        """Makes each active reminder's line ahead, so it plays at once when it's due."""
        # A message waiting for someone is said in a conversation with them (held_line), or, once their phone is home
        # and it has a time, like any reminder.
        lines = {reminder_line(r) for r in active if r["due"] is not None or r["next_at"] is not None}
        self.phrases.keep_only(lines | {held_line(r) for r in active if r["due"] is None})

    def _due(self) -> bool:
        clock = self._reminder_clock
        skills = self._skill_clock
        return (
            bool(self.announcements and self.announcements.waiting())
            or bool(clock and clock.is_due())
            or bool(skills and skills.is_due())
        )

    def say_announcements(self, follow_up_s: float = 0.0) -> None:
        """Says what's waiting to be said (the TV is playing, or why it isn't), then, if it asked something ("Toy
        Story, or Toy Story 4?"), listens a moment for the answer without the wake word. News that asks nothing
        doesn't listen: what's heard next is mostly the TV it just put on. What's said joins the conversation."""
        lines = self.announcements.take() if self.announcements else []
        audio = self._made_now(lines)
        if unsaid := [line for line in lines if line not in audio]:
            print(f"(trying {len(unsaid)} announcement(s) again in {VOICE_RETRY_S:.0f} s)")
            self.announcements.put_back(unsaid, VOICE_RETRY_S)
        said = [line for line in lines if line in audio]
        if not said:
            return
        self.journal.unprompted()
        self._chime()
        for line in said:
            if self._stop.is_set():
                break
            print(f"Bot:  {line}  (on its own)")
            self._play(audio[line])
            self.journal.said(line)
        self.brain.remember_said(" ".join(said))
        listens = bool(follow_up_s) and asks(said)
        with self._open_for_answer(listens) as answer_s:
            heard = (
                self.listen(start_timeout_s=follow_up_s, follow_up=True, max_s=answer_s, expect=offered(" ".join(said)))
                if listens
                else None
            )
            if heard is None:
                self.journal.nobody_spoke()
                return
            self.converse(0.0 if answer_s else follow_up_s, first=heard, asked=" ".join(said))

    def run_skills(self, follow_up_s: float = 0.0) -> None:
        """Each skill whose time has come, or that something happening set off: a turn of its own, with its
        instructions as the request, whose reply is said after a chime; then it listens a moment, without the wake
        word, for an answer to what it asked. A one-off's try that didn't happen isn't said: it's tried again, or
        given up and whoever asked is told (skills.Clock.settle). One to answer in a chat, run here because Telegram
        isn't running, is posted in their chat once Telegram is back, and one done whose answer can't be said now is
        said with the reminders (skills.Clock.hand_over). What a turn sent, and asked of the reminders, is made once
        its line is kept, said or handed over, as an ordinary turn's is once it's heard (_skill_did); never for one
        skipped, one that didn't happen, or one nobody heard."""
        clock = self._skill_clock
        for due in clock.take() if clock else []:
            with usage.turn(None if due.skill.household else due.skill.owner, usage.SKILL):
                skill, house = due.skill, Answers()
                print(f"(skill {skill.name}: {due.why or f'at {due.when:%H:%M}'})")
                # Their feeds and preferences; what the house answers, for whether a one-off happened; and one a door's
                # lock set off changes nothing in the house.
                whose = Request(speaker=None if skill.household else skill.owner, changed=house, by_door=due.door)
                try:
                    if usage.capped():
                        raise usage.Capped()
                    written = Reply(self.brain, on_its_own(skill, due.when, SKIP, due.why), whose)
                    text = written.read()
                except Exception as e:  # noqa: BLE001 - a skill that can't run waits for its next time
                    print(f"(skill {skill.name} didn't run: {e!r})")
                    if skill.once is not None:
                        clock.settle(due, house, e)
                    continue
                sent, changes = list(self.brain.sent), list(self.brain.changes)  # the brain forgets them with the turn
                if skill.once is not None and happened(house)[0]:
                    print(f"(skill {skill.name}: not said, since it didn't happen: {text!r})")
                    written.drop()
                    clock.settle(due, house)
                    continue
                if not text:  # skipped, or nothing at all
                    print(f"(skill {skill.name}: nothing to do now)")
                    written.drop()
                    clock.record(due, "")
                    continue
                if skill.say != ALOUD:  # not the room's to hear, nor part of its conversation
                    written.drop()
                    clock.hand_over(due, text)
                    self._skill_did(sent, changes, whose.speaker)
                    continue
                audio = self._made_now([text])
                if text not in audio:  # nobody heard it: not part of the conversation, as a reply that isn't heard
                    written.drop()
                    if skill.once is not None:  # done all the same: its answer waits for the voice
                        clock.hand_over(due, text)
                        self._skill_did(sent, changes, whose.speaker)
                    continue
                clock.record(due, text)
                self.journal.unprompted()
                self._chime()
                print(f"Bot:  {text}  (skill {skill.name})")
                self._play(audio[text])
                self.journal.said(text)
                self._skill_did(sent, changes, whose.speaker)
                with self._open_for_answer(bool(follow_up_s) and asks([text])) as answer_s:
                    heard = (
                        self.listen(start_timeout_s=follow_up_s, follow_up=True, max_s=answer_s, expect=offered(text))
                        if follow_up_s
                        else None
                    )
                    if heard is None:
                        self.journal.nobody_spoke()
                        continue
                    self.converse(0.0 if answer_s else follow_up_s, first=heard, asked=text)

    def _skill_did(self, sent: list[SentItem], changes: list[Change], who: str | None) -> None:
        """What a skill's own turn sent, and asked of the reminders, once its line is kept: kept for the TARS page
        (and `who`'s chat, as anything sent at home for them), and made, from `who`, as an ordinary turn's are."""
        for item in sent:
            print(f"(sent to the TARS page: {item.kind} '{item.title}')")
        self.journal.sent(sent, who)
        self._change_reminders(changes, who)

    def say_reminders(self, follow_up_s: float = 0.0) -> None:
        """Says what's due, after a chime, and listens a moment for "got it" if any of it waits for that."""
        clock = self._reminder_clock
        try:
            due = [r for r in self.reminders.due() if not (clock and clock.held(r["id"]))]
        except Exception as e:  # noqa: BLE001 - the table failed: give it a rest, or the wake loop would stop for it
            print(f"(couldn't read the reminders: {e!r})")
            if clock:
                clock.pause(REMINDERS_REST_S)
            return
        if self._elsewhere and len(kept := [r for r in due if not self._elsewhere(r)]) < len(due):
            due = kept
            self._reread_reminders()
        lines = {r["id"]: [t for t in [reminder_line(r), late(r)] if t] if says_line(r) else [] for r in due}
        audio = self._made_now([t for texts in lines.values() for t in texts])
        said = []
        for r in due:
            if lines[r["id"]] and not any(t in audio for t in lines[r["id"]]):
                print(f"(couldn't say reminder {r['id']}; trying again in {VOICE_RETRY_S:.0f} s)")
                self._hold(r["id"], VOICE_RETRY_S)
            else:
                said.append(r)
        if said:
            self.journal.unprompted()  # what's said back answers the reminder, not the last wake
            self._chime()
        # Reminders whose line was begun, or that are only the chime (a timer ringing on): one stopped before its
        # line is said again later.
        heard = [r["id"] for r in said if not lines[r["id"]]]
        for r in said:
            for text in [t for t in lines[r["id"]] if t in audio]:
                if self._stop.is_set():
                    break
                print(f"Bot:  {text}  (reminder {r['id']})")
                heard.append(r["id"])
                self._play(audio[text])
                self.journal.said(text)
            if r["id"] in heard:
                self._mark_said(r)
        self._reread_reminders()
        self._just_said = [r["id"] for r in said if r["needs_ack"] and r["id"] in heard]
        if self._stop.is_set():  # the stop button: what was begun is acknowledged, and nobody is listened for
            if self._just_said:
                print("(the stop button: acknowledged)")
                self._ack(list(self._just_said), None)
            self._just_said = []
            return
        try:
            with self._open_for_answer(bool(self._just_said), '"got it"') as answer_s:
                heard = (
                    self.listen(start_timeout_s=self.ack_window_s, tag=REMINDER_TAG, max_s=answer_s)
                    if self._just_said
                    else None
                )
                if heard is None:
                    self.journal.nobody_spoke()
                    return
                self.converse(0.0 if answer_s else follow_up_s, first=heard)
        finally:
            self._just_said = []

    @contextmanager
    def _open_for_answer(self, waits: bool, what: str = "the answer"):
        """Muted, nobody could answer what TARS just said: when it `waits` for an answer, unmuted for it, with the
        light showing, then muted again, unless someone's button already did. Yields how long the answer may be
        (ANSWER_ONLY_S) when it unmuted, else None: the answer is all it hears, and once it's replied, it listens for
        no follow-up, since the room was muted for a reason."""
        set_muted = self.body.set_muted
        opened = bool(waits and self.muted and set_muted)
        if opened:
            print(f"(muted: listening for {what} only)")
            set_muted(False)
        try:
            yield ANSWER_ONLY_S if opened else None
        finally:
            if opened and not self.muted:
                set_muted(True)

    def _say_held_line(self, r: dict) -> bool:
        text = held_line(r)
        print(f"Bot:  {text}  (reminder {r['id']})")
        if not self.say(text, keep=False, wait_s=UNPROMPTED_WAIT_S):
            return False
        self.journal.tars_said(text)
        return True

    def _mark_said(self, r: dict) -> None:
        try:
            self.reminders.said(r["id"])
        except Exception as e:  # noqa: BLE001 - it was said: hold it, so a table that won't take it can't loop it
            print(f"(couldn't mark reminder {r['id']} as said: {e!r})")
            self._hold(r["id"], r["repeat_every_s"])

    def _hold(self, reminder_id: int, seconds: float) -> None:
        if self._reminder_clock:
            self._reminder_clock.hold(reminder_id, time.time() + seconds)

    def _reread_reminders(self) -> None:
        if self._reminder_clock:
            self._reminder_clock.read()

    def _ack(self, ids: list[int], who: str | None, changed: Collection[int] = ()) -> None:
        """A bare <ack> is for the reminders just said, or else the one said last. `changed`: the reminders the same
        reply put off or cancelled, which it doesn't acknowledge."""
        try:
            ids = ids or self._just_said or [r["id"] for r in self.reminders.waiting()[:1]]
            for i in [i for i in ids if i not in changed]:
                # Just said, it may still be scheduled: the table didn't take its saying.
                said = i in self._just_said or (self._reminder_clock and self._reminder_clock.held(i))
                if (self.reminders.dismiss if said else self.reminders.ack)(i, who, VOICE):
                    print(f"(reminder {i} acknowledged by {who or 'an unknown voice'})")
        except Exception as e:  # noqa: BLE001 - the reply was said; only the record of it failed
            print(f"(couldn't mark the reminder acknowledged: {e!r})")
        self._reread_reminders()

    def wake_speaker(self, audio: np.ndarray | None) -> str | None:
        """Who said the wake word in `audio`, for the greeting; None if nobody is sure, or speaker ID couldn't tell."""
        if not self.speaker_id or audio is None:
            return None
        try:
            return self.speaker_id.identify(audio.astype("int16").tobytes(), threshold=self.name_threshold)
        except Exception as e:  # noqa: BLE001 - greeted without a name
            print(f"(couldn't tell who spoke: {e!r})")
            return None

    def greet(self, wake: Wake) -> None:
        text = greeting(self.wake_speaker(wake.audio))
        print(f"Bot:  {text}")
        if self.say(text, on_first_audio=self._greeted):
            self.journal.said(text)

    def greet_and_listen(self, wake: Wake = NO_WAKE) -> Utterance | None:
        """Says "Yes, <name>?" the moment the wake is confirmed. With the echo taken out of what the mic hears, it's
        said with the mic open, so whatever follows the wake word straight away is heard too; otherwise the mic
        waits for the greeting, as for anything TARS says. What followed the wake word before listening began (the
        wake's after_name) is dropped when the mic waits for the greeting, as what's heard after it doesn't run on
        from the name."""
        if not self.body.hears_while_speaking:
            self.greet(wake)
            return self.listen()
        text = greeting(self.wake_speaker(wake.audio))
        print(f"Bot:  {text}  (listening while it's said)")
        try:
            audio = self.phrases.get(text).result(timeout=PHRASE_WAIT_S)
        except Exception as e:  # noqa: BLE001 - no greeting, but still listening
            print(f"(couldn't say {text!r}: {e!r})")
            return self.listen(after_name=wake.after_name)
        failed: list[BaseException] = []

        def play() -> None:
            try:
                self.body.play(iter(audio), self.voice.sample_rate, self._greeted, listening=True)
            except BaseException as e:  # noqa: BLE001 - raised again once listening is over
                failed.append(e)

        playing = threading.Thread(target=play, daemon=True, name="greeting")
        playing.start()
        try:
            heard = self.listen(echo_of=text, after_name=wake.after_name)
        finally:
            playing.join()
        if failed:
            raise failed[0]
        self.journal.said(text)
        return heard

    def _greeted(self) -> None:
        """The greeting's first sound: how long after the wake word it came, for the turn's timings."""
        if self._woke_at is not None:
            self._greet_s = time.monotonic() - self._woke_at
            print(f"(greeted {self._greet_s:.2f}s after the wake word)")

    def ask_if_called(self, follow_up_s: float) -> None:
        """The wake sounded almost right ("hey cars"?): ask, and only carry on if someone answers."""
        print(f"Bot:  {ASK_PHRASE}  (not sure I heard my name)")
        if not self.say(ASK_PHRASE):
            return
        self.journal.said(ASK_PHRASE)
        heard = self.listen(start_timeout_s=ASK_TIMEOUT_S, tag=ASKED_TAG)
        if heard is None:
            print("(no answer, going back to sleep)")
            self.journal.nobody_spoke()
            return
        self.converse(follow_up_s, first=heard)

    def listen(
        self,
        start_timeout_s: float | None = None,
        follow_up: bool = False,
        tag: str | None = None,
        echo_of: str | None = None,
        max_s: float | None = None,
        after_name: Future[np.ndarray] | None = None,
        expect: tuple[str, ...] = (),
    ) -> Utterance | None:
        """Records one utterance, transcribing it as it comes, and starts answering at each pause in case it's the
        end. `max_s`: the longest it records, instead of the configured longest. `after_name`: what followed the wake
        word in the wake window: a request said in one breath with the name starts there. `expect`: words likely
        said, the choices TARS just offered."""
        # Past the month's cap, nothing goes out: the recording is kept here, for a house command heard on the Pi.
        session = BufferedSession(_over_the_cap) if usage.capped() else self.transcriber.session(expect)
        draft: Draft[Answer] | None = None

        def paused(pcm: bytes) -> None:
            nonlocal draft
            draft = self._draft(pcm, session, follow_up, tag, echo_of)

        def resumed() -> None:
            nonlocal draft
            if draft:
                draft.cancel()
                # A draft still waiting on the brain (a follow-up it may skip) lets go of the turn sooner. Nothing
                # else is being written now: the last reply was over before listening began.
                self.brain.interrupt()
                draft = None

        try:
            recorded = self.body.record(
                start_timeout_s=start_timeout_s,
                on_audio=session.feed,
                # Local first, nothing is heard until the turn is over: no answer is started in a pause.
                on_pause=None if self.commands and self.commands.local_first else paused,
                on_resume=resumed,
                turn_state=session.turn_state,
                stop=self._stop.is_set,
                max_s=max_s,
                lead=_found(after_name),
            )
        except BaseException:
            # Every draft must be settled, or the next one would wait behind it.
            resumed()
            session.close()
            raise
        if recorded is None:
            resumed()
            session.close()
            return None
        session.finish()
        pcm = recorded.pcm
        return Utterance(
            pcm,
            session,
            draft or self._draft(pcm, session, follow_up, tag, echo_of),
            recorded.silence_s,
            follow_up,
            echo_of,
        )

    def converse(
        self,
        follow_up_s: float,
        first: Utterance | None = None,
        greet_after_s: float = 0.0,
        greet: str = PAUSE,
        wake: Wake = NO_WAKE,
        asked: str = "",
    ) -> None:
        """Answer one request, then keep listening for follow-ups (no wake word) until nobody speaks.

        `first` is an already-recorded request (a reply to "Did you call me?"). `greet`: when to say "Yes, <name>?":
        ALWAYS at once, PAUSE once nothing has followed the wake word for `greet_after_s`, or NEVER. `wake`: what woke
        it, whose voice the greeting names, and what followed the name before listening began, where the request
        starts if it was said in one breath. `asked`: what TARS said on its own that `first` answers, so an answer
        that comes out as no words is asked for again.
        """
        # While they're still talking, get the reply's connections ready.
        threading.Thread(target=self.brain.warm, daemon=True, name="brain-warm").start()
        self.voice.warm()
        if not usage.capped():  # a line made now would be paid for
            self.prepare_phrases()
        try:
            with usage.turn(None, usage.VOICE):  # whose, once handle() knows the voice
                self._converse(follow_up_s, first, greet_after_s, greet, wake, asked)
        finally:
            self._just_said = []
            self.journal.end_conversation()

    def _converse(
        self,
        follow_up_s: float,
        first: Utterance | None,
        greet_after_s: float,
        greet: str,
        wake: Wake,
        asked: str,
    ) -> None:
        heard, pardoned, self._last_reply = first, False, asked
        if heard is None:
            if greet == ALWAYS:
                heard = self.greet_and_listen(wake)
            elif greet == PAUSE and greet_after_s:
                heard = self.listen(start_timeout_s=greet_after_s, after_name=wake.after_name)
                if heard is None:
                    self.greet(wake)
                    heard = self.listen()
            else:
                heard = self.listen(after_name=wake.after_name)
        if heard is None:
            print("(didn't hear anything)")
            self.journal.nobody_spoke()
            return
        while True:
            try:
                answered = self.handle(heard)
            except AudioDeviceError, BodyGone:  # the device, or the room's body, is gone: not a request that failed
                raise
            except Exception as e:  # noqa: BLE001 - one failed request shouldn't take the assistant down
                if self._stop.is_set():  # the stop button cut the reply off: that's no failure to say
                    print("(stopped)")
                    return
                if isinstance(e, usage.Capped):
                    self.say_capped()
                    return
                print(f"Error: {e!r}")
                if not isinstance(e, SERVICE_FAILURES):  # a service failing is no bug to trace
                    traceback.print_exc()
                self.say_error()
                return
            if self._stop.is_set():
                print("(stopped)")
                return
            if not answered and heard.echo_of and self._only_echo:
                # All it heard was a trace of its own greeting: the request is still to come.
                heard = self.listen()
                if heard is None:
                    return
                continue
            if not answered and self._only_echo and heard.follow_up and asks([self._last_reply]) and not pardoned:
                # Someone spoke after TARS asked, and no words came of it: an answer said as the question ended, its
                # start cut off ("twelve" to "11 or 12?"). Dropped, they'd wait for something that never happens.
                print("(heard someone after asking, but no words: asking again)")
                pardoned = True
                print(f"Bot:  {PARDON}")
                if self.say(PARDON):
                    self.journal.tars_said(PARDON)
                heard = self.listen(start_timeout_s=follow_up_s, follow_up=True, expect=offered(self._last_reply))
                if heard is None:
                    return
                continue
            if answered:
                pardoned = False
            held = answered and self._say_held(self._last_who)
            if held:  # what's said next may well be about the message: listen for it, at least the ack window
                heard = self.listen(
                    start_timeout_s=max(follow_up_s, self.ack_window_s), follow_up=True, tag=REMINDER_TAG
                )
            elif not answered or follow_up_s <= 0:
                return
            else:
                print(f"(listening {follow_up_s:.0f}s for a follow-up)")
                heard = self.listen(start_timeout_s=follow_up_s, follow_up=True, expect=offered(self._last_reply))
            if heard is None:
                return

    def _say_held(self, who: str | None) -> bool:
        """Says the messages waiting until `who` was heard, if any; True if one waits for an acknowledgement."""
        if not (self.reminders and who):
            return False
        try:
            held = self.reminders.held_for(who)
        except Exception as e:  # noqa: BLE001 - they wait for the next time
            print(f"(couldn't read the reminders: {e!r})")
            return False
        said = [r for r in held if self._say_held_line(r)]
        if not said:  # nothing held, or the voice is down: it waits for the next time they're heard
            return False
        for r in said:
            self._mark_said(r)
        self._just_said = [r["id"] for r in said if r["needs_ack"]]
        self._reread_reminders()
        return bool(self._just_said)

    def handle(self, heard: Utterance) -> bool:
        """Returns False if there was nothing meant for TARS to answer."""
        # Latency counts from when the speaker actually stopped, including the silence waited through.
        t_over = time.perf_counter()
        t_stopped_talking = t_over - heard.silence_s
        self.timings = {}
        answer: Answer | None = None
        try:
            heard.draft.go()  # the turn is over: what the answer does in the house can be done
            answer = heard.draft.take()
            name, score, embedding = answer.who
            if heard.echo_of and not answer.text:
                name, score, embedding = None, None, None
            self._last_who = name
            usage.identified(name)
            who = f" ({name or 'unknown'})" if self.speaker_id else ""
            print(f"You{who}:  {answer.text!r}")
            # Only the request right after the wake says whether the wake was real.
            self.journal.heard(heard.pcm, answer.text, name, score, embedding, first=not heard.follow_up)
            self._only_echo = not answer.text
            if not answer.text:
                heard.draft.keep()
                return False
            if answer.error:
                raise answer.error
            if answer.written and answer.written.skipped:
                print("(not meant for me, going quiet)")
                self.journal.not_for_tars()
                heard.draft.keep()
                return False
            self.timings = {"end_of_speech": heard.silence_s, "stt": answer.stt_s}
            if answer.t_asked:  # how long before the turn was over the brain was asked; 0: only after it was over
                self.timings["head_start"] = max(0.0, t_over - answer.t_asked)
            if self._greet_s is not None:  # the conversation's first request: how soon TARS greeted the wake word
                self.timings["greet"], self._greet_s = self._greet_s, None
            said = self._speak(answer, t_stopped_talking)
            self._last_reply = said.text
            if answer.written and answer.written.pardoned:  # the repeat answers it: not an untethered follow-up to skip
                self.brain.remember_said(PARDON)
            heard.draft.keep()
        except BaseException as e:
            # Unheard, or only half heard: stop it, and leave the brain as if it was never asked.
            heard.draft.cancel()
            if isinstance(e, Exception) and not isinstance(e, (AudioDeviceError, BodyGone)):
                self._log_failure(e, answer)
            raise
        finally:
            heard.session.close()
        self.journal.answered(
            said.text,
            said.sent,
            asker=name,
            timings=self.timings,
            answered_by=said.answered_by,
            quick_service=said.quick_service,
        )
        acks = answer.written.acks if answer.written else None
        # The brain's changes are its last reply's; a house command done on the Pi is none of its replies.
        changes = [] if answer.on_pi else self.brain.changes
        if acks is not None and self.reminders:
            # One the reply also put off or cancelled is that change's: acknowledged, it couldn't be put off.
            self._ack(acks, name, changed={c.id for c in changes if c.id is not None})
        self._change_reminders(changes, name)
        return True

    def _change_reminders(self, changes: list[Change], who: str | None) -> None:
        """Sets, cancels or snoozes what the reply asked for, now that it was kept."""
        if not changes or not self.reminder_tools:
            return
        self.journal.flush()
        for change in changes:
            try:
                self.reminder_tools.apply(change, from_name=who, conversation_id=self.journal.conversation_id)
                print(f"(reminders: {change.action} done)")
            except Exception as e:  # noqa: BLE001 - checked when the model asked; only a broken table gets here
                print(f"(couldn't {change.action} the reminder: {e!r})")
        self._reread_reminders()

    def _log_failure(self, e: Exception, answer: Answer | None) -> None:
        """Kept in the conversation, where it failed and why, with what TARS got to say of the answer."""
        text = "".join(answer.spoken).strip() if answer else ""
        if answer:
            self._measure(answer, answer.t_asked, None)
        asked = bool(answer and answer.t_asked)
        on_pi = bool(answer and answer.on_pi)  # no model answered it: the brain's are its last reply's
        self.journal.failed(
            text,
            failed_at(e),
            f"{type(e).__name__}: {e}",
            dict(self.timings),
            answered_by=ON_PI if on_pi else self.brain.answered_by if asked else None,
            quick_service=self.brain.quick_service if asked and not on_pi else None,
        )

    def _draft(
        self, pcm: bytes, session: Session, follow_up: bool, tag: str | None, echo_of: str | None = None
    ) -> Draft[Answer]:
        return Draft(self._thinking, lambda d: self._prepare(d, pcm, session, follow_up, tag, echo_of), self._discard)

    def _prepare(
        self, draft: Draft, pcm: bytes, session: Session, follow_up: bool, tag: str | None, echo_of: str | None = None
    ) -> Answer:
        """Transcribe, identify the speaker, and set the brain and voice to work, all without a sound."""
        # Speaker ID runs while the audio is transcribed, so it adds no latency.
        identifying = self._identifying.submit(self.speaker_id.describe, pcm) if self.speaker_id else None
        t = time.perf_counter()
        try:
            text = without_echo(session.transcript(), echo_of)
        except Exception as e:
            # With the cloud out of reach, a house command can still be heard on the Pi.
            capped = isinstance(e, usage.Capped)
            text = "" if draft.cancelled else self._heard_on_pi(pcm, capped)
            if not text:
                mark_failed_at(e, STT)
                raise
            why = "past the month's cap" if capped else f"speech to text didn't answer ({e!r})"
            print(f"({why}; heard a house command on the Pi)")
        stt_s = time.perf_counter() - t
        answer = Answer(text, self._who(identifying), stt_s)
        if not text or draft.cancelled:
            return answer
        prompt = self._prompt(text, answer.who.name, follow_up, tag)
        request = Request(answer.who.name, answer.who.score, by_voice=True, heard=text, may_act=draft.may_act)
        # A follow-up may not be meant for TARS (the TV, someone else in the room): only the model may skip it.
        if self.commands and tag is None and not follow_up and (command := self.commands.commands.match(text)):
            return self._command_reply(answer, command, prompt, request)
        try:
            # A follow-up, or a reply to "Did you call me?" (maybe a "no"), may not be meant for TARS at all.
            return self._start_reply(answer, prompt, may_skip=follow_up or tag is not None, request=request)
        except Exception as e:  # noqa: BLE001 - raised again in handle(), after what was heard is kept
            answer.error = e  # already forgotten
            return answer

    def _heard_on_pi(self, pcm: bytes, capped: bool = False) -> str:
        """A house command Vosk heard in the recording, or "". Local first, Vosk has listened already, unless the
        recording never went through it (`capped`: past the month's cap)."""
        if not (self.commands and self.commands.hear) or (self.commands.local_first and not capped):
            return ""
        try:
            heard = self.commands.hear(pcm)
        except Exception as e:  # noqa: BLE001 - the cloud's failure is what's said
            print(f"(couldn't listen for a house command on the Pi: {e!r})")
            return ""
        return heard if self.commands.commands.match(heard) else ""

    def _command_reply(self, answer: Answer, command: Command, prompt: str, request: Request) -> Answer:
        """A house command, done with the home tool once the turn is over and answered from lines made ahead, with no
        model; the exchange joins the conversation."""
        answer.on_pi, answer.t_asked = True, time.perf_counter()
        commands = self.commands

        def pieces() -> Iterator[str]:
            try:
                result = commands.call(command.args, request)
            except Exception as e:  # noqa: BLE001 - a device's failure, said as one
                result = f"error: {e!r}"
            print(f"(house command on the Pi: {command.device} {command.action}: {result})")
            if not request.go_ahead():  # thrown away: nothing was done, and nothing is said or remembered
                return
            said = commands.replies.say(command, result)
            self.brain.remember_exchange(prompt, said)
            yield from (f"{s} " for s in sentences(said))

        answer.reply = StreamedReply(
            _tee(pieces(), answer.spoken),
            _MadeAhead(self.voice, self.phrases),
            lambda sentence: answer.sentences.append((time.perf_counter(), sentence)),
        )
        return answer

    @staticmethod
    def _who(identifying: Future[tuple[str | None, float | None, np.ndarray | None]] | None) -> Identified:
        """Who spoke, or NOBODY if speaker ID couldn't tell: the request is answered either way."""
        if identifying is None:
            return NOBODY
        try:
            return Identified(*identifying.result())
        except Exception as e:  # noqa: BLE001 - answered as for an unknown voice
            print(f"(couldn't tell who spoke: {e!r})")
            return NOBODY

    def _start_reply(self, answer: Answer, prompt: str, may_skip: bool, request: Request | None = None) -> Answer:
        answer.t_asked = time.perf_counter()
        try:
            answer.written = Reply(self.brain, prompt, request, may_skip=may_skip, acks=bool(self.reminders))
        except BaseException as e:
            mark_failed_at(e, LLM)
            raise
        if answer.written.skipped:
            return answer
        answer.reply = StreamedReply(
            _tee(answer.written, answer.spoken),
            self.voice,
            lambda sentence: answer.sentences.append((time.perf_counter(), sentence)),
            self.phrases.made_ahead,
        )
        return answer

    def _discard(self, answer: Answer) -> None:
        if answer.reply:
            self.brain.interrupt()
            answer.reply.stop()
        if answer.written:
            answer.written.drop()

    def _prompt(self, text: str, name: str | None, follow_up: bool, tag: str | None) -> str:
        if self.speaker_id:
            text = f"[Speaker: {name or 'unknown'}] {text}"
        if follow_up:
            text = f"{FOLLOW_UP_TAG} {text}"
        if tag:
            text = f"{tag} {text}"
        return text

    def answer_text(self, text: str) -> Said:
        answer = self._start_reply(Answer(text, NOBODY, 0.0), text, may_skip=False)
        self.timings = {}
        try:
            said = self._speak(answer, answer.t_asked)
        except BaseException:
            self._discard(answer)
            raise
        if answer.written and answer.written.pardoned:  # as aloud: what's typed next answers it
            self.brain.remember_said(PARDON)
        return said

    def _speak(self, answer: Answer, t_start: float) -> Said:
        first_audio: list[float] = []
        printed = 0

        def audio() -> Iterator[bytes]:
            nonlocal printed
            for chunk in answer.reply:
                for _, sentence in answer.sentences[printed:]:
                    print(f"Bot:  {sentence}")
                printed = len(answer.sentences)
                yield chunk

        cut = self.body.play(audio(), self.voice.sample_rate, lambda: first_audio.append(time.perf_counter()))
        for _, sentence in answer.sentences[printed:]:
            print(f"Bot:  {sentence}")
        sent = [] if answer.on_pi else list(self.brain.sent)
        for item in sent:
            print(f"(sent to the TARS page: {item.kind} '{item.title}')")
        self._measure(answer, t_start, first_audio[0] if first_audio else None)
        self._report()
        self._cut_short(cut)
        if answer.on_pi:
            return Said("".join(answer.spoken).strip(), sent, ON_PI, None)
        return Said("".join(answer.spoken).strip(), sent, self.brain.answered_by, self.brain.quick_service)

    def _measure(self, answer: Answer, t_start: float, t_sound: float | None) -> None:
        if answer.sentences:
            self.timings["llm"] = answer.sentences[0][0] - answer.t_asked
        if answer.sentences and t_sound:
            self.timings["tts"] = max(0.0, t_sound - answer.sentences[0][0])
        if t_sound:
            self.timings["total"] = t_sound - t_start

    def _report(self) -> None:
        labels = {
            "greet": "greeted",
            "end_of_speech": "waited",
            "head_start": "answer started early by",
            "stt": "stt",
            "llm": "llm first sentence",
            "tts": "tts first audio",
        }
        parts = [
            f"{labels[k]} {v:.2f}s"
            for k, v in self.timings.items()
            if k in labels and not (k == "head_start" and round(v, 2) == 0)
        ]
        if "total" in self.timings:
            # With the answer started during the pause, the stages overlap the wait instead of adding to it.
            parts.append(f"TOTAL to first sound {self.timings['total']:.2f}s")
        print(f"[{' | '.join(parts)}]")


def _found(after_name: Future[np.ndarray] | None) -> np.ndarray | None:
    """What followed the name, once it's been found; None if it couldn't be, and the request is heard from the mic."""
    if after_name is None:
        return None
    try:
        return after_name.result(timeout=AFTER_NAME_WAIT_S)
    except Exception as e:  # noqa: BLE001 - only what came before listening is lost, as it always was
        print(f"(couldn't tell where the name ended: {e!r})")
        return None


def _tee(pieces: Iterator[str], into: list[str]) -> Iterator[str]:
    for piece in pieces:
        into.append(piece)
        yield piece


def _over_the_cap(pcm: bytes) -> str:
    """A turn's speech to text past the month's cap: nothing sent, and nothing heard but a house command on the Pi."""
    raise usage.Capped()
