"""`--mic-test`: a live meter for tuning the mic and the wake word, no API key needed.

The bottom line updates live. Every wake, and every near-miss that got close to the threshold,
is printed on its own line above it, so the history stays on screen while you walk around.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

from .audio import Microphone, find_device, save_wav
from .config import Config
from .recorder import UtteranceRecorder
from .verify import ANSWER, ASK, IGNORE, RecentAudio
from .wake import wake_word_trigger

QUIET_S = 1.0  # a near-miss ends after this long below the "close" line
AFTER_WAKE_S = 1.5  # ignore the tail of the same phrase after a wake


@dataclass
class WakeLog:
    """Turns a stream of scores into events: one line per wake, one per near-miss (with its peak)."""

    threshold: float
    close: float = 0.0  # scores at or above this count as a near-miss; defaults to half the threshold
    wakes: int = 0
    near_misses: int = 0
    _peak: float = 0.0
    _last_close: float = -1e9
    _mute_until: float = -1e9
    history: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.close = self.close or self.threshold / 2

    def update(self, score: float, now: float) -> str | None:
        """Feed one score; returns a line to print when an event happens."""
        if now < self._mute_until:
            return None
        if score >= self.threshold:
            self.wakes += 1
            self._peak, self._mute_until = 0.0, now + AFTER_WAKE_S
            return self._record(f"WAKE #{self.wakes}   score {score:.2f}")
        if score >= self.close:
            self._peak, self._last_close = max(self._peak, score), now
        elif self._peak and now - self._last_close > QUIET_S:
            peak, self._peak = self._peak, 0.0
            self.near_misses += 1
            return self._record(f"close      peak {peak:.2f} (needs {self.threshold:.2f})")
        return None

    def _record(self, text: str) -> str:
        line = f"{time.strftime('%H:%M:%S')}  {text}"
        self.history.append(line)
        return line


def save_wav_log(folder: Path, pcm, passed: bool, heard: str) -> Path:
    tag = "passed" if passed else "rejected"
    words = "_".join(heard.replace("[unk]", "unk").split())[:40] or "none"
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}_{tag}_{words}.wav"
    save_wav(path, pcm.astype("int16").tobytes())
    return path


def meter(level: float, score: float, threshold: float, speech: bool, width: int = 20) -> str:
    """One status line: loudness bar, speech flag, and a wake-score bar with the threshold marked."""
    mark = min(width - 1, int(threshold * width))
    filled = int(min(1.0, score) * width)
    bar = "".join("|" if i == mark else ("#" if i < filled else " ") for i in range(width))
    level_bar = "#" * int(min(1.0, level) * 10)
    return f"level [{level_bar:<10}] {'SPEECH' if speech else '      '}  wake [{bar}] {score:.2f}"


def mic_test(cfg: Config, root: Path) -> None:
    wake = wake_word_trigger(cfg.wake.model, cfg.wake.threshold)
    verifier = None
    if cfg.wake.verify:
        from .verify import PhraseVerifier

        check = root / cfg.wake.check_model if cfg.wake.check_model else None
        verifier = PhraseVerifier(wake.phrase, root / "models", check)
    recorder = UtteranceRecorder(cfg.recorder)
    recent = RecentAudio(cfg.wake.check_window_s)
    log = WakeLog(cfg.wake.threshold)
    passed = 0
    check = " Each wake is then double-checked by the speech recognizer." if verifier else ""
    print(
        f"Say '{wake.phrase}'. It wakes at {cfg.wake.threshold:.2f} (the | on the wake bar); "
        f"near-misses above {log.close:.2f} are logged too.{check} Ctrl+C to stop.\n"
    )
    try:
        with Microphone(find_device(cfg.audio.input_device, "input")) as mic:
            peak_hold, peak_until = 0.0, 0.0
            while True:
                block = mic.read()
                recent.add(block)
                now = time.monotonic()
                score = wake.score(block)
                event = log.update(score, now)
                if event:
                    woke = "WAKE" in event
                    if woke:
                        wake.reset()  # like the assistant does after a wake
                        ok = True
                        if verifier:
                            outcome, heard = verifier.decide(recent.audio())
                            ok = outcome == ANSWER
                            label = {ANSWER: "passed", ASK: "ASK 'Did you call me?'", IGNORE: "REJECTED"}[outcome]
                            event += f"   {label}: heard '{heard}'"
                            if verifier.last_confidence is not None:
                                event += f" ({verifier.last_confidence:.2f})"
                        # Keep what woke it, so real misses and false wakes can become test data.
                        saved = save_wav_log(
                            root / "voice_data" / "wake_log", recent.audio(), ok, heard if verifier else ""
                        )
                        event += f"   ({saved.name})"
                        passed += ok
                    # Clear the live line, print the event on its own line (beep if the assistant would answer).
                    print("\r\033[K" + event + ("\a" if woke and ok else ""), flush=True)
                # Hold the highest recent score for a second so short spikes are readable.
                if score >= peak_hold or now > peak_until:
                    peak_hold, peak_until = score, now + 1.0
                level = float(abs(block).mean()) / 3000
                line = meter(level, peak_hold, cfg.wake.threshold, recorder.is_speech(block))
                tally = (
                    f"wakes {log.wakes}" + (f" (passed {passed})" if verifier else "") + f"  close {log.near_misses}"
                )
                print(f"\r\033[K{line}   {tally}", end="", flush=True)
    finally:
        # The events are already on screen above; just finish the live line with the totals.
        passed_text = f" ({passed} passed the check)" if verifier else ""
        print(f"\n{log.wakes} wakes{passed_text}, {log.near_misses} near-misses.")
