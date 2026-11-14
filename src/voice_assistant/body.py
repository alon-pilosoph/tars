"""TARS's body: what it hears its name and the room with, and speaks through. The conversation (assistant.py) asks
it for a few things only: wait for a wake, record what's said, play audio, stop playing, and throw away what's heard
while nobody listens. `LocalBody` is the Pi's: the wake word, the mic, the recorder and the speaker, with the echo
canceller between the last two.

What's played is audio, made by the voice before it gets here, and played as it comes: a reply starts while the brain
is still writing it. A play the body cuts short itself returns True, and the conversation takes it as the stop
button. A device that's gone raises AudioDeviceError, which ends TARS so systemd starts it again.
"""

from collections.abc import Callable, Iterable
from concurrent.futures import Future
from contextlib import nullcontext
from typing import NamedTuple, Protocol

import numpy as np

from .audio import Microphone, Speaker
from .recorder import PREROLL_BLOCKS, UtteranceRecorder
from .stt import TurnState
from .wake import Due, Trigger


class BodyGone(Exception):
    """A network body went away (closed, lost, or silent while it should be streaming): it ends that body's
    conversation, never TARS, unlike an AudioDeviceError (bodylink.py)."""


class Wake(NamedTuple):
    """What a wait for a wake ended on."""

    how: str | None = None  # None or verify.ANSWER: its name; verify.ASK: it sounded close; wake.DUE: something's due
    audio: np.ndarray | None = None  # what woke it, for speaker ID; None when there's nothing to go on
    after_name: Future[np.ndarray] | None = None  # on a confirmed wake, what followed the name: found on the side


NO_WAKE = Wake()


class Recorded(NamedTuple):
    pcm: bytes  # 16 kHz mono int16
    silence_s: float  # how long ago they stopped talking when the recording ended


class Body(Protocol):
    # What to call to mute it as its own mute button does (the speakerphone's, and its light); None: it has none.
    set_muted: Callable[[bool], None] | None

    @property
    def hears_while_speaking(self) -> bool:
        """Whether what it plays is taken back out of what it hears, so it can listen while it speaks."""

    def wait(self, due: Due) -> Wake:
        """Blocks until someone wants to talk, or `due` (asked often, and quick) says something's due."""

    def record(
        self,
        start_timeout_s: float | None = None,
        on_audio: Callable[[bytes], None] | None = None,
        on_pause: Callable[[bytes], None] | None = None,
        on_resume: Callable[[], None] | None = None,
        turn_state: Callable[[], TurnState] | None = None,
        stop: Callable[[], bool] | None = None,
        max_s: float | None = None,
        lead: np.ndarray | None = None,
        preroll_blocks: int = PREROLL_BLOCKS,
        tail_blocks: int = 2,
    ) -> Recorded | None:
        """One utterance, from the start of speech until the turn is over; None if nobody spoke in time, or `stop()`
        said to stop. The callbacks and limits are UtteranceRecorder.record's."""

    def play(
        self,
        audio: Iterable[bytes],
        sample_rate: int,
        on_first_audio: Callable[[], None] | None = None,
        listening: bool = False,
    ) -> bool:
        """Plays 16-bit mono PCM as it comes, and returns once its last sound has played: True if it was cut short.
        It doesn't hear itself meanwhile, unless `listening` (with hears_while_speaking): a recording goes on while
        it plays."""

    def stop(self) -> None:
        """From another thread: what's playing stops now; the next play plays as usual."""

    def discard(self) -> None:
        """What's heard is thrown away, for while nobody listens (muted, or held), and a device that's gone is still
        noticed."""

    def clear(self) -> None:
        """What was heard so far is old: listening starts from now."""


class LocalBody:
    """The Pi's body, or any machine's with a mic and a speaker. Typed questions have only the speaker: the rest may
    be None."""

    def __init__(
        self,
        mic: Microphone | None,
        speaker: Speaker,
        trigger: Trigger | None,
        recorder: UtteranceRecorder | None,
        set_muted: Callable[[bool], None] | None = None,
    ):
        self.mic = mic
        self.speaker = speaker
        self.trigger = trigger
        self.recorder = recorder
        self.set_muted = set_muted

    @property
    def hears_while_speaking(self) -> bool:
        return bool(self.mic and self.mic.echo and self.mic.echo.working)

    def wait(self, due: Due) -> Wake:
        how = self.trigger.wait(self.mic, due=due)
        return Wake(how, self.trigger.last_audio, self.trigger.after_name)

    def record(self, **kw) -> Recorded | None:
        pcm = self.recorder.record(self.mic, **kw)
        return None if pcm is None else Recorded(pcm, self.recorder.trailing_silence_s)

    def play(
        self,
        audio: Iterable[bytes],
        sample_rate: int,
        on_first_audio: Callable[[], None] | None = None,
        listening: bool = False,
    ) -> bool:
        with nullcontext() if listening or not self.mic else self.mic.paused():
            return bool(self.speaker.play_pcm_stream(audio, sample_rate, on_first_audio=on_first_audio))

    def stop(self) -> None:
        self.speaker.stop()

    def discard(self) -> None:
        self.mic.discard()

    def clear(self) -> None:
        self.mic.clear()
