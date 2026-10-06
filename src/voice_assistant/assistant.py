"""The conversation loop: wait for the wake word, listen, answer out loud, and keep listening for follow-ups.

Each request is transcribed while it's spoken, an answer is drafted at the first pause, and the reply is spoken
sentence by sentence as the brain writes it. Short lines ("Yes?", "Did you call me?", the error lines) are made ahead
and kept on disk, so they play at once, even offline.
"""

import hashlib
import random
import threading
import time
import traceback
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import numpy as np
from openai import OpenAIError

from .audio import AudioDeviceError, Microphone, Speaker, chime
from .config import LLMConfig, RemindersConfig, SpeakerConfig
from .conversations import SentItem
from .draft import Draft
from .files import atomic_write
from .journal import Journal
from .llm import ASKED_TAG, FOLLOW_UP_TAG, REMINDER_TAG, Brain, split_ack, split_skip
from .recorder import UtteranceRecorder
from .reminders import VOICE, Clock, ReminderTools, held_line, late
from .reminders import line as reminder_line
from .speaker import SpeakerID
from .speech import StreamedReply, failed_at, mark_failed_at
from .stt import Session, Transcriber
from .tts import Voice
from .verify import ASK
from .wake import DUE, Trigger

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
ERROR_LINE_WAIT_S = 1.0


def greeting(name: str | None) -> str:
    return f"Yes, {name.capitalize()}?" if name else "Yes?"


class Identified(NamedTuple):
    name: str | None
    score: float | None
    embedding: np.ndarray | None


NOBODY = Identified(None, None, None)


@dataclass
class Said:
    text: str
    sent: list[SentItem]
    answered_by: str | None = None  # which model wrote it (Brain.answered_by)


@dataclass
class Utterance:
    pcm: bytes
    session: Session  # transcribing it, fed while it was recorded
    draft: "Draft[Answer]"
    silence_s: float  # how long ago they stopped talking when the recording ended
    follow_up: bool = False


@dataclass
class Answer:
    """A request, transcribed, and the reply being made to it, not yet heard by anyone."""

    text: str
    who: Identified
    stt_s: float
    asked: bool = False  # the brain was asked, so a discarded answer must be forgotten
    t_asked: float = 0.0
    reply: StreamedReply | None = None  # None: nothing to say (no words, or not meant for TARS)
    skipped: bool = False  # an overheard follow-up the brain chose not to answer
    spoken: list[str] = field(default_factory=list)
    error: Exception | None = None  # the brain failed to start the reply; raised once what was heard is written down
    acks: list[int] | None = None  # the reply acknowledged reminders: these, or [] for the ones just said
    sentences: list[tuple[float, str]] = field(default_factory=list)  # (when it was written, the sentence)


