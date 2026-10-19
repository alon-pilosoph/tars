"""The short lines TARS says, made ahead so they play at once. Two kinds are kept: the fixed lines ("Yes, Alon?",
"Did you call me?", the error lines), in memory and on disk, so they play even after a restart with the network down;
and the active reminders' lines, in memory until the reminder is done. Any other line is made when it's said."""

import hashlib
import threading
import time
from collections.abc import Callable, Collection
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

from .files import atomic_write
from .tts import Voice

# A line that failed to make ahead (the voice is down) is made ahead again only after this; one that's needed now is
# tried again at once.
RETRY_S = 60.0

type Audio = Future[list[bytes]]


class _Made(NamedTuple):
    audio: Audio
    tried_at: float


class Phrases:
    """`folder`: where the fixed lines' audio is kept for this voice, or None for nowhere. `clock`: seconds, for when
    a line that failed is tried again."""

    def __init__(self, voice: Voice, folder: Path | None = None, clock: Callable[[], float] = time.monotonic):
        self._voice, self._folder, self._clock = voice, folder, clock
        # One line at a time, on a thread of its own: making phrases never delays speaker ID, which an answer waits on.
        self._making = ThreadPoolExecutor(max_workers=1, thread_name_prefix="phrases")
        self._lock = threading.Lock()  # the reminders' lines are made ahead from the reminders' own thread
        self._fixed: dict[str, _Made] = {}
        self._reminders: dict[str, _Made] = {}

    def get(self, text: str, keep: bool = True, ahead: bool = False) -> Audio:
        """The line's audio: made already, being made, or queued now. `keep`: a fixed line; otherwise an active
        reminder's line is still found, and anything else is made just this once. `ahead`: not needed yet, so one
        that failed is tried again only RETRY_S after the last try, not at once."""
        if keep:
            return self._kept(self._fixed, text, ahead, save=True)
        with self._lock:
            reminder = text in self._reminders
        if reminder:
            return self._kept(self._reminders, text, ahead, save=False)
        return self._making.submit(self._make, text, save=False)

    def keep_only(self, lines: Collection[str]) -> None:
        """The active reminders' lines, each made ahead; the lines of reminders that are done are let go."""
        with self._lock:
            for text in [t for t in self._reminders if t not in lines]:
                del self._reminders[text]
        for text in lines:
            self._kept(self._reminders, text, ahead=True, save=False)

    def made_ahead(self, text: str) -> Audio | None:
        """A fixed line's audio, if it's been asked for."""
        with self._lock:
            made = self._fixed.get(text)
        return made.audio if made else None

    def __contains__(self, text: str) -> bool:
        with self._lock:
            return text in self._fixed or text in self._reminders

    def drain(self) -> None:
        """Waits until every line queued so far is made, or has failed."""
        self._making.submit(lambda: None).result()

    def _kept(self, lines: dict[str, _Made], text: str, ahead: bool, save: bool) -> Audio:
        with self._lock:
            made = lines.get(text)
            if made is not None and not self._try_again(made, ahead):
                return made.audio
            audio: Audio = Future()
            lines[text] = _Made(audio, self._clock())
        # A saved line is read here, rather than queued behind lines waiting on the network.
        if save and (saved := self._saved(text)):
            audio.set_result([saved])
        else:
            self._making.submit(self._make_into, audio, text, save)
        return audio

    def _try_again(self, made: _Made, ahead: bool) -> bool:
        if not made.audio.done() or made.audio.exception() is None:
            return False
        if not ahead:
            return True
        return self._clock() - made.tried_at >= RETRY_S

    def _make_into(self, audio: Audio, text: str, save: bool) -> None:
        try:
            audio.set_result(self._make(text, save))
        except Exception as e:  # noqa: BLE001 - whoever waits on the line gets it
            audio.set_exception(e)

    def _make(self, text: str, save: bool) -> list[bytes]:
        audio = list(self._voice.stream(text))
        if save and (path := self._path(text)):
            try:
                atomic_write(path, b"".join(audio))
            except OSError as e:  # only a head start for the next run
                print(f"(couldn't save {text!r} for next time: {e!r})")
        return audio

    def _path(self, text: str) -> Path | None:
        return self._folder / f"{hashlib.sha256(text.encode()).hexdigest()[:16]}.pcm" if self._folder else None

    def _saved(self, text: str) -> bytes | None:
        path = self._path(text)
        try:
            return (path.read_bytes() if path else None) or None
        except OSError:
            return None
