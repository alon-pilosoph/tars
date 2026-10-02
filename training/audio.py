"""Audio helpers shared by the training scripts and the benchmarks: reading clips, the test conditions (noise and real
rooms no model trains on), and long stretches of audiobook speech.

Only numpy is imported at module level; scipy and soundfile aren't in every training environment.
"""

import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from training.common import SR

# Every end-to-end test runs each clip through these, in this order:
# (name, interference bank or None, SNR in dB, through a real recorded room?)
CONDITIONS = [
    ("clean", None, None, False),
    ("tv_5dB", "tv", 5, False),
    ("babble_5dB", "babble", 5, False),
    ("far_room+tv_10dB", "tv", 10, True),
    ("tv_15dB", "tv", 15, False),
    ("babble_15dB", "babble", 15, False),
    ("tv_10dB", "tv", 10, False),
    ("babble_10dB", "babble", 10, False),
]
QUIET = "clean"


def read_wav(path: Path) -> np.ndarray:
    """A 16-bit mono WAV as int16, resampled to 16 kHz if needed."""
    with wave.open(str(path)) as f:
        audio = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
        rate = f.getframerate()
    if rate != SR:
        from scipy.signal import resample_poly

        audio = resample_poly(audio.astype(np.float32), SR, rate).astype(np.int16)
    return audio


def bank(folder: Path, limit: int = 200) -> list[np.ndarray]:
    return [read_wav(f) for f in sorted(folder.glob("*.wav"))[:limit]]


def mix(clip: np.ndarray, noise: np.ndarray, snr_db: float | None, rng: np.random.Generator) -> np.ndarray:
    """`clip` with a random stretch of `noise` at `snr_db` (None: the clip as it is)."""
    if snr_db is None:
        return clip
    start = rng.integers(0, max(1, len(noise) - len(clip)))
    n = np.resize(noise[start : start + len(clip)].astype(np.float32), len(clip))
    p_clip = np.mean(clip.astype(np.float32) ** 2) + 1e-9
    p_noise = np.mean(n**2) + 1e-9
    n *= np.sqrt(p_clip / (p_noise * 10 ** (snr_db / 10)))
    return np.clip(clip + n, -32768, 32767).astype(np.int16)


def in_room(clip: np.ndarray, rir: np.ndarray) -> np.ndarray:
    """`clip` played in the room `rir` was recorded in, at the same loudness."""
    from scipy.signal import fftconvolve

    wet = fftconvolve(clip.astype(np.float32), rir.astype(np.float32) / (np.abs(rir).max() + 1e-9))[: len(clip)]
    wet *= (np.sqrt(np.mean(clip.astype(np.float32) ** 2)) + 1e-9) / (np.sqrt(np.mean(wet**2)) + 1e-9)
    return np.clip(wet, -32768, 32767).astype(np.int16)


class Interference:
    """The test interference (DATA/bench/interference/test, from make_interference.py): noise banks and real rooms."""

    def __init__(self, folder: Path, banks: tuple[str, ...] = ("tv", "babble")):
        self.banks = {name: bank(folder / name) for name in banks}
        self.rooms = bank(folder / "rooms", limit=1000)
        empty = [name for name, clips in {**self.banks, "rooms": self.rooms}.items() if not clips]
        if empty:
            raise SystemExit(f"No test interference in {folder} ({', '.join(empty)}): run make_interference.py.")

    def apply(self, clip: np.ndarray, condition: tuple, rng: np.random.Generator) -> np.ndarray:
        _, noise, snr, room = condition
        if room:
            clip = in_room(clip, self.rooms[rng.integers(len(self.rooms))])
        return mix(clip, self.banks[noise][rng.integers(len(self.banks[noise]))], snr, rng) if noise else clip


def conditioned(
    sets: dict, interference: Interference, rng: np.random.Generator, conditions: list = CONDITIONS
) -> Iterator[tuple[str, str, bool, np.ndarray]]:
    """Yields (condition, set name, should it be accepted?, clip) for every clip of every set ({name: (files,
    should it be accepted?)}) in every condition. The order is fixed, so a clip always gets the same noise."""
    for condition in conditions:
        for name, (files, should) in sets.items():
            for f in files:
                yield condition[0], name, should, interference.apply(read_wav(f), condition, rng)


def quiet_room(rng: np.random.Generator, seconds: float = 2.0) -> np.ndarray:
    """Faint noise padded around a test clip, as in a live stream."""
    return rng.normal(0, 40, int(SR * seconds)).astype(np.int16)


def long_speech(folder: Path, max_hours: float) -> np.ndarray:
    """About `max_hours` of LibriSpeech test-clean back to back, for false answers per hour."""
    import soundfile as sf

    flacs = sorted(folder.glob("**/*.flac"))
    if not flacs:
        raise SystemExit(f"No audiobooks in {folder}: run training/data/downloads.sh.")
    parts, total = [], 0
    for f in flacs:
        a, _ = sf.read(f, dtype="int16")
        parts.append(a)
        total += len(a)
        if total > max_hours * 3600 * SR:
            break
    return np.concatenate(parts)
