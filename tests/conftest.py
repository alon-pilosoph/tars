"""Stand-ins for the audio devices and APIs, so the pipeline can be tested offline in milliseconds."""

import types
from contextlib import contextmanager

import numpy as np
import pytest

from voice_assistant.audio import BLOCK_SAMPLES, SAMPLE_RATE


class FakeMic:
    """Replays a list of 80 ms blocks."""

    def __init__(self, blocks=()):
        self.blocks = iter(blocks)

    def read(self):
        return next(self.blocks)

    @contextmanager
    def paused(self, tail_s=0.3):
        yield


class FakeSpeaker:
    def __init__(self):
        self.sounds = []

    def chime(self, **kw):
        self.sounds.append(("chime", kw.get("freq", 880.0)))

    def error_tone(self):
        self.sounds.append(("error", None))


def quiet_block(rng=np.random.default_rng(0)):
    return rng.normal(0, 30, BLOCK_SAMPLES).astype(np.int16)


def voiced_block():
    """Harmonics of 150 Hz with a slow wobble: something webrtcvad reliably calls speech."""
    t = np.arange(BLOCK_SAMPLES) / SAMPLE_RATE
    wave = sum(np.sin(2 * np.pi * 150 * k * t) / k for k in range(1, 12))
    return (wave * 4000 * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))).astype(np.int16)


def fake_openai_chat(reply_for):
    """A client whose chat stream yields `reply_for(messages)` as a single chunk."""

    def create(**kw):
        delta = types.SimpleNamespace(content=reply_for(kw["messages"]))
        return [types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)])]

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


@pytest.fixture
def speaker():
    return FakeSpeaker()
