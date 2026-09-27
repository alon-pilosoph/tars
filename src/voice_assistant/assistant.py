import random
import threading
import time
import traceback
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np
from openai import OpenAIError

from .audio import Microphone, MicrophoneError, Speaker
from .conversations import SentItem
from .draft import Draft
from .journal import Journal
from .llm import ASKED_TAG, FOLLOW_UP_TAG, Brain, split_skip
from .recorder import UtteranceRecorder
from .speaker import SpeakerID
from .speech import StreamedReply
from .stt import Session, Transcriber
from .tts import Voice
from .verify import ASK
from .wake import Trigger

ASK_PHRASE = "Did you call me?"
ASK_TIMEOUT_S = 5.0  # how long to wait for an answer to it
# Said instead of a reply when a request fails (network down, a service timing out). Made ahead like the greetings,
# so they play even when the failure was the voice service itself.
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


class Voiceprint(NamedTuple):
    name: str | None
    score: float | None
    embedding: np.ndarray | None


NOBODY = Voiceprint(None, None, None)


@dataclass
class Said:
    text: str
    sent: list[SentItem]  # what it sent to the web UI while saying it


@dataclass
class Utterance:
    pcm: bytes
    transcript: Session  # fed while it was recorded
    draft: "Draft[Answer]"
    silence_s: float  # how long ago they stopped talking when the recording ended
    follow_up: bool = False
    tag: str | None = None


@dataclass
class Answer:
    """A request, transcribed, and the reply being made to it, not yet heard by anyone."""

    text: str
    voice: Voiceprint
    stt_s: float
    asked: bool = False  # the brain was asked, so a discarded answer must be forgotten
    t_asked: float = 0.0
    reply: StreamedReply | None = None  # None: nothing to say (no words, or not meant for TARS)
    skipped: bool = False  # an overheard follow-up the brain chose not to answer
    spoken: list[str] = field(default_factory=list)
    sentences: list[tuple[float, str]] = field(default_factory=list)  # (when it was written, the sentence)


