"""An answer prepared while TARS waits to be sure you've finished: start early, speak late.

A short pause starts one in the background while the recording goes on. If you carry on talking it's thrown away;
it's only played, logged and allowed to send anything once your turn is confirmed over. At worst an early start
costs a wasted request, never a wrong answer.
"""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Generic, TypeVar

T = TypeVar("T")


class Draft(Generic[T]):
    """Runs `prepare` on `pool`, then waits there until it's taken and done, or cancelled; `discard` then undoes it.

    Give every draft the same single-thread pool: the next one only starts once the last is settled, so two never
    work on the conversation at once.
    """

    def __init__(self, pool: ThreadPoolExecutor, prepare: Callable[["Draft[T]"], T], discard: Callable[[T], None]):
        self.cancelled = False
        self._ready, self._settled = threading.Event(), threading.Event()
        self._result: T | None = None
        self._error: Exception | None = None
        self._discard = discard
        pool.submit(self._run, prepare)

    def _run(self, prepare: Callable[["Draft[T]"], T]) -> None:
        try:
            self._result = prepare(self)
        except Exception as e:  # noqa: BLE001 - handed to whoever takes the draft
            self._error = e
        self._ready.set()
        self._settled.wait()
        if self.cancelled and self._result is not None:
            self._discard(self._result)

    def take(self) -> T:
        """The prepared answer, once it's ready. Call done() after using it."""
        self._ready.wait()
        if self._error:
            raise self._error
        return self._result

    def done(self) -> None:
        self._settled.set()

    def cancel(self) -> None:
        """They're still talking: throw it away (in the background; this doesn't wait)."""
        self.cancelled = True
        self._settled.set()