class Assistant:
    """Typed questions (answer_text) need only the speaker, the brain and the voice: the rest may be None."""

    def __init__(
        self,
        mic: Microphone | None,
        speaker: Speaker,
        trigger: Trigger | None,
        recorder: UtteranceRecorder | None,
        transcriber: Transcriber | None,
        brain: Brain,
        voice: Voice,
        speaker_id: SpeakerID | None = None,
        name_threshold: float = SpeakerConfig.wake_threshold,
        journal: Journal | None = None,
        humor: int = LLMConfig.humor,
        phrases: Path | None = None,
        reminders: ReminderTools | None = None,
        ack_window_s: float = RemindersConfig.ack_window_s,
    ):
        self.mic = mic
        self.speaker = speaker
        self.trigger = trigger
        self.recorder = recorder
        self.transcriber = transcriber
        self.brain = brain
        self.voice = voice
        self.speaker_id = speaker_id
        self.name_threshold = name_threshold
        self.journal = journal or Journal()
        self.error_lines = ERROR_LINES["dry" if humor >= DRY_FROM_HUMOR else "plain"]
        self.timings: dict[str, float] = {}  # the last answer's latency by stage, in seconds
        self._identifying = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speaker-id")
        self._thinking = threading.Lock()  # drafts take turns
        # Its own pool, so making phrases never delays speaker ID, which an answer waits on.
        self._phrasing = ThreadPoolExecutor(max_workers=1, thread_name_prefix="phrases")
        self._phrases: dict[str, Future[list[bytes]]] = {}
        self._phrase_dir = phrases  # the short lines' audio for this voice, so they survive an offline restart
        self.reminder_tools = reminders
        self.reminders = reminders.reminders if reminders else None
        self.ack_window_s = ack_window_s
        self._clock = Clock(self.reminders, on_read=self._prepare_reminders) if reminders else None
        self._just_said: list[int] = []  # reminders just said, waiting for an acknowledgement
        self._last_who: str | None = None  # whose request was answered last, by voice

    def run_forever(self, idle_message: str, follow_up_s: float = 0.0, greet_after_s: float = 0.0) -> None:
        self.prepare_phrases()
        if self._clock:
            self._clock.start()
        while True:
            print(f"\n{idle_message}")
            woke = self.trigger.wait(self.mic, due=self._clock.is_due if self._clock else None)
            if woke == DUE:
                self.say_reminders(follow_up_s)
                continue
            if woke == ASK:
                self.ask_if_called(follow_up_s)
                continue
            print("Listening...")
            self.converse(follow_up_s, greet_after_s=greet_after_s)

    def prepare_phrases(self) -> None:
        """Synthesizes the short lines ahead so they play instantly. Called again on each wake, to add greetings for
        people enrolled since and retry lines that failed."""
        names = list(self.speaker_id.voiceprints) if self.speaker_id else []
        for line in [ASK_PHRASE, greeting(None), *self.error_lines] + [greeting(n) for n in names]:
            self._phrase(line)

    def _phrase(self, text: str, keep: bool = True) -> Future[list[bytes]]:
        """The line's audio: made already, being made, or queued now (again, if it failed before). `keep`: saved
        for the next run too (the fixed lines, not a reminder's)."""
        made = self._phrases.get(text)
        if made is None or (made.done() and made.exception()):
            made = self._phrases[text] = self._saved_phrase(text) or self._phrasing.submit(
                self._make_phrase, text, keep
            )
        return made

    def _phrase_path(self, text: str) -> Path | None:
        return self._phrase_dir / f"{hashlib.sha256(text.encode()).hexdigest()[:16]}.pcm" if self._phrase_dir else None

    def _saved_phrase(self, text: str) -> Future[list[bytes]] | None:
        """A line made in an earlier run, read here rather than queued behind lines still waiting on the network."""
        path = self._phrase_path(text)
        try:
            audio = path.read_bytes() if path else None
        except OSError:
            return None
        if not audio:
            return None
        saved: Future[list[bytes]] = Future()
        saved.set_result([audio])
        return saved

    def _make_phrase(self, text: str, keep: bool = True) -> list[bytes]:
        audio = list(self.voice.stream(text))
        if keep and (path := self._phrase_path(text)):
            try:
                atomic_write(path, b"".join(audio))
            except OSError as e:  # only a head start for the next run
                print(f"(couldn't save {text!r} for next time: {e!r})")
        return audio

    def say(self, text: str, keep: bool = True) -> bool:
        """Plays a short line, made ahead if possible. False if it couldn't be said."""
        try:
            audio = self._phrase(text, keep).result()
        except Exception as e:  # noqa: BLE001 - a line TARS can't say is skipped, never fatal
            print(f"(couldn't say {text!r}: {e!r})")
            return False
        with self._mic_paused():
            self.speaker.play_pcm_stream(iter(audio), self.voice.sample_rate)
        return True

    def _mic_paused(self):
        return self.mic.paused() if self.mic else nullcontext()

    def say_error(self) -> None:
        # One still being made gets a moment: it's quicker than asking again.
        wait([f for line in self.error_lines if (f := self._phrases.get(line))], ERROR_LINE_WAIT_S, FIRST_COMPLETED)
        ready = [
            line
            for line in self.error_lines
            if (made := self._phrases.get(line)) and made.done() and not made.exception()
        ]
        if not ready:  # the voice was never reachable, so there's nothing to say it with
            print("(couldn't say what went wrong: the voice isn't reachable either)")
            return
        line = random.choice(ready)
        print(f"Bot:  {line}")
        self.say(line)

    def _prepare_reminders(self, active: list[dict]) -> None:
        """Makes each active reminder's line ahead, so it plays at once when it's due."""
        for r in active:
            self._phrase(reminder_line(r) if r["due"] is not None else held_line(r), keep=False)

    def say_reminders(self, follow_up_s: float = 0.0) -> None:
        """Says what's due, after a chime, and listens a moment for "got it" if any of it waits for that."""
        try:
            due = self.reminders.due()
        except Exception as e:  # noqa: BLE001 - try again on the clock's next read
            print(f"(couldn't read the reminders: {e!r})")
            due = []
        if not due:
            if self._clock:
                self._clock.read()
            return
        with self._mic_paused():
            self.speaker.play_pcm_stream(iter([chime(self.voice.sample_rate)]), self.voice.sample_rate)
        for r in due:
            for text in filter(None, [reminder_line(r), late(r)]):
                print(f"Bot:  {text}  (reminder {r['id']})")
                if self.say(text, keep=False):
                    self.journal.said(text)
            self.reminders.said(r["id"])
        if self._clock:
            self._clock.read()
        self._just_said = [r["id"] for r in due if r["needs_ack"]]
        try:
            heard = self.listen(start_timeout_s=self.ack_window_s, tag=REMINDER_TAG) if self._just_said else None
            if heard is None:
                self.journal.nobody_spoke()
                return
            self.converse(follow_up_s, first=heard)
        finally:
            self._just_said = []

    def _ack(self, ids: list[int], who: str | None) -> None:
        """A bare <ack> is for the reminders just said, or else the one said last."""
        try:
            ids = ids or self._just_said or [r["id"] for r in self.reminders.waiting()[:1]]
            for i in ids:
                if self.reminders.ack(i, who, VOICE):
                    print(f"(reminder {i} acknowledged by {who or 'an unknown voice'})")
        except Exception as e:  # noqa: BLE001 - the reply was said; only the record of it failed
            print(f"(couldn't mark the reminder acknowledged: {e!r})")
        if self._clock:
            self._clock.read()

    def wake_speaker(self) -> str | None:
        if not self.speaker_id or (audio := self.trigger.last_audio) is None:
            return None
        return self.speaker_id.identify(audio.astype("int16").tobytes(), threshold=self.name_threshold)

    def greet(self) -> None:
        text = greeting(self.wake_speaker())
        print(f"Bot:  {text}")
        if self.say(text):
            self.journal.said(text)

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
        self, start_timeout_s: float | None = None, follow_up: bool = False, tag: str | None = None
    ) -> Utterance | None:
        """Records one utterance, transcribing it as it comes, and starts answering at each pause in case it's the
        end."""
        session = self.transcriber.session()
        draft: Draft[Answer] | None = None

        def paused(pcm: bytes) -> None:
            nonlocal draft
            draft = self._draft(pcm, session, follow_up, tag)

        def resumed() -> None:
            nonlocal draft
            if draft:
                draft.cancel()
                # A draft still waiting on the brain (a follow-up it may skip) lets go of the turn sooner. Nothing
                # else is being written now: the last reply was over before listening began.
                self.brain.interrupt()
                draft = None

        try:
            pcm = self.recorder.record(
                self.mic,
                start_timeout_s=start_timeout_s,
                on_audio=session.feed,
                on_pause=paused,
                on_resume=resumed,
                turn_state=session.turn_state,
            )
        except BaseException:
            # Every draft must be settled, or the next one would wait behind it.
            resumed()
            session.close()
            raise
        if pcm is None:
            resumed()
            session.close()
            return None
        session.finish()
        return Utterance(
            pcm,
            session,
            draft or self._draft(pcm, session, follow_up, tag),
            self.recorder.trailing_silence_s,
            follow_up,
        )

    def converse(self, follow_up_s: float, first: Utterance | None = None, greet_after_s: float = 0.0) -> None:
        """Answer one request, then keep listening for follow-ups (no wake word) until nobody speaks.

        `first` is an already-recorded request (a reply to "Did you call me?"). With `greet_after_s`, a pause that
        long after the wake word gets a "Yes, <name>?" before TARS keeps waiting.
        """
        # While they're still talking, get the reply's connections ready.
        threading.Thread(target=self.brain.warm, daemon=True, name="brain-warm").start()
        self.voice.warm()
        self.prepare_phrases()
        try:
            self._converse(follow_up_s, first, greet_after_s)
        finally:
            self._just_said = []
            self.journal.end_conversation()

    def _converse(self, follow_up_s: float, first: Utterance | None, greet_after_s: float) -> None:
        heard = first
        if heard is None and greet_after_s:
            heard = self.listen(start_timeout_s=greet_after_s)
            if heard is None:
                self.greet()
        if heard is None:
            heard = self.listen()
        if heard is None:
            print("(didn't hear anything)")
            self.journal.nobody_spoke()
            return
        while True:
            try:
                answered = self.handle(heard)
            except AudioDeviceError:
                raise
            except Exception as e:  # noqa: BLE001 - one failed request shouldn't take the assistant down
                print(f"Error: {e!r}")
                if not isinstance(e, OpenAIError):  # a service failing (Cerebras's too) is no bug to trace
                    traceback.print_exc()
                self.say_error()
                return
            held = answered and self._say_held(self._last_who)
            if held:  # what's said next may well be about the message: listen for it, at least the ack window
                heard = self.listen(
                    start_timeout_s=max(follow_up_s, self.ack_window_s), follow_up=True, tag=REMINDER_TAG
                )
            elif not answered or follow_up_s <= 0:
                return
            else:
                print(f"(listening {follow_up_s:.0f}s for a follow-up)")
                heard = self.listen(start_timeout_s=follow_up_s, follow_up=True)
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
        for r in held:
            text = held_line(r)
            print(f"Bot:  {text}  (reminder {r['id']})")
            if self.say(text, keep=False):
                self.journal.tars_said(text)
            self.reminders.said(r["id"])
        self._just_said = [r["id"] for r in held if r["needs_ack"]]
        if self._clock and held:
            self._clock.read()
        return bool(self._just_said)

    def handle(self, heard: Utterance) -> bool:
        """Returns False if there was nothing meant for TARS to answer."""
        # Latency counts from when the speaker actually stopped, including the silence waited through.
        t_stopped_talking = time.perf_counter() - heard.silence_s
        self.timings = {}
        answer: Answer | None = None
        try:
            answer = heard.draft.take()
            name, score, embedding = answer.who
            self._last_who = name
            who = f" ({name or 'unknown'})" if self.speaker_id else ""
            print(f"You{who}:  {answer.text!r}")
            # Only the request right after the wake says whether the wake was real.
            self.journal.heard(heard.pcm, answer.text, name, score, embedding, first=not heard.follow_up)
            if not answer.text:
                heard.draft.keep()
                return False
            if answer.error:
                raise answer.error
            if answer.skipped:
                print("(not meant for me, going quiet)")
                self.journal.not_for_tars()
                heard.draft.keep()
                return False
            self.timings = {"end_of_speech": heard.silence_s, "stt": answer.stt_s}
            said = self._speak(answer, t_stopped_talking)
            heard.draft.keep()
        except BaseException as e:
            # Unheard, or only half heard: stop it, and leave the brain as if it was never asked.
            heard.draft.cancel()
            if isinstance(e, Exception) and not isinstance(e, AudioDeviceError):
                self._log_failure(e, answer)
            raise
        finally:
            heard.session.close()
        self.journal.answered(said.text, said.sent, asker=name, timings=self.timings, answered_by=said.answered_by)
        if answer.acks is not None and self.reminders:
            self._ack(answer.acks, name)
        self._change_reminders(self.brain.changes, name)
        return True

    def _change_reminders(self, changes: list, who: str | None) -> None:
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
        if self._clock:
            self._clock.read()

    def _log_failure(self, e: Exception, answer: "Answer | None") -> None:
        """Kept in the conversation, where it failed and why, with what TARS got to say of the answer."""
        text = "".join(answer.spoken).strip() if answer else ""
        if answer:
            self._measure(answer, answer.t_asked, None)
        by = self.brain.answered_by if answer and answer.t_asked else None
        self.journal.failed(text, failed_at(e), f"{type(e).__name__}: {e}", dict(self.timings), by)

    def _draft(self, pcm: bytes, session: Session, follow_up: bool, tag: str | None) -> Draft[Answer]:
        return Draft(self._thinking, lambda d: self._prepare(d, pcm, session, follow_up, tag), self._discard)

    def _prepare(self, draft: Draft, pcm: bytes, session: Session, follow_up: bool, tag: str | None) -> Answer:
        """Transcribe, identify the speaker, and set the brain and voice to work, all without a sound."""
        # Speaker ID runs while the audio is transcribed, so it adds no latency.
        identifying = self._identifying.submit(self.speaker_id.describe, pcm) if self.speaker_id else None
        t = time.perf_counter()
        try:
            text = session.transcript()
        except Exception as e:
            mark_failed_at(e, "stt")
            raise
        stt_s = time.perf_counter() - t
        answer = Answer(text, Identified(*identifying.result()) if identifying else NOBODY, stt_s)
        if not text or draft.cancelled:
            return answer
        prompt = self._prompt(text, answer.who.name, follow_up, tag)
        try:
            # A follow-up, or a reply to "Did you call me?" (maybe a "no"), may not be meant for TARS at all.
            return self._start_reply(answer, prompt, may_skip=follow_up or tag is not None)
        except Exception as e:  # noqa: BLE001 - raised again in handle(), after what was heard is kept
            answer.asked, answer.error = False, e  # already forgotten
            return answer

    def _start_reply(self, answer: Answer, prompt: str, may_skip: bool) -> Answer:
        answer.asked, answer.t_asked = True, time.perf_counter()
        try:
            pieces = self.brain.stream_reply(prompt)
            if may_skip:
                answer.skipped, pieces = split_skip(pieces)
                if answer.skipped:
                    self.brain.forget_last()  # overheard conversation shouldn't linger in the history
                    return answer
            if self.reminders:
                answer.acks, pieces = split_ack(pieces)
            pieces = _tee(pieces, answer.spoken)
        except BaseException as e:
            self.brain.forget_last()  # a question that got no answer shouldn't linger either
            mark_failed_at(e, "llm")
            raise
        answer.reply = StreamedReply(
            pieces, self.voice, lambda sentence: answer.sentences.append((time.perf_counter(), sentence))
        )
        return answer

    def _discard(self, answer: Answer) -> None:
        if answer.reply:
            self.brain.interrupt()
            answer.reply.stop()
        if answer.asked:
            self.brain.forget_last()

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
            return self._speak(answer, answer.t_asked)
        except BaseException:
            self._discard(answer)
            raise

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

        with self._mic_paused():
            self.speaker.play_pcm_stream(
                audio(), self.voice.sample_rate, on_first_audio=lambda: first_audio.append(time.perf_counter())
            )
        for _, sentence in answer.sentences[printed:]:
            print(f"Bot:  {sentence}")
        sent = list(self.brain.sent)
        for item in sent:
            print(f"(sent to the TARS page: {item.kind} '{item.title}')")
        self._measure(answer, t_start, first_audio[0] if first_audio else None)
        self._report()
        return Said("".join(answer.spoken).strip(), sent, self.brain.answered_by)

    def _measure(self, answer: Answer, t_start: float, t_sound: float | None) -> None:
        if answer.sentences:
            self.timings["llm"] = answer.sentences[0][0] - answer.t_asked
        if answer.sentences and t_sound:
            self.timings["tts"] = max(0.0, t_sound - answer.sentences[0][0])
        if t_sound:
            self.timings["total"] = t_sound - t_start

    def _report(self) -> None:
        labels = {"end_of_speech": "waited", "stt": "stt", "llm": "llm first sentence", "tts": "tts first audio"}
        parts = [f"{labels[k]} {v:.2f}s" for k, v in self.timings.items() if k in labels]
        if "total" in self.timings:
            # With the answer started during the pause, the stages overlap the wait instead of adding to it.
            parts.append(f"TOTAL to first sound {self.timings['total']:.2f}s")
        print(f"[{' | '.join(parts)}]")


def _tee(pieces: Iterator[str], into: list[str]) -> Iterator[str]:
    for piece in pieces:
        into.append(piece)
        yield piece
