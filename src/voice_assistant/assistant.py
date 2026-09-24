import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAIError

from .audio import Microphone, MicrophoneError, Speaker
from .llm import FOLLOW_UP_TAG, Brain, split_skip
from .recorder import UtteranceRecorder
from .speaker import SpeakerID
from .speech import speak_streamed_reply
from .stt import Transcriber
from .tts import Voice
from .wake import Trigger


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

    def run_forever(self, idle_message: str, follow_up_s: float = 0.0) -> None:
        while True:
            print(f"\n{idle_message}")
            self.trigger.wait(self.mic)
            with self.mic.paused(tail_s=0.05):
                self.speaker.chime()
            print("Listening...")
            self.converse(follow_up_s)

    def converse(self, follow_up_s: float) -> None:
        """Answer one request, then keep listening for follow-ups (no wake word) until nobody speaks."""
        pcm = self.recorder.record(self.mic)
        if pcm is None:
            print("(didn't hear anything)")
            return
        follow_up = False
        while True:
            try:
                answered = self.handle(pcm, follow_up)
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

    def handle(self, pcm: bytes, follow_up: bool = False) -> bool:
        """Transcribe and answer one request. Returns False if there was nothing (meant for us) to answer."""
        # Latency is measured from the moment you actually stopped talking,
        # including the silence we waited through to be sure you were done.
        silence_s = self.recorder.trailing_silence_s
        t_stopped_talking = time.perf_counter() - silence_s

        # Identify the speaker while the audio is being transcribed, so it adds no latency.
        who = self._background.submit(self.speaker_id.identify, pcm) if self.speaker_id else None
        t_stt = time.perf_counter()
        text = self.transcriber.transcribe(pcm)
        stt_s = time.perf_counter() - t_stt
        name = who.result() if who else None
        print(f"You{f' ({name or 'unknown'})' if who else ''}:  {text!r}")
        if not text:
            return False
        if who:
            text = f"[Speaker: {name or 'unknown'}] {text}"
        if follow_up:
            text = f"{FOLLOW_UP_TAG} {text}"

        timings = [f"end-of-speech wait {silence_s:.2f}s", f"stt {stt_s:.2f}s"]
        return self.answer(text, t_stopped_talking, timings, follow_up=follow_up)

    def answer(
        self, text: str, t_start: float | None = None, timings: list[str] | None = None, follow_up: bool = False
    ) -> bool:
        """Speak the reply. Returns False if the model decided an overheard follow-up wasn't meant for it."""
        t_llm = time.perf_counter()
        t_start = t_start or t_llm
        timings = list(timings or [])
        marks: dict[str, float] = {}

        pieces = self.brain.stream_reply(text)
        if follow_up:
            skipped, pieces = split_skip(pieces)
            if skipped:
                # Overheard conversation shouldn't linger in the history.
                self.brain.forget_last()
                print("(not meant for me, going quiet)")
                return False

        def on_sentence(sentence: str) -> None:
            marks.setdefault("first_sentence", time.perf_counter())
            print(f"Bot:  {sentence}")

        with self.mic.paused():
            self.speaker.play_pcm_stream(
                speak_streamed_reply(pieces, self.voice, on_sentence),
                self.voice.sample_rate,
                on_first_audio=lambda: marks.setdefault("first_audio", time.perf_counter()),
            )

        if "first_sentence" in marks:
            timings.append(f"llm first sentence {marks['first_sentence'] - t_llm:.2f}s")
        if "first_audio" in marks:
            if "first_sentence" in marks:
                timings.append(f"tts first audio {marks['first_audio'] - marks['first_sentence']:.2f}s")
            timings.append(f"TOTAL to first sound {marks['first_audio'] - t_start:.2f}s")
        print(f"[{' | '.join(timings)}]")
        return True
