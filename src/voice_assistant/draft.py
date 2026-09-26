"""An answer prepared while TARS waits to be sure you've finished: start early, speak late.

A short pause starts one in the background while the recording goes on. If you carry on talking it's thrown away;
it's only played, logged and allowed to send anything once your turn is confirmed over. At worst an early start
costs a wasted request, never a wrong answer.
"""

import threading
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")

# Longer than any answer takes to play. Only a bug leaves a draft unsettled; without a limit that would stop every
# later answer, since drafts take turns.
SETTLE_TIMEOUT_S = 600.0


class Draft(Generic[T]):
    """Runs `prepare` on its own thread, holding `turn` until it's kept or cancelled; a cancelled one is `discard`ed.

    Give every draft the same lock: the next one only starts once the last is settled, so two never work on the
    conversation at once. (A daemon thread per draft, so a pending one never holds up quitting.)
    """

    def __init__(self, turn: threading.Lock, prepare: Callable[["Draft[T]"], T], discard: Callable[[T], None]):
        self.cancelled = False
        self._ready, self._settled = threading.Event(), threading.Event()
        self._result: T | None = None
        self._error: Exception | None = None
        self._discard = discard
        threading.Thread(target=self._run, args=(turn, prepare), daemon=True, name="draft").start()

    def _run(self, turn: threading.Lock, prepare: Callable[["Draft[T]"], T]) -> None:
        with turn:
            self._work(prepare)

    def _work(self, prepare: Callable[["Draft[T]"], T]) -> None:
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
        """The prepared answer, once it's ready. Then keep() it, or cancel() it if it can't be used."""
        self._ready.wait()
        if self._error:
            raise self._error
        assert self._result is not None
        return self._result

    def keep(self) -> None:
        self._settled.set()

    def cancel(self) -> None:
        """Throw it away (in the background; this doesn't wait)."""
        self.cancelled = True
        self._settled.set()
