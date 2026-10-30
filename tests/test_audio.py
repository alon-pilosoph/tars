"""The audio devices, faked: a microphone that goes quiet, the speaker's playback, refill and dead device, and devices
that only work at their own rate."""

import itertools
import queue
import threading
import time

import numpy as np
import pytest

import voice_assistant.audio as audio_module
from voice_assistant.audio import (
    BLOCK_SAMPLES,
    SAMPLE_RATE,
    Microphone,
    MicrophoneError,
    NoDeviceError,
    Resampler,
    Speaker,
    SpeakerError,
    device_rate,
    find_device,
)


@pytest.fixture(autouse=True)
def devices_take_any_rate(monkeypatch):
    """No real device is asked what it takes: each takes whatever it's opened at, unless a test says otherwise."""
    monkeypatch.setattr(audio_module, "device_rate", lambda device, kind, wanted: wanted)


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
        self.callback, self.played, self.running, self.opened = callback, bytearray(), False, kw

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


def test_no_device_to_open_says_plug_it_in(monkeypatch):
    def missing(**_):
        raise audio_module.sd.PortAudioError("Error querying device -1")

    monkeypatch.setattr(audio_module.sd, "RawInputStream", missing)
    monkeypatch.setattr(audio_module.sd, "RawOutputStream", missing)
    with pytest.raises(NoDeviceError, match="No microphone to open .* Plug in the speakerphone"):
        Microphone(None)
    with pytest.raises(NoDeviceError, match="No speaker to open .* Plug in the speakerphone"):
        Speaker(None, 24_000, prebuffer_s=0.1)
    headphones_only = [{"name": "bcm2835 Headphones", "max_input_channels": 0}]
    monkeypatch.setattr(audio_module.sd, "query_devices", lambda: headphones_only)
    with pytest.raises(NoDeviceError, match="No input device matching 'PowerConf'"):
        find_device("PowerConf", "input")


# Devices that only work at their own rate (the PowerConf: 48 kHz, nothing else, on Linux).


def tone(rate: int, seconds: float, hz: float = 440.0) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (8000 * np.sin(2 * np.pi * hz * t)).astype(np.int16)


@pytest.mark.parametrize(("rate_in", "rate_out"), [(48_000, 16_000), (24_000, 48_000), (16_000, 48_000)])
def test_resampling_in_blocks_is_scipys_resampling_of_the_whole(rate_in, rate_out):
    from scipy.signal import resample_poly

    audio = tone(rate_in, 0.5)
    whole = Resampler(rate_in, rate_out)
    once = np.concatenate([whole(audio), whole.flush()])
    pieces = Resampler(rate_in, rate_out)
    cuts = [0, 1, 97, 3840, 5000, len(audio)]
    blocks = np.concatenate([pieces(audio[a:b]) for a, b in itertools.pairwise(cuts)] + [pieces.flush()])
    assert np.array_equal(blocks, once)  # no seams where the blocks meet
    expected = resample_poly(
        audio.astype(np.float64), rate_out // np.gcd(rate_in, rate_out), rate_in // np.gcd(rate_in, rate_out)
    )
    lag = 10 * max(rate_in, rate_out) // np.gcd(rate_in, rate_out) // (rate_in // np.gcd(rate_in, rate_out))
    assert np.abs(once[lag : lag + len(expected)] - np.rint(expected)).max() <= 1


class FakeInput:
    """An input device: the test hands it blocks, as the device's driver would."""

    def __init__(self, callback, **kw):
        self.callback, self.opened = callback, kw

    def give(self, block: np.ndarray):
        self.callback(block.tobytes(), len(block), None, None)

    def start(self):
        pass


