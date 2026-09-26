"""Stand-ins for the audio devices and APIs, so the pipeline can be tested offline in milliseconds."""

import json
import types
from contextlib import contextmanager

import numpy as np
import pytest
from fastapi.testclient import TestClient

import voice_assistant.assistant as assistant_module
from voice_assistant.assistant import Assistant
from voice_assistant.audio import BLOCK_SAMPLES, SAMPLE_RATE
from voice_assistant.conversations import ConversationLog
from voice_assistant.events import EventLog
from voice_assistant.webui import create_app

AUDIO = np.zeros(SAMPLE_RATE, np.int16)  # a second of silence: stands in for any recording


class FakeMic:
    """Replays a list of 80 ms blocks."""

    def __init__(self, blocks=()):
        self.blocks = iter(blocks)

    def read(self):
        return next(self.blocks)

    @contextmanager
    def paused(self, tail_s=0.3):
        yield


class SilentMic:
    """`n` blocks of silence, for the trigger (which clears the queue first)."""

    def __init__(self, n):
        self.blocks = iter(np.zeros((n, BLOCK_SAMPLES), np.int16))

    def clear(self):
        pass

    def read(self):
        return next(self.blocks)


class FakeTrigger:
    """A wake-word model that fires on the given block numbers."""

    phrase, threshold = "hey tars", 0.9

    def __init__(self, fire_at):
        self.fire_at, self.n, self.resets = set(fire_at), 0, 0

    def score(self, block):
        self.n += 1
        return 1.0 if self.n in self.fire_at else 0.0

    def reset(self):
        self.resets += 1


class FakeSpeaker:
    def __init__(self):
        self.sounds = []

    def chime(self, **kw):
        self.sounds.append(("chime", kw.get("freq", 880.0)))

    def error_tone(self):
        self.sounds.append(("error", None))


QUIET_RNG, VOICED_RNG = np.random.default_rng(0), np.random.default_rng(1)


def quiet_block(rng=QUIET_RNG):
    return rng.normal(0, 30, BLOCK_SAMPLES).astype(np.int16)


def voiced_block(rng=VOICED_RNG):
    """Voice-like: many equal harmonics of 150 Hz plus breath noise, with a slow wobble.

    webrtcvad calls it speech, and like a real voice its energy is spread over many frequencies (low tonality).
    """
    t = np.arange(BLOCK_SAMPLES) / SAMPLE_RATE
    wave = sum(np.sin(2 * np.pi * 150 * k * t + k) for k in range(1, 20)) + rng.normal(0, 1.5, BLOCK_SAMPLES)
    return (wave * 600 * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))).astype(np.int16)


def chime_block(freq=880.0):
    t = np.arange(BLOCK_SAMPLES) / SAMPLE_RATE
    return (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)


def fake_openai_chat(reply_for, calls_for=None, search_for=None):
    """A Responses API client. Each request streams `reply_for(messages)` as one text delta, where `messages` is
    the system prompt followed by the input; `calls_for(messages)` can add tool calls: [(name, arguments dict)], and
    `search_for(messages)` web-search event types that come first. tool_choice="none" suppresses the calls.
    Every request's keyword arguments are kept in `.requests`."""
    requests = []

    def create(**kw):
        requests.append(kw)
        messages = [{"role": "system", "content": kw["instructions"]}, *kw["input"]]
        events = [types.SimpleNamespace(type=t) for t in (search_for(messages) if search_for else [])]
        if text := reply_for(messages):
            events.append(types.SimpleNamespace(type="response.output_text.delta", delta=text))
        calls = calls_for(messages) if calls_for and kw.get("tool_choice") != "none" else []
        for i, (name, args) in enumerate(calls):
            item = types.SimpleNamespace(
                type="function_call", call_id=f"call_{len(requests)}_{i}", name=name, arguments=json.dumps(args)
            )
            events.append(types.SimpleNamespace(type="response.output_item.done", item=item))
        events.append(types.SimpleNamespace(type="response.completed"))
        return events

    client = types.SimpleNamespace(responses=types.SimpleNamespace(create=create))
    client.requests = requests
    return client


@pytest.fixture
def speaker():
    return FakeSpeaker()


@pytest.fixture
def log(tmp_path):
    return EventLog(tmp_path / "events")


@pytest.fixture
def convos(log):
    return ConversationLog(log)


@pytest.fixture
def client(log):
    return TestClient(create_app(log))


class ScriptedRecorder:
    """record() returns the next scripted utterance (None = nobody spoke) and logs the timeout it was given."""

    trailing_silence_s = 0.0

    def __init__(self, script):
        self.script = list(script)
        self.timeouts = []

    def record(self, mic, start_timeout_s=None, **kw):
        self.timeouts.append(start_timeout_s)
        return self.script.pop(0)


class ScriptedTranscriber:
    def __init__(self, texts):
        self.texts = iter(texts)

    def transcribe(self, pcm):
        return next(self.texts)


class RecordingBrain:
    def __init__(self, replies=()):
        self.replies = iter(replies)
        self.asked = []
        self.forgotten = 0
        self.sent = []

    def stream_reply(self, text):
        self.asked.append(text)
        return iter([next(self.replies, "ok")])

    def forget_last(self):
        self.forgotten += 1


@pytest.fixture
def no_tts(monkeypatch):
    """Consume the reply the way playback would, without calling TTS."""
    monkeypatch.setattr(
        assistant_module, "speak_streamed_reply", lambda pieces, voice, on_sentence: iter(list(pieces) and [])
    )


def make_assistant(speaker, utterances, transcripts, replies=(), journal=None):
    speaker.play_pcm_stream = lambda chunks, *a, **kw: list(chunks)
    voice = types.SimpleNamespace(sample_rate=24_000, stream=lambda text: iter([b"did-you-call-me"]))
    brain = RecordingBrain(replies)
    recorder = ScriptedRecorder(utterances)
    assistant = Assistant(
        FakeMic(), speaker, None, recorder, ScriptedTranscriber(transcripts), brain, voice, journal=journal
    )
    return assistant, brain


def speech(seconds=1.0) -> bytes:
    """What the recorder hands over: 16-bit mono PCM."""
    return b"\0\0" * int(SAMPLE_RATE * seconds)
