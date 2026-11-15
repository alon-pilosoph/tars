"""What a real microphone does to sound before TARS hears it, for training stage 1 on audio that sounds like phones,
laptops and speakerphones rather than a studio.

The lab's synthetic clips, with noise mixed in, scored "hey TARS" at about 0.99; the same model on the Pi's
speakerphone scored real ones at about 0.4 (docs/wake-word.md#measuring-on-real-microphones). A speakerphone gates
quiet audio to exact zeros, evens out loudness, and narrows the band; phones and laptops do their own versions. Each
clip gets one made-up device, every stage of it random:

1. the microphone's frequency response: a high-pass, often a low-pass, sometimes a peak or a dip;
2. sometimes a narrowband codec: 8 kHz and back, sometimes 8-bit mu-law;
3. often automatic gain: a slow leveller towards a target loudness, with a compressor on top;
4. sometimes a noise gate: quiet stretches go to exact zeros, as the Pi's speakerphone does;
5. a final level, from very quiet to near clipping;
6. sometimes clipping.

Used by stage1/features.py (--device) on positives and lookalikes alike, and by device_backgrounds.py for negatives
that sound the same, so the model can't learn that a device's sound means a wake.
"""

import numpy as np

RATE = 16_000
FRAME = 160  # 10 ms


def _db(x: float) -> float:
    return 10 ** (x / 20)


def frequency_response(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    from scipy.signal import butter, sosfilt

    sos = [butter(2, rng.uniform(60, 400), "highpass", fs=RATE, output="sos")]
    if rng.random() < 0.6:
        sos.append(butter(rng.choice([2, 4]), rng.uniform(3400, 7600), "lowpass", fs=RATE, output="sos"))
    y = x
    for s in sos:
        y = sosfilt(s, y)
    if rng.random() < 0.4:
        y = _peak(y, rng.uniform(300, 5000), rng.uniform(-8, 8), rng.uniform(0.5, 3))
    return y


def _peak(x: np.ndarray, hz: float, gain_db: float, q: float) -> np.ndarray:
    """An RBJ peaking filter."""
    from scipy.signal import lfilter

    a = 10 ** (gain_db / 40)
    w = 2 * np.pi * hz / RATE
    alpha = np.sin(w) / (2 * q)
    b = [1 + alpha * a, -2 * np.cos(w), 1 - alpha * a]
    den = [1 + alpha / a, -2 * np.cos(w), 1 - alpha / a]
    return lfilter(b, den, x)


def narrowband(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    from scipy.signal import resample_poly

    y = resample_poly(x, 1, 2)
    if rng.random() < 0.5:  # 8-bit mu-law, as old telephony does
        mu = 255
        peak = max(np.abs(y).max(), 1e-6)
        c = np.sign(y / peak) * np.log1p(mu * np.abs(y / peak)) / np.log1p(mu)
        c = np.round(c * 127) / 127
        y = np.sign(c) * (np.expm1(np.abs(c) * np.log1p(mu)) / mu) * peak
    return resample_poly(y, 2, 1)[: len(x)]


def _frame_levels(x: np.ndarray) -> np.ndarray:
    n = len(x) // FRAME
    frames = x[: n * FRAME].reshape(n, FRAME)
    return np.sqrt((frames**2).mean(axis=1) + 1e-12)


def _per_sample(gains: np.ndarray, length: int) -> np.ndarray:
    """Frame gains as a smooth curve over every sample."""
    at = (np.arange(len(gains)) + 0.5) * FRAME
    return np.interp(np.arange(length), at, gains)


def automatic_gain(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """A leveller that rises slowly and falls fast towards a target, with a compressor above a threshold."""
    levels = _frame_levels(x)
    if not len(levels):
        return x
    target, most = _db(rng.uniform(-26, -14)), _db(rng.uniform(12, 30))
    rise, fall = np.exp(-1 / rng.uniform(20, 80)), np.exp(-1 / rng.uniform(2, 8))  # in frames
    gains, g = np.empty(len(levels)), 1.0
    for i, level in enumerate(levels):
        want = min(most, target / level) if level > 1e-5 else g
        g = (rise if want > g else fall) * g + (1 - (rise if want > g else fall)) * want
        gains[i] = g
    y = x * _per_sample(gains, len(x))
    threshold, ratio = _db(rng.uniform(-24, -10)), rng.uniform(2, 6)
    out_levels = _frame_levels(y)
    over = np.maximum(out_levels / threshold, 1.0)
    return y * _per_sample(over ** (1 / ratio - 1), len(y))


def noise_gate(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Quiet stretches to exact zeros: opens at once on sound, closes after a hold, with a short fade."""
    levels = _frame_levels(x)
    if not len(levels):
        return x
    peak = levels.max()
    if peak <= 0:
        return x
    threshold = peak * _db(rng.uniform(-40, -20))
    hold = int(rng.integers(5, 30))  # frames
    fade = int(rng.integers(1, 4))
    open_, since = np.zeros(len(levels)), hold + 1
    for i, level in enumerate(levels):
        since = 0 if level >= threshold else since + 1
        open_[i] = 1.0 if since <= hold else max(0.0, 1 - (since - hold) / fade)
    gains = np.repeat(open_, FRAME)
    gains = np.concatenate([gains, np.full(len(x) - len(gains), gains[-1] if len(gains) else 1.0)])
    return x * gains


def level_and_clip(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    peak = np.abs(x).max()
    if peak <= 0:
        return x
    y = x * (_db(rng.uniform(-40, -1)) / peak)
    if rng.random() < 0.1:
        limit = np.abs(y).max() * rng.uniform(0.3, 0.9)
        y = np.clip(y, -limit, limit)
    return y


def device(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """One made-up device's take on `x` (float, -1 to 1, 16 kHz), as float32."""
    y = np.asarray(x, dtype=np.float64)
    y = frequency_response(y, rng)
    if rng.random() < 0.15:
        y = narrowband(y, rng)
    if rng.random() < 0.6:
        y = automatic_gain(y, rng)
    if rng.random() < 0.35:
        y = noise_gate(y, rng)
    y = level_and_clip(y, rng)
    return np.clip(y, -1.0, 1.0).astype(np.float32)


class DeviceAugmentation:
    """microWakeWord's Augmentation, then a device, on `share` of the clips (the rest pass as they were)."""

    def __init__(self, inner, share: float, seed: int | None = None):
        self.inner, self.share = inner, share
        self.rng = np.random.default_rng(seed)

    def augment_clip(self, audio: np.ndarray) -> np.ndarray:
        out = self.inner.augment_clip(audio)
        if self.rng.random() >= self.share:
            return out
        if out.dtype == np.int16:
            return (device(out.astype(np.float32) / 32768, self.rng) * 32767).astype(np.int16)
        return device(out, self.rng)

    def augment_generator(self, audio_generator):
        for audio in audio_generator:
            yield self.augment_clip(audio)
