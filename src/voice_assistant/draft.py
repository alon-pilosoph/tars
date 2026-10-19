"""An answer prepared in the background after a short pause, while the recording goes on.

It is played, logged and allowed to send anything only once the turn is confirmed over; if the speaker carries on,
it is discarded. An early start costs at most a wasted request. Anything the answer does in the house waits for
go(): a draft may be thrown away, but a door it unlocked stays unlocked.
"""

import threading
from collections.abc import Callable

# Longer than any answer takes to play. Only a bug leaves a draft unsettled, and since drafts share one lock, an
# unbounded wait would block every later answer.
SETTLE_TIMEOUT_S = 600.0


class Draft[T]:
    """Runs `prepare` on its own thread, holding `turn` until kept or cancelled; a cancelled result is `discard`ed.

    Give every draft the same lock, so the next one starts only after the last is settled and two never work on the
    conversation at once.
    """

    def __init__(self, turn: threading.Lock, prepare: Callable[[Draft[T]], T], discard: Callable[[T], None]):
        self.cancelled = False
        self.going = False
        self._ready, self._settled, self._decided = threading.Event(), threading.Event(), threading.Event()
        self._result: T | None = None
        self._error: Exception | None = None
        self._discard = discard
        threading.Thread(target=self._run, args=(turn, prepare), daemon=True, name="draft").start()

    def _run(self, turn: threading.Lock, prepare: Callable[[Draft[T]], T]) -> None:
        with turn:
            self._work(prepare)

    def _work(self, prepare: Callable[[Draft[T]], T]) -> None:
        if not self.cancelled:
            try:
                self._result = prepare(self)
            except Exception as e:  # noqa: BLE001 - handed to whoever takes the draft
                self._error = e
        self._ready.set()
        if not self._settled.wait(SETTLE_TIMEOUT_S):
            print("(an answer draft was never used or thrown away; throwing it away)")
            self.cancelled = True
        if self.cancelled and self._result is not None:
            self._discard(self._result)

    def take(self) -> T:
        """Blocks until the answer is ready. The caller must then keep() or cancel() it."""
        self._ready.wait()
        if self._error:
            raise self._error
        assert self._result is not None
        return self._result

    def go(self) -> None:
        """The turn is over and this answer will be heard: what it does may be done (may_act)."""
        self.going = not self.cancelled
        self._decided.set()

    def may_act(self, timeout_s: float) -> bool:
        """From the answer's own thread: blocks until the turn is over (True: go ahead) or the draft is thrown away
        (False), at most `timeout_s`; False if neither happened by then."""
        self._decided.wait(timeout_s)
        return self.going and not self.cancelled

    def keep(self) -> None:
        self._settled.set()

    def cancel(self) -> None:
        """Returns at once; the draft's own thread discards the result."""
        self.cancelled = True
        self._decided.set()
        self._settled.set()
