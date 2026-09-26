import random
import threading
import time
import traceback
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from openai import OpenAIError

from .audio import Microphone, MicrophoneError, Speaker
from .conversations import SentItem
from .draft import Draft
from .journal import Journal
from .llm import ASKED_TAG, FOLLOW_UP_TAG, Brain, split_skip
from .recorder import UtteranceRecorder
from .speaker import SpeakerID
from .speech import StreamedReply, speak_streamed_reply
from .stt import Session, Transcriber
from .tts import Voice
from .verify import ASK
from .wake import Trigger

ASK_PHRASE = "Did you call me?"
# Said instead of a reply when a request fails (network down, a service timing out). Made at startup like the
# greetings, so they play even when the failure was the voice service itself.
ERROR_LINES = {
    "dry": [
        "That didn't work. Not my finest moment. Try again.",
        "I lost that one somewhere between here and the server. Ask me again.",
        "Something broke. I'd blame the cloud, but I'm a little bit the cloud. Try again.",
    ],
    "plain": ["Something went wrong. Please try again.", "That didn't work. Please ask again."],
}
ASK_TIMEOUT_S = 5.0  # how long to wait for an answer to it


def greeting(name: str | None) -> str:
    return f"Yes, {name.capitalize()}?" if name else "Yes?"


@dataclass
class Reply:
    text: str
    sent: list[SentItem]  # what it sent to the web UI while saying it


@dataclass
class Utterance:
    pcm: bytes
    transcript: Session  # fed while it was recorded
    draft: Draft | None = None  # an answer already being prepared, from a pause just before the end


