"""The assistant's side of a recording session (recording.py): takes asked for over control.py, recorded on the
assistant's own thread, which owns the mic.

While a session is open TARS is held: it doesn't listen for its name (a prompt can say "hey TARS"), and what's due
is still said between takes. Each session holds it on its own (by its id): one leaving or finishing doesn't let go
of another still open. A hold lapses HOLD_S after its page last asked for anything, so a page left open doesn't keep
TARS deaf. A take asked for while TARS is busy with a conversation is refused rather than kept waiting.
"""

import base64
import itertools
import queue
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .audio import SAMPLE_RATE
from .control import Refused

HOLD_S = 180.0
PICKUP_S = 3.0  # how long a take waits for the assistant to come back from a conversation before it's refused
STOP_WAIT_S = 3.0  # after a tap, how long the take may take to end (the recorder notices within a block)
KEPT = 4  # the takes remembered for the page to fetch: the current one and a few before it
LEVEL_GAIN = 6.0  # a block's loudness (RMS of full scale) times this is the meter's 0-1; speech sits around 0.3-1

BUSY, MUTED, GONE = "busy", "muted", "gone"


@dataclass
class Take:
    id: int
    holder: str = ""  # the session holding TARS for it
    started: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    cancelled: bool = False
    t0: float | None = None
    seconds: float = 0.0
    level: float = 0.0
    pcm: bytes | None = None
    stopped: str = ""  # recording.SILENCE | TAP | NOBODY, once done


def level(pcm: bytes) -> float:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    return float(min(1.0, np.sqrt((x**2).mean()) * LEVEL_GAIN)) if len(x) else 0.0


class TakeDesk:
    """Between the control thread, which asks for takes and their state, and the assistant's thread, which records
    them (Assistant._wait_held)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._queue: queue.Queue[Take] = queue.Queue()
        # Ids start somewhere new each run, so one from before a restart is never another take's: the web UI, which
        # keeps running, remembers takes by it. Well under 2**53, for the page's JavaScript.
        self._ids = itertools.count(secrets.randbits(40) + 1)
        self._takes: OrderedDict[int, Take] = OrderedDict()
        self._holds: dict[str, float] = {}  # each session holding TARS: until when

    # The control thread's side.

    def hold(self, holder: str = "", seconds: float = HOLD_S) -> None:
        with self._lock:
            self._holds[holder] = self._clock() + seconds

    def release(self, holder: str = "") -> None:
        """`holder` lets go; TARS stays held while any other session still holds it."""
        with self._lock:
            self._holds.pop(holder, None)

    def held(self) -> bool:
        now = self._clock()
        with self._lock:
            self._holds = {h: until for h, until in self._holds.items() if until > now}
            return bool(self._holds)

    def ask(self, holder: str = "", pickup_s: float = PICKUP_S) -> Take:
        """A new take, once its beep has played and it's recording; Refused(BUSY) if the assistant didn't come to it
        within `pickup_s` (it's in a conversation)."""
        self.hold(holder)
        take = Take(next(self._ids), holder)
        with self._lock:
            self._takes[take.id] = take
            while len(self._takes) > KEPT:
                self._takes.popitem(last=False)
        self._queue.put(take)
        if not take.started.wait(pickup_s):
            with self._lock:
                if not take.started.is_set():
                    take.cancelled = True
            if take.cancelled:
                raise Refused(BUSY)
        return take

    def find(self, take_id: int) -> Take:
        with self._lock:
            take = self._takes.get(take_id)
        if take is None:
            raise Refused(GONE)
        return take

    def state(self, take: Take) -> dict:
        seconds = take.seconds if take.done.is_set() else self._clock() - (take.t0 or self._clock())
        return {
            "ok": True,
            "id": take.id,
            "state": "done" if take.done.is_set() else "recording",
            "seconds": round(seconds, 2),
            "level": 0.0 if take.done.is_set() else round(take.level, 2),
        }

    def finish(self, take_id: int, wait_s: float = STOP_WAIT_S) -> dict:
        """Stops the take if it's still going, and answers with its audio: base64 PCM, or None if nobody spoke."""
        take = self.find(take_id)
        self.hold(take.holder)
        take.stop.set()
        if not take.done.wait(wait_s):
            raise Refused(BUSY)
        return {
            **self.state(take),
            "pcm": base64.b64encode(take.pcm).decode() if take.pcm else None,
            "stopped": take.stopped,
        }

    # The assistant's side.

    def next(self, timeout_s: float) -> Take | None:
        """The next take to record, marked started (the caller beeps first, then sets `t0`); None if none came."""
        try:
            take = self._queue.get(timeout=timeout_s)
        except queue.Empty:
            return None
        with self._lock:
            if take.cancelled:
                return None
        return take

    def recording(self, take: Take) -> bool:
        """The beep has played: the take records from now, and its asker is answered. False if the asker gave up
        meanwhile (it was told TARS was busy), and the take isn't recorded."""
        with self._lock:
            if take.cancelled:
                return False
            take.t0 = self._clock()
            take.started.set()
            return True

    def ended(self, take: Take, pcm: bytes | None, stopped: str) -> None:
        take.pcm, take.stopped = pcm, stopped
        take.seconds = len(pcm) / 2 / SAMPLE_RATE if pcm else self._clock() - (take.t0 or self._clock())
        take.done.set()