class Assistant:
    def __init__(
        self,
        mic: Microphone,
        speaker: Speaker,
        trigger: Trigger,
        recorder: UtteranceRecorder,
        transcriber: Transcriber,
        brain: Brain,
        voice: Voice,
        speaker_id: SpeakerID | None = None,
        name_threshold: float = 0.25,
        journal: Journal | None = None,
        humor: int = 75,
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
        self._identifying = ThreadPoolExecutor(max_workers=1)
        self._thinking = threading.Lock()  # drafts take turns
        # Its own pool, so making phrases never delays speaker ID, which an answer waits on.
        self._phrasing = ThreadPoolExecutor(max_workers=1)
        self._phrases: dict[str, Future[list[bytes]]] = {}

    def run_forever(self, idle_message: str, follow_up_s: float = 0.0, greet_after_s: float = 0.0) -> None:
        self.prepare_phrases()
        while True:
            print(f"\n{idle_message}")
            if self.trigger.wait(self.mic) == ASK:
                self.ask_if_called(follow_up_s)
                continue
            with self.mic.paused(tail_s=0.05):
                self.speaker.chime()
            print("Listening...")
            self.converse(follow_up_s, greet_after_s=greet_after_s)

    def prepare_phrases(self) -> None:
        """Synthesize the short lines ahead of time so they play instantly: a greeting for anyone who got a
        voiceprint since (named in the web UI, say), and again any line that failed (the network was down)."""
        names = list(self.speaker_id.voiceprints) if self.speaker_id else []
        for line in [ASK_PHRASE, greeting(None), *self.error_lines] + [greeting(n) for n in names]:
            made = self._phrases.get(line)
            if made is None or (made.done() and made.exception()):
                self._phrases[line] = self._phrasing.submit(lambda text=line: list(self.voice.stream(text)))

    def say(self, text: str) -> bool:
        """Play a short line, from the ones made ahead if it's there. False if it couldn't be said."""
        if text not in self._phrases:
            self._phrases[text] = self._phrasing.submit(lambda: list(self.voice.stream(text)))
        try:
            audio = self._phrases[text].result()
        except Exception as e:  # noqa: BLE001 - a line TARS can't say is skipped, never fatal
            print(f"(couldn't say {text!r}: {e!r})")
            return False
        with self.mic.paused():
            self.speaker.play_pcm_stream(iter(audio), self.voice.sample_rate)
        return True

    def say_error(self) -> None:
        # One still being made gets a moment: it's quicker than the tone and a retry.
        wait([f for line in self.error_lines if (f := self._phrases.get(line))], ERROR_LINE_WAIT_S, FIRST_COMPLETED)
        ready = [
            line
            for line in self.error_lines
            if (made := self._phrases.get(line)) and made.done() and not made.exception()
        ]
        if not ready:  # the voice was never reachable: the tone is all there is
            with self.mic.paused(tail_s=0.05):
                self.speaker.error_tone()
            return
        line = random.choice(ready)
        print(f"Bot:  {line}")
        self.say(line)

    def wake_speaker(self) -> str | None:
        """Who said the wake word, if we can tell from that short clip."""
        audio = getattr(self.trigger, "last_audio", None)
        if not self.speaker_id or audio is None:
            return None
        return self.speaker_id.identify(audio.astype("int16").tobytes(), threshold=self.name_threshold)

    def greet(self) -> None:
        """Just "hey TARS" and a pause: answer like a person would, by name when we're sure who it is."""
        text = greeting(self.wake_speaker())
        print(f"Bot:  {text}")
        if self.say(text):
            self.journal.said(text)

    def ask_if_called(self, follow_up_s: float) -> None:
        """It sounded almost like our name ("hey cars"?): ask, and only carry on if someone answers."""
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
        """Record one utterance, transcribing it as it comes (a streaming service connects before anyone speaks),
        and start answering at each pause, in case it's the end."""
        session = self.transcriber.session()
        draft: Draft[Answer] | None = None

        def paused(pcm: bytes) -> None:
            nonlocal draft
            draft = self._draft(pcm, session, follow_up, tag)

        def resumed() -> None:
            nonlocal draft
            if draft:
                draft.cancel()
                draft = None

        try:
            pcm = self.recorder.record(
                self.mic,
                start_timeout_s=start_timeout_s,
                on_audio=session.feed,
                on_pause=paused,
                on_resume=resumed,
                turn_state=getattr(session, "turn_state", None),
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
        if finish := getattr(session, "finish", None):
            finish()  # a service that follows the turn stops listening, and sends its last words
        return Utterance(
            pcm,
            session,
            draft or self._draft(pcm, session, follow_up, tag),
            self.recorder.trailing_silence_s,
            follow_up,
            tag,
        )

    def converse(self, follow_up_s: float, first: Utterance | None = None, greet_after_s: float = 0.0) -> None:
        """Answer one request, then keep listening for follow-ups (no wake word) until nobody speaks.

        `first` is an already-recorded request (a reply to "Did you call me?"). With `greet_after_s`, a pause that
        long after the wake word gets a "Yes, <name>?" before we keep waiting.
        """
        # While they're still talking, get the reply's connections ready.
        threading.Thread(target=self.brain.warm, daemon=True).start()
        self.voice.warm()
        self.prepare_phrases()
        try:
            self._converse(follow_up_s, first, greet_after_s)
        finally:
            self.journal.close()

    def _converse(self, follow_up_s: float, first: Utterance | None, greet_after_s: float) -> None:
        heard = first
        if heard is None:
            heard = self.listen(start_timeout_s=greet_after_s) if greet_after_s else None
            if heard is None:
                if greet_after_s:
                    self.greet()
                heard = self.listen()
        if heard is None:
            print("(didn't hear anything)")
            self.journal.nobody_spoke()
            return
        while True:
            try:
                answered = self.handle(heard)
            except MicrophoneError:
                raise  # Not recoverable here; let the process exit so a supervisor can restart it.
            except Exception as e:  # noqa: BLE001 - one failed request shouldn't take the assistant down
                what = "OpenAI error" if isinstance(e, OpenAIError) else "Error"
                print(f"{what}: {e!r}")
                if not isinstance(e, OpenAIError):
                    traceback.print_exc()
                self.say_error()
                return
            if not answered or follow_up_s <= 0:
                return
            # A softer, higher tone than the wake chime: "still listening".
            with self.mic.paused(tail_s=0.05):
                self.speaker.chime(freq=1320.0, duration_s=0.06, volume=0.12)
            print(f"(listening {follow_up_s:.0f}s for a follow-up)")
            heard = self.listen(start_timeout_s=follow_up_s, follow_up=True)
            if heard is None:
                return

    def handle(self, heard: Utterance) -> bool:
        """Answer one request. Returns False if there was nothing (meant for us) to answer."""
        # Latency is measured from the moment you actually stopped talking,
        # including the silence we waited through to be sure you were done.
        t_stopped_talking = time.perf_counter() - heard.silence_s
        answer = None
        try:
            answer = heard.draft.take()
            name, score, embedding = answer.voice
            who = f" ({name or 'unknown'})" if self.speaker_id else ""
            print(f"You{who}:  {answer.text!r}")
            # Only the request right after the wake says whether the wake was real.
            turn = self.journal.heard(heard.pcm, answer.text, name, score, embedding, first=not heard.follow_up)
            if not answer.text:
                heard.draft.keep()
                return False
            if answer.skipped:
                print("(not meant for me, going quiet)")
                self.journal.not_for_tars(turn)
                heard.draft.keep()
                return False
            self.timings = {"end_of_speech": heard.silence_s, "stt": answer.stt_s}
            said = self._speak(answer, t_stopped_talking)
            heard.draft.keep()
        except BaseException:
            # Unheard, or only half heard: stop it, and leave the brain as if it was never asked.
            heard.draft.cancel()
            raise
        finally:
            heard.transcript.close()
        self.journal.answered(turn, said.text, said.sent, asker=name)
        return True

    def _draft(self, pcm: bytes, transcript: Session, follow_up: bool, tag: str | None) -> Draft[Answer]:
        return Draft(self._thinking, lambda d: self._prepare(d, pcm, transcript, follow_up, tag), self._discard)

    def _prepare(self, draft: Draft, pcm: bytes, transcript: Session, follow_up: bool, tag: str | None) -> Answer:
        """Transcribe, identify the speaker, and set the brain and voice to work, all without a sound."""
        # Identify the speaker while the audio is being transcribed, so it adds no latency.
        voice = self._identifying.submit(self.speaker_id.describe, pcm) if self.speaker_id else None
        t = time.perf_counter()
        text = transcript.transcript()
        stt_s = time.perf_counter() - t
        answer = Answer(text, Voiceprint(*voice.result()) if voice else NOBODY, stt_s)
        if not text or draft.cancelled:
            return answer
        prompt = self._prompt(text, answer.voice.name, follow_up, tag)
        # A follow-up, or a reply to "Did you call me?" (maybe a "no"), may not be meant for TARS at all.
        return self._start_reply(answer, prompt, may_skip=follow_up or tag is not None)

    def _start_reply(self, answer: Answer, prompt: str, may_skip: bool) -> Answer:
        answer.asked, answer.t_asked = True, time.perf_counter()
        try:
            pieces = _tee(self.brain.stream_reply(prompt), answer.spoken)
            if may_skip:
                answer.skipped, pieces = split_skip(pieces)
                if answer.skipped:
                    self.brain.forget_last()  # overheard conversation shouldn't linger in the history
                    return answer
        except BaseException:
            self.brain.forget_last()  # a question that got no answer shouldn't linger either
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

        with self.mic.paused():
            self.speaker.play_pcm_stream(
                audio(), self.voice.sample_rate, on_first_audio=lambda: first_audio.append(time.perf_counter())
            )
        for _, sentence in answer.sentences[printed:]:
            print(f"Bot:  {sentence}")
        sent = list(self.brain.sent)
        for item in sent:
            print(f"(sent to the TARS page: {item.kind} '{item.title}')")
        self._report(answer, t_start, first_audio[0] if first_audio else None)
        return Said("".join(answer.spoken).strip(), sent)

    def _report(self, answer: Answer, t_start: float, t_sound: float | None) -> None:
        if answer.sentences:
            self.timings["llm"] = answer.sentences[0][0] - answer.t_asked
        if answer.sentences and t_sound:
            self.timings["tts"] = max(0.0, t_sound - answer.sentences[0][0])
        if t_sound:
            self.timings["total"] = t_sound - t_start
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
