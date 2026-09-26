"""How fast TARS answers: from the moment you stop talking to its first sound, stage by stage.

    uv run python tools/latency_bench.py                               # the pipeline config.toml picks
    uv run python tools/latency_bench.py --set recorder.end_silence_s=0.3 --set tts.model=gpt-4o-mini-tts

The questions are spoken once (by Deepgram's Aura, or OpenAI) and kept in voice_data/bench/questions/ (gitignored). Each is fed to
the assistant at real-time pace, like a mic would, and the reply goes to a silent speaker that notes when the first
sound would have played (after the same prebuffer the real speaker waits for). Costs a few cents per run.
"""

import argparse
import dataclasses
import statistics
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf
from dotenv import dotenv_values
from scipy.signal import resample_poly

from voice_assistant.__main__ import make_openai_client, make_pipeline
from voice_assistant.assistant import Assistant
from voice_assistant.audio import BLOCK_SAMPLES, BLOCK_SECONDS, SAMPLE_RATE
from voice_assistant.config import RecorderConfig, load_config
from voice_assistant.recorder import UtteranceRecorder, make_recorder
from voice_assistant.vad import SileroVAD

REPO = Path(__file__).parents[1]
QUESTIONS = [
    "What's the capital of Australia?",
    "Tell me a fun fact about octopuses.",
    "How many minutes are there in a day?",
    "What rhymes with orange?",
    "Give me a good name for a grumpy cat.",
    "Explain gravity in one sentence.",
    "What should I cook tonight with eggs and spinach?",
    "Say something that would make a robot laugh.",
]
LEAD_S, TAIL_S = 0.4, 5.0  # room noise before the question, and after it


def speak(root: Path, text: str) -> np.ndarray:
    """One question, 16 kHz. Deepgram's Aura voices sound like a person to the speech detector; OpenAI's are so tonal
    that it loses the middle of some sentences."""
    env = dotenv_values(root / ".env")
    if env.get("DEEPGRAM_API_KEY"):
        r = httpx.post(
            "https://api.deepgram.com/v1/speak?model=aura-2-thalia-en&encoding=linear16&sample_rate=16000"
            "&container=none",
            headers={"Authorization": f"Token {env['DEEPGRAM_API_KEY']}"},
            json={"text": text},
            timeout=30,
        )
        r.raise_for_status()
        return np.frombuffer(r.content, np.int16)
    client = make_openai_client(root / ".env")
    with client.audio.speech.with_streaming_response.create(
        model="tts-1", voice="alloy", input=text, response_format="pcm"
    ) as response:
        pcm = response.read()
    audio = resample_poly(np.frombuffer(pcm, np.int16).astype(np.float32), 2, 3)  # 24 kHz -> 16 kHz
    return np.clip(audio, -32768, 32767).astype(np.int16)


def questions(root: Path) -> list[np.ndarray]:
    folder = root / "voice_data/bench/questions"
    folder.mkdir(parents=True, exist_ok=True)
    clips = []
    for i, text in enumerate(QUESTIONS):
        path = folder / f"{i:02d}.wav"
        if not path.exists():
            sf.write(path, speak(root, text), SAMPLE_RATE)
        clips.append(sf.read(path, dtype="int16")[0])
    return clips