@pytest.mark.parametrize("rate", [48_000, 44_100])
def test_a_microphone_at_its_own_rate_still_gives_80_ms_at_16_khz(monkeypatch, rate):
    monkeypatch.setattr(audio_module, "device_rate", lambda device, kind, wanted: rate)
    monkeypatch.setattr(audio_module.sd, "RawInputStream", FakeInput)
    mic = Microphone(None)
    assert mic._stream.opened["samplerate"] == rate
    heard = tone(rate, 1.0)
    step = mic._stream.opened["blocksize"]
    assert step == round(BLOCK_SAMPLES * rate / SAMPLE_RATE)  # 80 ms of the device's own samples
    for at in range(0, len(heard) - step + 1, step):
        mic._stream.give(heard[at : at + step])
    blocks = [mic.read() for _ in range(mic._queue.qsize())]
    assert len(blocks) >= 11 and {len(b) for b in blocks} == {BLOCK_SAMPLES}
    middle = np.concatenate(blocks)[1600:-1600]  # 440 Hz at full level, at 16 kHz
    assert 7800 < np.abs(middle).max() < 8200  # the same level, give or take the filter's ripple
    spectrum = np.abs(np.fft.rfft(middle))
    assert abs(np.argmax(spectrum) * SAMPLE_RATE / len(middle) - 440) < 5


def test_a_speaker_at_its_own_rate_plays_the_voice_converted(monkeypatch):
    monkeypatch.setattr(audio_module, "device_rate", lambda device, kind, wanted: 48_000)
    monkeypatch.setattr(audio_module.sd, "RawOutputStream", FakeOutput)
    played_for_echo = []

    class Echo:
        def played(self, pcm, rate):
            played_for_echo.append(rate)

    voice = tone(24_000, 0.3, hz=1000)
    with Speaker(None, 24_000, prebuffer_s=10.0, echo=Echo()) as speaker:
        speaker.play_pcm_stream([voice.tobytes()], 24_000)
        played = np.frombuffer(bytes(speaker._stream.played), np.int16)
    assert speaker._stream.opened["samplerate"] == 48_000
    assert set(played_for_echo) == {48_000}  # the canceller hears it at the rate it really played
    voiced = played[np.flatnonzero(played)[0] : np.flatnonzero(played)[-1] + 1]
    assert abs(len(voiced) - 2 * len(voice)) < 100  # twice the samples: the same 0.3 s
    spectrum = np.abs(np.fft.rfft(voiced))
    assert abs(np.argmax(spectrum) * 48_000 / len(voiced) - 1000) < 10


def test_a_device_is_opened_at_the_rate_wanted_if_it_takes_it_and_its_own_if_not(monkeypatch):
    monkeypatch.undo()  # the real device_rate, with sounddevice faked
    takes = {48_000}

    def check(device, samplerate, channels, dtype):
        if samplerate not in takes:
            raise audio_module.sd.PortAudioError("Invalid sample rate")

    monkeypatch.setattr(audio_module.sd, "check_input_settings", check)
    monkeypatch.setattr(audio_module.sd, "check_output_settings", check)
    monkeypatch.setattr(audio_module.sd, "query_devices", lambda device, kind: {"default_samplerate": 48000.0})
    assert device_rate(3, "input", 16_000) == 48_000
    assert device_rate(3, "output", 48_000) == 48_000
    takes.add(16_000)
    assert device_rate(3, "input", 16_000) == 16_000

    def nothing_there(device, kind):
        raise audio_module.sd.PortAudioError("Error querying device -1")

    takes.clear()
    monkeypatch.setattr(audio_module.sd, "query_devices", nothing_there)
    assert device_rate(None, "input", 16_000) == 16_000  # opening it then says what's wrong


def test_a_stop_cuts_what_s_playing_and_takes_no_more_of_it_and_the_next_plays(monkeypatch):
    monkeypatch.setattr(audio_module.sd, "RawOutputStream", FakeOutput)
    taken = []

    def reply(speaker):  # streamed, and stopped after its first piece is in
        for n in range(10):
            taken.append(n)
            yield np.full(2400, 1000 + n, np.int16).tobytes()
            if n == 0:
                speaker.stop()

    with Speaker(None, 24_000, prebuffer_s=10.0) as speaker:
        speaker.play_pcm_stream(reply(speaker), 24_000)
        assert taken == [0, 1]  # the piece after the stop was the last taken, and none of it went in
        assert not np.frombuffer(bytes(speaker._stream.played), np.int16).any()  # cut before it played
        speaker.play_pcm_stream([np.full(2400, 3000, np.int16).tobytes()], 24_000)
        played = np.frombuffer(bytes(speaker._stream.played), np.int16)
    assert (played == 3000).sum() == 2400
