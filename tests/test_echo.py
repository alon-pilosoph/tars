"""Echo cancellation: TARS's own sound taken out of what the mic hears, and a leftover trace taken off a transcript."""

import queue

import numpy as np
import pytest

import voice_assistant.audio as audio_module
from voice_assistant.audio import BLOCK_SAMPLES, SAMPLE_RATE, Microphone, Speaker
from voice_assistant.echo import EchoCanceller, without_echo

from .test_audio import FakeOutput


@pytest.mark.parametrize(
    "text, said, left",
    [
        ("Yes, Alon? What's the weather", "Yes, Alon?", "What's the weather"),
        ("yes what's the weather", "Yes, Alon?", "what's the weather"),  # only part of it got through
        ("yes alon", "Yes, Alon?", ""),
        ("What's the weather", "Yes, Alon?", "What's the weather"),
        ("Alon wants to know", "Yes, Alon?", "Alon wants to know"),  # only from the start, in order
        ("Yes, please", None, "Yes, please"),
    ],
)
def test_a_trace_of_what_tars_said_is_taken_off_the_start_of_what_it_heard(text, said, left):
    assert without_echo(text, said) == left


def speechlike(seconds: float, rng) -> np.ndarray:
    """Noise in syllable-length bursts, loud enough to be heard: enough like a voice for an echo canceller."""
    n = int(SAMPLE_RATE * seconds)
    bursts = (np.sin(np.arange(n) * 2 * np.pi * 4 / SAMPLE_RATE) > 0).astype(float)
    return rng.normal(0, 0.2, n) * bursts


def test_tars_own_sound_is_taken_out_of_what_the_mic_hears():
    rng = np.random.default_rng(0)
    played = speechlike(4.0, rng)
    room = np.zeros(1600)  # 100 ms of room: the direct sound, then decaying reflections
    room[0] = 0.6
    room += rng.normal(0, 0.05, 1600) * np.exp(-np.arange(1600) / 300)
    echo = np.concatenate([np.zeros(800), np.convolve(played, room)])[: len(played)]  # 50 ms to the mic
    pcm = lambda x: (np.clip(x, -1, 1) * 32767).astype(np.int16)
    canceller = EchoCanceller(SAMPLE_RATE)
    canceller.set_delay(0.05)
    out, heard = [], pcm(echo)
    for i in range(0, len(played) - BLOCK_SAMPLES + 1, BLOCK_SAMPLES):
        canceller.played(pcm(played[i : i + BLOCK_SAMPLES]).tobytes(), SAMPLE_RATE)
        out.append(canceller.heard(heard[i : i + BLOCK_SAMPLES]))
    after = np.concatenate(out)[SAMPLE_RATE * 2 :].astype(float)  # once it has learned the room
    before = heard[SAMPLE_RATE * 2 : SAMPLE_RATE * 2 + len(after)].astype(float)
    removed_db = 10 * np.log10(np.mean(before**2) / max(np.mean(after**2), 1e-9))
    assert removed_db > 15


class RecordingEcho:
    def __init__(self):
        self.heard_by_speaker, self.heard_blocks = bytearray(), 0

    def played(self, pcm, sample_rate):
        self.heard_by_speaker += pcm

    def heard(self, block):
        self.heard_blocks += 1
        return block


def test_the_speaker_tells_the_canceller_everything_it_plays_silence_included(monkeypatch):
    monkeypatch.setattr(audio_module.sd, "RawOutputStream", FakeOutput)
    echo = RecordingEcho()
    voice = np.arange(1000, 3400, dtype=np.int16).tobytes()
    with Speaker(None, 24_000, prebuffer_s=0.1, echo=echo) as speaker:
        speaker.play_pcm_stream([voice], 24_000)
        speaker._stream.stop()
        reported, device = bytes(echo.heard_by_speaker), bytes(speaker._stream.played)
    n = min(len(reported), len(device))  # the fake device's thread may be one frame further along in either
    assert reported[:n] == device[:n] and voice in reported  # the same frames, the silence around them too


def test_the_mic_keeps_cleaning_while_closed_so_the_canceller_keeps_learning():
    mic = Microphone.__new__(Microphone)  # skip opening a real device
    mic._queue, mic._muted, mic.echo = queue.Queue(), True, RecordingEcho()
    mic._on_audio(np.zeros(BLOCK_SAMPLES, np.int16).tobytes(), BLOCK_SAMPLES, None, None)
    assert mic.echo.heard_blocks == 1 and mic._queue.empty()
    mic._muted = False
    mic._on_audio(np.ones(BLOCK_SAMPLES, np.int16).tobytes(), BLOCK_SAMPLES, None, None)
    assert mic.echo.heard_blocks == 2 and mic._queue.get_nowait().sum() == BLOCK_SAMPLES