def speech_end(clip: np.ndarray) -> int:
    """The sample where the voice stops (the clip's own trailing silence doesn't count)."""
    frames = clip[: len(clip) // 320 * 320].reshape(-1, 320).astype(np.float32)
    loud = np.flatnonzero(np.sqrt((frames**2).mean(axis=1)) > 300)
    return int((loud[-1] + 1) * 320) if len(loud) else len(clip)


class RoomTone:
    """Real room tone: the quiet ends of the owner's recordings (after their last word), spliced in random order.
    Plain white noise won't do: after loud speech, the speech detector keeps calling it speech for seconds."""

    def __init__(self, root: Path, rng: np.random.Generator):
        vad = UtteranceRecorder(RecorderConfig(), SileroVAD(root / RecorderConfig().vad_model))
        tails = []
        for f in sorted((root / "voice_data").glob("*/*/speech/*.wav")):
            pcm = sf.read(f, dtype="int16")[0]
            blocks = pcm[: len(pcm) // BLOCK_SAMPLES * BLOCK_SAMPLES].reshape(-1, BLOCK_SAMPLES)
            speech = [i for i, b in enumerate(blocks) if vad.is_speech(b)]
            quiet = blocks[(speech[-1] + 3 if speech else 0) :]  # a little past the last word
            if len(quiet) >= 5:
                tails.append(np.concatenate(quiet))
        if not tails:
            raise SystemExit("needs recordings in voice_data/<person>/<mic>/speech/ (voice-assistant --record-voice)")
        self._tails, self._rng = tails, rng

    def __call__(self, n: int) -> np.ndarray:
        """n samples, the pieces crossfaded: a hard cut clicks, and a click sounds like speech starting."""
        fade = int(0.05 * SAMPLE_RATE)
        ramp = np.linspace(0, 1, fade)
        out = self._tails[self._rng.integers(len(self._tails))].astype(np.float32)
        while len(out) < n:
            nxt = self._tails[self._rng.integers(len(self._tails))].astype(np.float32)
            out = np.concatenate([out[:-fade], out[-fade:] * (1 - ramp) + nxt[:fade] * ramp, nxt[fade:]])
        return out[:n].astype(np.int16)


class RealtimeMic:
    """Hands out 80 ms blocks no faster than a real mic would, then room noise forever."""

    def __init__(self, pcm: np.ndarray, room: RoomTone):
        self._blocks = pcm[: len(pcm) // BLOCK_SAMPLES * BLOCK_SAMPLES].reshape(-1, BLOCK_SAMPLES)
        self._room = room
        self._i = 0
        self.t0 = time.perf_counter()

    def read(self) -> np.ndarray:
        wait = self.t0 + (self._i + 1) * BLOCK_SECONDS - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        block = self._blocks[self._i] if self._i < len(self._blocks) else self._room(BLOCK_SAMPLES)
        self._i += 1
        return block

    def clear(self) -> None:
        pass

    @contextmanager
    def paused(self, tail_s: float = 0.3):
        yield


class SilentSpeaker:
    """Takes the reply's audio as fast as it comes and notes when a real speaker would have started playing."""

    def __init__(self, sample_rate: int, prebuffer_s: float):
        self.sample_rate = sample_rate
        self._prebuffer = int(prebuffer_s * sample_rate) * 2
        self.first_sound: float | None = None

    def play_pcm_stream(self, chunks, sample_rate, on_first_audio=None) -> None:
        got = 0
        for chunk in chunks:
            got += len(chunk)
            if got >= self._prebuffer and self.first_sound is None:
                self._started(on_first_audio)
        if got and self.first_sound is None:
            self._started(on_first_audio)

    def _started(self, on_first_audio) -> None:
        self.first_sound = time.perf_counter()
        if on_first_audio:
            on_first_audio()

    def chime(self, *args, **kwargs) -> None:
        pass

    def error_tone(self) -> None:
        print("(error tone)")


def apply(cfg, assignment: str) -> None:
    """--set section.key=value, converted to the type the setting already has."""
    key, value = assignment.split("=", 1)
    section, name = key.split(".")
    part = getattr(cfg, section)
    old = getattr(part, name)
    new = (
        value
        if isinstance(old, str)
        else type(old)(value.lower() in ("1", "true", "yes") if isinstance(old, bool) else value)
    )
    setattr(cfg, section, dataclasses.replace(part, **{name: new}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=REPO / "config.toml")
    parser.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE")
    parser.add_argument("--turns", type=int, default=len(QUESTIONS))
    args = parser.parse_args()
    root = args.config.resolve().parent
    cfg = load_config(args.config)
    for assignment in args.set:
        apply(cfg, assignment)

    clips = questions(root)[: args.turns]
    transcriber, brain, voice = make_pipeline(cfg, root, typed=True)
    speaker = SilentSpeaker(voice.sample_rate, cfg.audio.playback_prebuffer_s)
    room = RoomTone(root, np.random.default_rng(0))
    assistant = Assistant(None, speaker, None, make_recorder(cfg, root), transcriber, brain, voice)
    rows = []
    for clip in clips:
        lead = room(int(LEAD_S * SAMPLE_RATE))
        mic = assistant.mic = RealtimeMic(np.concatenate([lead, clip, room(int(TAIL_S * SAMPLE_RATE))]), room)
        stopped = mic.t0 + (len(lead) + speech_end(clip)) / SAMPLE_RATE
        speaker.first_sound, assistant.timings = None, {}
        assistant.converse(follow_up_s=0)
        if speaker.first_sound is None:
            print("(no answer)")
            continue
        rows.append({**assistant.timings, "measured": speaker.first_sound - stopped})
        time.sleep(0.5)

    stages = ["end_of_speech", "stt", "llm", "tts", "measured"]
    print("\nmedian over", len(rows), "turns, seconds:")
    for stage in stages:
        values = [r[stage] for r in rows if stage in r]
        if values:
            print(f"  {stage:14} {statistics.median(values):5.2f}   (min {min(values):.2f}, max {max(values):.2f})")
    print("  (measured = when you stopped talking -> first sound, from the audio itself)")


if __name__ == "__main__":
    main()
