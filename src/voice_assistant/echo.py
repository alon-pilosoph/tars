"""Echo cancellation: what TARS plays is taken back out of what the mic hears, so the mic can stay open while TARS
makes a sound. WebRTC's echo canceller (AEC3, the one in browsers' video calls), through LiveKit's SDK, the optional
extra "echo" ([audio] echo_cancel).

The speaker hands over everything it gives the device, silence included (played()), and the mic everything it hears
(heard()), both from their audio callbacks, in 10 ms frames. The canceller learns the room's echo from the two, so it
needs a second or so of TARS talking before it's at its best; it keeps what it learned between replies.
"""

import difflib
import re
import threading

import numpy as np

FRAME_S = 0.01  # the canceller works in 10 ms frames
MAX_DELAY_S = 0.5


class EchoCanceller:
    def __init__(self, mic_rate: int):
        from livekit import rtc  # only with [audio] echo_cancel: the optional extra "echo"

        self._rtc = rtc
        self._apm = rtc.AudioProcessingModule(echo_cancellation=True, high_pass_filter=True)
        self._lock = threading.Lock()
        self._mic_rate = mic_rate
        self._mic_frame = int(mic_rate * FRAME_S)
        self._played = bytearray()  # less than a frame, waiting for the rest
        self._delay_ms = 0
        self.working = True

    def set_delay(self, seconds: float) -> None:
        """How long a sound takes from the speaker's callback to the mic's: the two devices' latencies. A hint: the
        canceller finds the exact delay itself, within MAX_DELAY_S."""
        self._delay_ms = round(min(MAX_DELAY_S, max(0.0, seconds)) * 1000)

    def _failed(self, e: Exception) -> None:
        if self.working:
            print(f"(echo cancellation failed, off until TARS restarts: {e!r})")
        self.working = False

    def played(self, pcm: bytes, sample_rate: int) -> None:
        """What the speaker is handing the device (16-bit mono), from its callback."""
        if not self.working:
            return
        frame = int(sample_rate * FRAME_S) * 2
        with self._lock:
            self._played += pcm
            try:
                while len(self._played) >= frame:
                    chunk = bytes(self._played[:frame])
                    del self._played[:frame]
                    self._apm.process_reverse_stream(self._rtc.AudioFrame(chunk, sample_rate, 1, frame // 2))
            except Exception as e:  # noqa: BLE001 - from the speaker's callback, which must never raise
                self._failed(e)

    def heard(self, block: np.ndarray) -> np.ndarray:
        """A block from the mic (16-bit mono, a whole number of 10 ms frames), with TARS's own sound taken out."""
        if not self.working:
            return block
        out = np.empty_like(block)
        n = self._mic_frame
        with self._lock:
            try:
                for i in range(0, len(block) - n + 1, n):
                    self._apm.set_stream_delay_ms(self._delay_ms)
                    frame = self._rtc.AudioFrame(block[i : i + n].tobytes(), self._mic_rate, 1, n)
                    self._apm.process_stream(frame)
                    out[i : i + n] = np.frombuffer(bytes(frame.data), np.int16)
            except Exception as e:  # noqa: BLE001 - from the mic's callback, which must never raise
                self._failed(e)
                return block
        tail = len(block) - len(block) % n
        out[tail:] = block[tail:]  # never with BLOCK_SAMPLES, which is 8 frames
        return out


WORD = re.compile(r"[\w']+")
LIKE = 0.5


def without_echo(text: str, said: str | None) -> str:
    """`text` with what TARS was saying while it was heard taken off its start, if a trace of it got through:
    "Yes, Alon? What time is it" -> "What time is it", and "Yes, Alan? ..." too. Every word of `said` has to be
    there, in order, near enough; a one-word greeting goes only if it's all that was heard ("Yes, turn on the
    lights" keeps its "Yes")."""
    if not said:
        return text
    words = [w.lower() for w in WORD.findall(said)]
    heard = list(WORD.finditer(text))
    if len(heard) < len(words) or (len(words) == 1 and len(heard) > 1):
        return text
    for word, match in zip(words, heard, strict=False):
        if difflib.SequenceMatcher(None, word, match.group().lower()).ratio() < LIKE:
            return text
    return text[heard[len(words) - 1].end() :].lstrip(" ,.?!;:-")
