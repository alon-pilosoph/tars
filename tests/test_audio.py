"""The audio devices, faked: a microphone that goes quiet, and the speaker's playback, refill and dead device."""

import queue
import threading
import time

import numpy as np
import pytest

import voice_assistant.audio as audio_module
from voice_assistant.audio import Microphone, MicrophoneError, Speaker, SpeakerError


def test_dead_microphone_raises_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(audio_module, "MIC_STALL_S", 0.1)
    mic = Microphone.__new__(Microphone)  # skip opening a real device
    mic._queue = queue.Queue()
    with pytest.raises(MicrophoneError):
        mic.read()


class FakeOutput:
    """An output device pulling 10 ms at a time, as fast as it can."""

    latency = 0.0

    def __init__(self, callback, **kw):
        self.callback, self.played, self.running = callback, bytearray(), False

    def start(self):
        self.running = True
        threading.Thread(target=self._pull, daemon=True).start()

    def _pull(self):
        while self.running:
            out = bytearray(480)
            self.callback(out, 240, None, None)
            self.played += out

    def stop(self):
        self.running = False

    def close(self):
        pass


class DeadOutput(FakeOutput):
    def start(self):
        pass


@pytest.mark.parametrize("split_at", [None, 1, 4801, 24_000])  # chunks cut anywhere, mid-sample included
def test_playback_is_from_the_first_sound_to_the_last_not_the_silence_around_them(monkeypatch, split_at):
    monkeypatch.setattr(audio_module.sd, "RawOutputStream", FakeOutput)
    padding = np.full(12_000, 50, np.int16).tobytes()  # half a second too quiet to hear
    voice = np.arange(1000, 3400, dtype=np.int16).tobytes()
    audio = padding + voice + padding
    chunks = [audio] if split_at is None else [audio[:split_at], audio[split_at:]]
    with Speaker(None, 24_000, prebuffer_s=10.0) as speaker:  # holds playback until all of it is in
        speaker.play_pcm_stream(chunks, 24_000)
        played = np.frombuffer(bytes(speaker._stream.played), np.int16)
    assert played[played != 0].tobytes() == voice


def test_audio_that_runs_dry_mid_reply_waits_for_a_refill_instead_of_stuttering(monkeypatch):
    monkeypatch.setattr(audio_module.sd, "RawOutputStream", FakeOutput)
    first, late, rest = (
        np.arange(start, start + n, dtype=np.int16) for start, n in [(1000, 2400), (4000, 480), (5000, 2400)]
    )

    def network():  # a burst, a gap long enough to play it all, then the rest in two pieces
        yield first.tobytes()
        time.sleep(0.2)
        yield late.tobytes()
        time.sleep(0.2)
        yield rest.tobytes()

    with Speaker(None, 24_000, prebuffer_s=0.1) as speaker:
        speaker.play_pcm_stream(network(), 24_000)
        played = np.frombuffer(bytes(speaker._stream.played), np.int16)
    voiced = np.flatnonzero(played)
    assert played[voiced].tobytes() == np.concatenate([first, late, rest]).tobytes()
    late_and_rest = voiced[len(first) :]
    assert np.all(np.diff(late_and_rest) == 1)  # the late bit waited for the rest, and they played as one


def test_a_dead_speaker_raises_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(audio_module.sd, "RawOutputStream", DeadOutput)
    monkeypatch.setattr(audio_module, "SPEAKER_STALL_S", 0.1)
    with Speaker(None, 24_000, prebuffer_s=0.1) as speaker, pytest.raises(SpeakerError):
        speaker.play_pcm_stream([np.full(2400, 1000, np.int16).tobytes()], 24_000)
