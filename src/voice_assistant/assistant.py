import time
import traceback
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from openai import OpenAIError

from .audio import Microphone, MicrophoneError, Speaker
from .conversations import SentItem
from .journal import Journal
from .llm import ASKED_TAG, FOLLOW_UP_TAG, Brain, split_skip
from .recorder import UtteranceRecorder
from .speaker import SpeakerID
from .speech import speak_streamed_reply
from .stt import Transcriber
from .tts import Voice
from .verify import ASK
from .wake import Trigger

ASK_PHRASE = "Did you call me?"
ASK_TIMEOUT_S = 5.0  # how long to wait for an answer to it


def greeting(name: str | None) -> str:
    return f"Yes, {name.capitalize()}?" if name else "Yes?"


@dataclass
class Reply:
    text: str
    sent: list[SentItem]  # what it sent to the web UI while saying it


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
        self.name_threshold = name_threshold
        self.journal = journal or Journal()
        self.timings: dict[str, float] = {}  # the last answer's latency by stage, in seconds
        self._phrases: dict[str, list[bytes]] = {}  # short lines synthesized once: "Did you call me?", "Yes, Alon?"

    def run_forever(self, idle_message: str, follow_up_s: float = 0.0, greet_after_s: float = 0.0) -> None:
        # Synthesize the short lines ahead of time so they play instantly.
        names = list(self.speaker_id.voiceprints) if self.speaker_id else []
        for line in [ASK_PHRASE, greeting(None)] + [greeting(n) for n in names]:
            self._background.submit(self._phrase_audio, line)
        while True:
            print(f"\n{idle_message}")
            if self.trigger.wait(self.mic) == ASK:
                self.ask_if_called(follow_up_s)
                continue
            with self.mic.paused(tail_s=0.05):
                self.speaker.chime()
            print("Listening...")
            self.converse(follow_up_s, greet_after_s=greet_after_s)

    def _phrase_audio(self, text: str) -> list[bytes]:
        if text not in self._phrases:
            self._phrases[text] = list(self.voice.stream(text))
        return self._phrases[text]

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
        pcm = self.recorder.record(self.mic, start_timeout_s=ASK_TIMEOUT_S)
        if pcm is None:
            print("(no answer, going back to sleep)")
            self.journal.nobody_spoke()
            return
        self.converse(follow_up_s, first=(pcm, ASKED_TAG))

    def converse(self, follow_up_s: float, first: tuple[bytes, str] | None = None, greet_after_s: float = 0.0) -> None:
        """Answer one request, then keep listening for follow-ups (no wake word) until nobody speaks.

        `first` is an already-recorded request and the tag to send it with (a reply to "Did you call me?").
        With `greet_after_s`, a pause that long after the wake word gets a "Yes, <name>?" before we keep waiting.
        """
        try:
            self._converse(follow_up_s, first, greet_after_s)
        finally:
            self.journal.close()

    def _converse(self, follow_up_s: float, first: tuple[bytes, str] | None, greet_after_s: float) -> None:
        if first:
            pcm, tag = first
        else:
            tag = None
            pcm = self.recorder.record(self.mic, start_timeout_s=greet_after_s) if greet_after_s else None
            if pcm is None:
                if greet_after_s:
                    self.greet()
                pcm = self.recorder.record(self.mic)
        if pcm is None:
            print("(didn't hear anything)")
            self.journal.nobody_spoke()
            return
        follow_up = False
        while True:
            try:
                answered = self.handle(pcm, follow_up, tag)
                tag = None
            except MicrophoneError:
                raise  # Not recoverable here; let the process exit so a supervisor can restart it.
            except Exception as e:
                # One failed request (network down, API error, a bug) shouldn't take the assistant down.
                what = "OpenAI error" if isinstance(e, OpenAIError) else "Error"
                print(f"{what}: {e!r}")
                if not isinstance(e, OpenAIError):
                    traceback.print_exc()
                with self.mic.paused(tail_s=0.05):
                    self.speaker.error_tone()
                return
            if not answered or follow_up_s <= 0:
                return
            follow_up = True
            # A softer, higher tone than the wake chime: "still listening".
            with self.mic.paused(tail_s=0.05):
                self.speaker.chime(freq=1320.0, duration_s=0.06, volume=0.12)
            print(f"(listening {follow_up_s:.0f}s for a follow-up)")
            pcm = self.recorder.record(self.mic, start_timeout_s=follow_up_s)
            if pcm is None:
                return

    def handle(self, pcm: bytes, follow_up: bool = False, tag: str | None = None) -> bool:
        """Transcribe and answer one request. Returns False if there was nothing (meant for us) to answer."""
        # Latency is measured from the moment you actually stopped talking,
        # including the silence we waited through to be sure you were done.
        silence_s = self.recorder.trailing_silence_s
        t_stopped_talking = time.perf_counter() - silence_s

        # Identify the speaker while the audio is being transcribed, so it adds no latency.
        who = self._background.submit(self.speaker_id.describe, pcm) if self.speaker_id else None
        t_stt = time.perf_counter()
        text = self.transcriber.transcribe(pcm)
        stt_s = time.perf_counter() - t_stt
        name, score, embedding = who.result() if who else (None, None, None)
        voice = f" ({name or 'unknown'})" if who else ""
        print(f"You{voice}:  {text!r}")
        # Only the request right after the wake says whether the wake was real.
        turn = self.journal.heard(pcm, text, name, score, embedding, first=not follow_up)
        if not text:
            return False
        if who:
            text = f"[Speaker: {name or 'unknown'}] {text}"
        if follow_up:
            text = f"{FOLLOW_UP_TAG} {text}"
        if tag:
            text = f"{tag} {text}"

        self.timings = {"end_of_speech": silence_s, "stt": stt_s}
        timings = [f"end-of-speech wait {silence_s:.2f}s", f"stt {stt_s:.2f}s"]
        # A reply to "Did you call me?" may be a "no", so it can be skipped like an overheard follow-up.
        reply = self.answer(text, t_stopped_talking, timings, follow_up=follow_up or tag is not None)
        if reply is None:
            self.journal.not_for_tars(turn)
        else:
            self.journal.answered(turn, reply.text, reply.sent, asker=name)
        return reply is not None

    def answer(
        self, text: str, t_start: float | None = None, timings: list[str] | None = None, follow_up: bool = False
    ) -> Reply | None:
        """Speak the reply. Returns None if the model decided an overheard follow-up wasn't meant for it."""
        t_llm = time.perf_counter()
        t_start = t_start or t_llm
        timings = list(timings or [])
        marks: dict[str, float] = {}

        spoken: list[str] = []
        pieces = _tee(self.brain.stream_reply(text), spoken)
        if follow_up:
            skipped, pieces = split_skip(pieces)
            if skipped:
                # Overheard conversation shouldn't linger in the history.
                self.brain.forget_last()
                print("(not meant for me, going quiet)")
                return None

        def on_sentence(sentence: str) -> None:
            marks.setdefault("first_sentence", time.perf_counter())
            print(f"Bot:  {sentence}")

        with self.mic.paused():
            self.speaker.play_pcm_stream(
                speak_streamed_reply(pieces, self.voice, on_sentence),
                self.voice.sample_rate,
                on_first_audio=lambda: marks.setdefault("first_audio", time.perf_counter()),
            )
        sent = list(self.brain.sent)
        for item in sent:
            print(f"(sent to the TARS page: {item.kind} '{item.title}')")

        if "first_sentence" in marks:
            self.timings["llm"] = marks["first_sentence"] - t_llm
            timings.append(f"llm first sentence {self.timings['llm']:.2f}s")
        if "first_audio" in marks:
            if "first_sentence" in marks:
                self.timings["tts"] = marks["first_audio"] - marks["first_sentence"]
                timings.append(f"tts first audio {self.timings['tts']:.2f}s")
            self.timings["total"] = marks["first_audio"] - t_start
            timings.append(f"TOTAL to first sound {self.timings['total']:.2f}s")
        print(f"[{' | '.join(timings)}]")
        return Reply("".join(spoken).strip(), sent)


def _tee(pieces: Iterator[str], into: list[str]) -> Iterator[str]:
    for piece in pieces:
        into.append(piece)
        yield piece
