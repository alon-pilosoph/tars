"""`--mic-test`: a live meter for tuning the mic and the wake word, no API key needed.

Each wake and near-miss prints on its own line above the live meter, so the history stays on screen.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

from .audio import Microphone, find_device
from .config import Config
from .recorder import make_recorder
from .verify import ANSWER, ASK, IGNORE, NearMisses, RecentAudio
from .versions import pair_source
from .wake import wake_word_trigger

AFTER_WAKE_S = 1.5  # ignore the tail of the same phrase after a wake


@dataclass
class Logged:
    woke: bool
    line: str


@dataclass
class WakeLog:
    """One line per wake and per near-miss (with its peak), counted the way the assistant counts them."""

    threshold: float
    wakes: int = 0
    near_misses: int = 0
    history: list[str] = field(default_factory=list)

    def __post_init__(self):
        self._near = NearMisses(self.threshold)
        self.close = self._near.near
        self._last = self._mute_until = None

    def update(self, score: float, now: float) -> Logged | None:
        seconds, self._last = (now - self._last if self._last is not None else 0.0), now
        if self._mute_until is not None and now < self._mute_until:
            return None
        if score >= self.threshold:
            self.wakes += 1
            self._near.update(score, seconds)
            self._mute_until = now + AFTER_WAKE_S
            return self._record(True, f"WAKE #{self.wakes}   score {score:.2f}")
        peak = self._near.update(score, seconds)
        if peak is None:
            return None
        self.near_misses += 1
        return self._record(False, f"close      peak {peak:.2f} (needs {self.threshold:.2f})")

    def _record(self, woke: bool, text: str) -> Logged:
        line = f"{time.strftime('%H:%M:%S')}  {text}"
        self.history.append(line)
        return Logged(woke, line)


def meter(level: float, score: float, threshold: float, speech: bool, width: int = 20) -> str:
    mark = min(width - 1, int(threshold * width))
    filled = int(min(1.0, score) * width)
    bar = "".join("|" if i == mark else ("#" if i < filled else " ") for i in range(width))
    level_bar = "#" * int(min(1.0, level) * 10)
    return f"level [{level_bar:<10}] {'SPEECH' if speech else '      '}  wake [{bar}] {score:.2f}"


def mic_test(cfg: Config, root: Path) -> None:
    pair = pair_source(cfg, root).in_use()
    threshold = pair.threshold
    wake = wake_word_trigger(str(pair.model_path), threshold)
    verifier = None
    if cfg.wake.verify:
        from .verify import PhraseVerifier

        verifier = PhraseVerifier(wake.phrase, root / "models")
        verifier.use_check(pair.check)
    recorder = make_recorder(cfg, root, turn_model=False)
    recent = RecentAudio(pair.check_window_s)
    log = WakeLog(threshold)
    passed = 0
    checked = " Each wake is then double-checked by the speech recognizer." if verifier else ""
    print(
        f"Say '{wake.phrase}'. It wakes at {threshold:.2f} (the | on the wake bar); "
        f"near-misses above {log.close:.2f} are logged too.{checked} Ctrl+C to stop.\n"
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
                    line, answers = event.line, False
                    if event.woke:
                        wake.reset()
                        answers = True
                        if verifier:
                            outcome, heard = verifier.decide(recent.audio())
                            answers = outcome == ANSWER
                            label = {ANSWER: "passed", ASK: "ASK 'Did you call me?'", IGNORE: "REJECTED"}[outcome]
                            line += f"   {label}: heard '{heard}'"
                            if verifier.last_confidence is not None:
                                line += f" ({verifier.last_confidence:.2f})"
                        passed += answers
                    print("\r\033[K" + line + ("\a" if answers else ""), flush=True)
                # Hold the peak score for a second so short spikes stay readable.
                if score >= peak_hold or now > peak_until:
                    peak_hold, peak_until = score, now + 1.0
                level = float(abs(block).mean()) / 3000
                line = meter(level, peak_hold, threshold, recorder.is_speech(block))
                passed_text = f" (passed {passed})" if verifier else ""
                tally = f"wakes {log.wakes}{passed_text}  close {log.near_misses}"
                print(f"\r\033[K{line}   {tally}", end="", flush=True)
    finally:
        passed_text = f" ({passed} passed the check)" if verifier else ""
        print(f"\n{log.wakes} wakes{passed_text}, {log.near_misses} near-misses.")
