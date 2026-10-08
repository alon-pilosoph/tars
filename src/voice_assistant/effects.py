"""Optional post-processing for synthesized speech, applied chunk by chunk as the audio streams in."""

from collections.abc import Iterator
from typing import TYPE_CHECKING

import numpy as np
from scipy.signal import butter, lfilter, sosfilt

if TYPE_CHECKING:
    from .tts import Voice

RING_OUT_S = 0.025


class SpeakerBox:
    """A voice from a small speaker inside a metal enclosure, like TARS.

    Band-pass (small speaker), short feedback comb (metal ring), early reflections (small box, no reverb tail),
    gentle saturation. Filter state carries across chunks, so streamed audio has no clicks at chunk boundaries.
    """

    def __init__(
        self,
        sample_rate: int,
        band_hz: tuple[float, float] = (215, 5200),
        comb_ms: float = 3.75,
        comb_feedback: float = 0.32,
        reflections: tuple[tuple[float, float], ...] = ((6, 0.15), (12, 0.10), (17, 0.04)),
        drive: float = 1.25,
    ):
        self._sos = butter(4, band_hz, btype="band", fs=sample_rate, output="sos")
        self._sos_state = np.zeros((self._sos.shape[0], 2))

        delay = int(sample_rate * comb_ms / 1000)
        self._comb_b = np.array([1 - comb_feedback])
        self._comb_a = np.zeros(delay + 1)
        self._comb_a[0], self._comb_a[delay] = 1, -comb_feedback
        self._comb_state = np.zeros(delay)

        self._taps = [(int(sample_rate * ms / 1000), gain) for ms, gain in reflections]
        self._history = np.zeros(max(delay for delay, _ in self._taps))

        self._drive = drive

    def process(self, pcm: bytes) -> bytes:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64) / 32768
        y, self._sos_state = sosfilt(self._sos, x, zi=self._sos_state)
        y, self._comb_state = lfilter(self._comb_b, self._comb_a, y, zi=self._comb_state)

        extended = np.concatenate([self._history, y])
        out = y.copy()
        start = len(self._history)
        for delay, gain in self._taps:
            out += gain * extended[start - delay : start - delay + len(y)]
        self._history = extended[-len(self._history) :]

        out = np.tanh(self._drive * out) / np.tanh(self._drive)
        return (np.clip(out, -1, 1) * 32767).astype(np.int16).tobytes()


EFFECTS = {"tars": SpeakerBox}


class VoiceWithEffect:
    def __init__(self, voice: Voice, effect: str):
        self._voice = voice
        self._make_effect = EFFECTS[effect]
        self.sample_rate = voice.sample_rate

    def warm(self) -> None:
        self._voice.warm()

    def stream(self, text: str) -> Iterator[bytes]:
        effect = self._make_effect(self.sample_rate)
        pending = b""
        for chunk in self._voice.stream(text):
            pending += chunk
            usable = len(pending) - len(pending) % 2
            if usable:
                yield effect.process(pending[:usable])
            pending = pending[usable:]
        yield effect.process(bytes(int(RING_OUT_S * self.sample_rate) * 2))


def apply_effect(voice: Voice, effect: str) -> Voice:
    return VoiceWithEffect(voice, effect) if effect else voice
