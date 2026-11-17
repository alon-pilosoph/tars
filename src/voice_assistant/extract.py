"""Getting a household member's voice out of mixed audio (the TV, other people talking), for the wake's double-check.
Experimental and off by default ([extract] enabled).

The double-check always hears the raw audio first, so a guest's "hey TARS" is judged as it always is. Only when it
turns a wake down, and that wake was said over other speech (`mixed`), does each enrolled voice get taken out of the
same few seconds and checked again: one more chance, never a filter. The taking out runs on an extraction server
(tools/extract_server.py: WeSep's target speaker extraction, too slow for a Pi 4, about 0.2 s on a GPU), fed a few of
each person's ordinary requests (voice_data/enroll/<name>/*.wav) as the reference. Measured offline on the owner's
takes over a real TV hour (docs/wake-word.md): answered 70/44/20/12% at voice 10/5/0/-5 dB over the TV, 93/76/52/22%
with the second chance, and no extra false answers on the TV-only hours or on his own lookalikes.
"""

import io
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE

ENROLL_MAX_S = 15.0  # of each person's reference audio: enough for the speaker encoder, small to send
NAME_S = 1.2  # the end of a wake window that holds "hey TARS"; what's before it is what was around it
STEP = 512  # the speech detector's step


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) + 1e-3


def mixed(audio: np.ndarray, speech: Callable[[np.ndarray], float], share: float, level_db: float) -> bool:
    """Whether a wake window was said over other speech: the speech detector (`speech`, fed STEP-sample steps in
    order) hears speech in at least `share` of what came before the name, and the name is at most `level_db` louder
    than it. Offline, this flags 83-92% of takes over a real TV and none in quiet."""
    cut = len(audio) - int(NAME_S * SAMPLE_RATE)
    before, name = audio[:cut], audio[cut:]
    steps = [before[i : i + STEP] for i in range(0, len(before) - STEP + 1, STEP)]
    if not steps:
        return False
    talking = np.mean([speech(s) >= 0.5 for s in steps])
    return bool(talking >= share and 20 * np.log10(_rms(name) / _rms(before)) <= level_db)


def kept_enough(voice: np.ndarray, audio: np.ndarray, least: float) -> bool:
    """Whether an extraction (`voice`, float, full scale 1) of `audio` (int16) can be trusted at all: one that kept
    almost nothing (a wrong reference, a failed pass) would be checked as silence, so it's dropped and the raw audio's
    answer stands."""
    if not np.isfinite(voice).all():
        return False
    level = float(np.sqrt(np.mean((audio.astype(np.float64) / 32768) ** 2)))
    return bool(level > 0 and np.sqrt(np.mean(voice.astype(np.float64) ** 2)) / level >= least)


def pack(**arrays: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.savez(buffer, **arrays)
    return buffer.getvalue()


def unpack(data: bytes) -> dict[str, np.ndarray]:
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


class Extractor:
    """The client for the extraction server: each enrolled person's voice, taken out of a wake window."""

    def __init__(self, url: str, enroll: Path, timeout_s: float, least: float, http=None):
        import httpx

        self._url, self._timeout, self._least = url, timeout_s, least
        self._http = http or httpx.Client(timeout=timeout_s)
        self.voices = self._references(enroll)

    @staticmethod
    def _references(folder: Path) -> dict[str, np.ndarray]:
        """Each person's reference: their WAVs (16 kHz mono), joined, at most ENROLL_MAX_S."""
        import soundfile as sf

        voices = {}
        for person in sorted(p for p in folder.glob("*") if p.is_dir()) if folder.is_dir() else []:
            parts = []
            for wav in sorted(person.glob("*.wav")):
                x, rate = sf.read(str(wav), dtype="int16", always_2d=True)
                if rate == SAMPLE_RATE:
                    parts += [x.mean(axis=1).astype(np.int16), np.zeros(SAMPLE_RATE // 4, np.int16)]
            if parts:
                voices[person.name] = np.concatenate(parts)[: int(ENROLL_MAX_S * SAMPLE_RATE)]
        return voices

    def extract(self, audio: np.ndarray, reference: np.ndarray) -> np.ndarray | None:
        """`reference`'s voice out of `audio` (int16), or None if the server failed or it kept too little."""
        try:
            reply = self._http.post(self._url, content=pack(mix=audio, enroll=reference), timeout=self._timeout)
            reply.raise_for_status()
            voice = unpack(reply.content)["voice"]
        except Exception as e:  # noqa: BLE001 - the raw check's answer stands
            print(f"(couldn't take a voice out of the mix: {e!r})")
            return None
        if not kept_enough(voice, audio, self._least):
            return None
        peak = float(np.abs(voice).max()) or 1.0
        return (np.clip(voice / peak * 0.9, -1, 1) * 32767).astype(np.int16)

    def each_voice(self, audio: np.ndarray) -> Iterator[np.ndarray]:
        """Each enrolled voice taken out of `audio`, one at a time, so the first that passes ends it."""
        for reference in self.voices.values():
            if (voice := self.extract(audio, reference)) is not None:
                yield voice


class SecondChance:
    """What VerifiedTrigger asks when the double-check turns a wake down: the enrolled voices taken out of that wake
    window, but only when it was said over other speech."""

    def __init__(self, extractor: Extractor, speech_detector, share: float, level_db: float):
        self._extractor, self._detector = extractor, speech_detector
        self._share, self._level_db = share, level_db

    def __call__(self, audio: np.ndarray) -> Iterator[np.ndarray]:
        if not self._extractor.voices:
            return iter(())
        self._detector.reset()
        if not mixed(audio, self._detector, self._share, self._level_db):
            return iter(())
        return self._extractor.each_voice(audio)