@dataclass
class Answer:
    """A request, transcribed, and the reply being made to it, not yet heard by anyone."""

    text: str
    speaker: tuple[str | None, float | None, object]  # speaker ID's (name, score, embedding)
    stt_s: float
    t_asked: float = 0.0  # when the brain was asked
    reply: StreamedReply | None = None  # None: nothing to say (no words, or not meant for TARS)
    skipped: bool = False  # an overheard follow-up the brain chose not to answer
    spoken: list[str] = field(default_factory=list)  # the reply's text so far
    sentences: list[tuple[float, str]] = field(default_factory=list)  # when each sentence was written, and it


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
        self._background = ThreadPoolExecutor(max_workers=1)
        self._thinking = ThreadPoolExecutor(max_workers=1)  # drafts, one at a time
        self.name_threshold = name_threshold
        self.journal = journal or Journal()
        self._error_lines = ERROR_LINES["dry" if humor >= 50 else "plain"]
        self.timings: dict[str, float] = {}  # the last answer's latency by stage, in seconds
        self._phrases: dict[str, list[bytes]] = {}  # short lines synthesized once: "Did you call me?", "Yes, Alon?"
        self._phrasing = ThreadPoolExecutor(max_workers=1)  # apart from speaker ID, which a reply waits on
        self._preparing: set[str] = set()

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
        """Synthesize the short lines ahead of time so they play instantly, including a greeting for anyone who
        got a voiceprint since the last time (named in the web UI, say)."""
        names = list(self.speaker_id.voiceprints) if self.speaker_id else []
        for line in [ASK_PHRASE, greeting(None), *self._error_lines] + [greeting(n) for n in names]:
            if line not in self._phrases and line not in self._preparing:
                self._preparing.add(line)
                self._phrasing.submit(self._phrase_audio, line)

    def _phrase_audio(self, text: str) -> list[bytes]:
        if text not in self._phrases:
            self._phrases[text] = list(self.voice.stream(text))
        return self._phrases[text]

    def say_error(self) -> None:
        ready = [line for line in self._error_lines if line in self._phrases]
        if not ready:  # the voice was never reachable: the tone is all there is
            with self.mic.paused(tail_s=0.05):
                self.speaker.error_tone()
            return
        line = random.choice(ready)
        print(f"Bot:  {line}")
        self.say(line)

    def say(self, text: str) -> None:
        with self.mic.paused():
            self.speaker.play_pcm_stream(iter(self._phrase_audio(text)), self.voice.sample_rate)

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
        self.journal.said(text)
        self.say(text)

    def ask_if_called(self, follow_up_s: float) -> None:
        """It sounded almost like our name ("hey cars"?): ask, and only carry on if someone answers."""
        print(f"Bot:  {ASK_PHRASE}  (not sure I heard my name)")
        self.journal.said(ASK_PHRASE)
        self.say(ASK_PHRASE)
        heard = self.listen(start_timeout_s=ASK_TIMEOUT_S, tag=ASKED_TAG)
        if heard is None:
            print("(no answer, going back to sleep)")
            self.journal.nobody_spoke()
            return
        self.converse(follow_up_s, first=(heard, ASKED_TAG))

    def listen(
        self, start_timeout_s: float | None = None, follow_up: bool = False, tag: str | None = None
    ) -> Utterance | None:
        """Record one utterance, transcribing it as it comes (a streaming service connects before anyone speaks),
        and start answering at each pause, in case it's the end."""
        session = self.transcriber.session()
        drafts: list[Draft] = []

        def paused(pcm: bytes, silence_s: float) -> None:
            drafts.append(self._draft(pcm, session, follow_up, tag))

        def resumed() -> None:
            while drafts:
                drafts.pop().cancel()

        try:
            pcm = self.recorder.record(
                self.mic, start_timeout_s=start_timeout_s, on_audio=session.feed, on_pause=paused, on_resume=resumed
            )
        except BaseException:
            # Every draft must be settled, or the next one would wait behind it forever.
            resumed()
            raise
        if pcm is None:
            resumed()
            session.cancel()
            return None
        return Utterance(pcm, session, drafts[-1] if drafts else None)

    def converse(
        self, follow_up_s: float, first: tuple[Utterance, str] | None = None, greet_after_s: float = 0.0
    ) -> None:
        """Answer one request, then keep listening for follow-ups (no wake word) until nobody speaks.

        `first` is an already-recorded request and the tag to send it with (a reply to "Did you call me?").
        With `greet_after_s`, a pause that long after the wake word gets a "Yes, <name>?" before we keep waiting.
        """
        # While they're still talking, get the reply's connections ready.
        threading.Thread(target=self.brain.warm, daemon=True).start()
        self.prepare_phrases()
        try:
            self._converse(follow_up_s, first, greet_after_s)
        finally:
            self.journal.close()

    def _converse(self, follow_up_s: float, first: tuple[Utterance, str] | None, greet_after_s: float) -> None:
        if first:
            heard, tag = first
        else:
            tag = None
            heard = self.listen(start_timeout_s=greet_after_s) if greet_after_s else None
            if heard is None:
                if greet_after_s:
                    self.greet()
                heard = self.listen()
        if heard is None:
            print("(didn't hear anything)")
            self.journal.nobody_spoke()
            return
        follow_up = False
        while True:
            try:
                answered = self.handle(heard, follow_up, tag)
                tag = None
            except MicrophoneError:
                raise  # Not recoverable here; let the process exit so a supervisor can restart it.
            except Exception as e:
                # One failed request (network down, API error, a bug) shouldn't take the assistant down.
                what = "OpenAI error" if isinstance(e, OpenAIError) else "Error"
                print(f"{what}: {e!r}")
                if not isinstance(e, OpenAIError):
                    traceback.print_exc()
                self.say_error()
                return
            if not answered or follow_up_s <= 0:
                return
            follow_up = True
            # A softer, higher tone than the wake chime: "still listening".
            with self.mic.paused(tail_s=0.05):
                self.speaker.chime(freq=1320.0, duration_s=0.06, volume=0.12)
            print(f"(listening {follow_up_s:.0f}s for a follow-up)")
            heard = self.listen(start_timeout_s=follow_up_s, follow_up=True)
            if heard is None:
                return

    def handle(self, heard: Utterance, follow_up: bool = False, tag: str | None = None) -> bool:
        """Answer one request. Returns False if there was nothing (meant for us) to answer."""
        # Latency is measured from the moment you actually stopped talking,
        # including the silence we waited through to be sure you were done.
        silence_s = self.recorder.trailing_silence_s
        t_stopped_talking = time.perf_counter() - silence_s
        draft = heard.draft or self._draft(heard.pcm, heard.transcript, follow_up, tag)
        try:
            answer = draft.take()
            name, score, embedding = answer.speaker
            who = f" ({name or 'unknown'})" if self.speaker_id else ""
            print(f"You{who}:  {answer.text!r}")
            # Only the request right after the wake says whether the wake was real.
            turn = self.journal.heard(heard.pcm, answer.text, name, score, embedding, first=not follow_up)
            if not answer.text:
                return False
            if answer.skipped:
                print("(not meant for me, going quiet)")
                self.journal.not_for_tars(turn)
                return False
            self.timings = {"end_of_speech": silence_s, "stt": answer.stt_s}
            reply = self._speak(
                answer, t_stopped_talking, [f"end-of-speech wait {silence_s:.2f}s", f"stt {answer.stt_s:.2f}s"]
            )
            self.journal.answered(turn, reply.text, reply.sent, asker=name)
            return True
        finally:
            draft.done()
            heard.transcript.cancel()

    def _draft(self, pcm: bytes, transcript: Session, follow_up: bool, tag: str | None) -> Draft[Answer]:
        return Draft(self._thinking, lambda d: self._prepare(d, pcm, transcript, follow_up, tag), self._discard)

    def _prepare(self, draft: Draft, pcm: bytes, transcript: Session, follow_up: bool, tag: str | None) -> Answer:
        """Transcribe, identify the speaker, and set the brain and voice to work, all without a sound."""
        # Identify the speaker while the audio is being transcribed, so it adds no latency.
        who = self._background.submit(self.speaker_id.describe, pcm) if self.speaker_id else None
        t = time.perf_counter()
        text = transcript.peek()
        answer = Answer(text, who.result() if who else (None, None, None), time.perf_counter() - t)
        if not text or draft.cancelled:
            return answer
        answer.t_asked = time.perf_counter()
        pieces = _tee(self.brain.stream_reply(self._prompt(text, answer.speaker[0], follow_up, tag)), answer.spoken)
        # A follow-up, or a reply to "Did you call me?" (maybe a "no"), may not be meant for TARS at all.
        if follow_up or tag:
            answer.skipped, pieces = split_skip(pieces)
            if answer.skipped:
                self.brain.forget_last()  # overheard conversation shouldn't linger in the history
                return answer
        answer.reply = speak_streamed_reply(
            pieces, self.voice, lambda sentence: answer.sentences.append((time.perf_counter(), sentence))
        )
        return answer

    def _discard(self, answer: Answer) -> None:
        if answer.reply:
            answer.reply.cancel()
        if answer.t_asked and not answer.skipped:
            self.brain.forget_last()

    def _prompt(self, text: str, name: str | None, follow_up: bool, tag: str | None) -> str:
        if self.speaker_id:
            text = f"[Speaker: {name or 'unknown'}] {text}"
        if follow_up:
            text = f"{FOLLOW_UP_TAG} {text}"
        if tag:
            text = f"{tag} {text}"
        return text

    def answer(self, text: str, follow_up: bool = False) -> Reply | None:
        """Answer typed text (--text). Returns None if the model decided an overheard follow-up wasn't meant for it."""
        answer = Answer(text, (None, None, None), 0.0, t_asked=time.perf_counter())
        pieces = _tee(self.brain.stream_reply(text), answer.spoken)
        if follow_up:
            skipped, pieces = split_skip(pieces)
            if skipped:
                self.brain.forget_last()
                return None
        answer.reply = speak_streamed_reply(
            pieces, self.voice, lambda sentence: answer.sentences.append((time.perf_counter(), sentence))
        )
        self.timings = {}
        return self._speak(answer, answer.t_asked, [])

    def _speak(self, answer: Answer, t_start: float, timings: list[str]) -> Reply:
        marks: dict[str, float] = {}
        printed = 0

        def started() -> None:
            marks.setdefault("first_audio", time.perf_counter())

        def audio():
            nonlocal printed
            for chunk in answer.reply:
                while printed < len(answer.sentences):
                    print(f"Bot:  {answer.sentences[printed][1]}")
                    printed += 1
                yield chunk

        with self.mic.paused():
            self.speaker.play_pcm_stream(audio(), self.voice.sample_rate, on_first_audio=started)
        for _, sentence in answer.sentences[printed:]:
            print(f"Bot:  {sentence}")
        sent = list(self.brain.sent)
        for item in sent:
            print(f"(sent to the TARS page: {item.kind} '{item.title}')")

        if answer.sentences:
            self.timings["llm"] = answer.sentences[0][0] - answer.t_asked
            timings.append(f"llm first sentence {self.timings['llm']:.2f}s")
        if "first_audio" in marks:
            if answer.sentences:
                self.timings["tts"] = max(0.0, marks["first_audio"] - answer.sentences[0][0])
                timings.append(f"tts first audio {self.timings['tts']:.2f}s")
            self.timings["total"] = marks["first_audio"] - t_start
            timings.append(f"TOTAL to first sound {self.timings['total']:.2f}s")
        print(f"[{' | '.join(timings)}]")
        return Reply("".join(answer.spoken).strip(), sent)


def _tee(pieces: Iterator[str], into: list[str]) -> Iterator[str]:
    for piece in pieces:
        into.append(piece)
        yield piece
