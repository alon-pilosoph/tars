import queue
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16_000
# 80 ms of audio: the frame size openWakeWord is built around.
BLOCK_SAMPLES = 1280
BLOCK_SECONDS = BLOCK_SAMPLES / SAMPLE_RATE


def find_device(name: str, kind: str) -> int | None:
    """Resolve a device by name substring, so config survives index changes across machines and boots."""
    if not name:
        return None
    channels_key = "max_input_channels" if kind == "input" else "max_output_channels"
    for index, device in enumerate(sd.query_devices()):
        if name.lower() in device["name"].lower() and device[channels_key] > 0:
            return index
    raise SystemExit(f"No {kind} device matching {name!r}. Run with --list-devices to see what's available.")


def list_devices() -> None:
    default_in, default_out = sd.default.device
    for index, device in enumerate(sd.query_devices()):
        roles = []
        if device["max_input_channels"] > 0:
            roles.append("in" + ("*" if index == default_in else ""))
        if device["max_output_channels"] > 0:
            roles.append("out" + ("*" if index == default_out else ""))
        print(f"{index:>3}  {'/'.join(roles):<9} {device['name']}")
    print("\n* = system default")


class MicrophoneError(RuntimeError):
    """The input device stopped delivering audio (unplugged, or the driver died)."""


# Blocks arrive every 80 ms, so this long without one means the device is gone.
MIC_STALL_S = 3.0


class Microphone:
    """One continuous 16 kHz mono stream. Everything downstream reads 80 ms blocks from the same queue."""

    def __init__(self, device: int | None):
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self._muted = False
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SAMPLES,
            device=device,
            channels=1,
            dtype="int16",
            callback=self._on_audio,
        )

    def _on_audio(self, indata, frames, time_info, status) -> None:
        if self._muted:
            return
        try:
            self._queue.put_nowait(np.frombuffer(indata, dtype=np.int16).copy())
        except queue.Full:
            pass  # Consumer fell behind; dropping audio beats unbounded latency.

    def __enter__(self) -> "Microphone":
        self._stream.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stream.stop()
        self._stream.close()

    def read(self) -> np.ndarray:
        try:
            return self._queue.get(timeout=MIC_STALL_S)
        except queue.Empty:
            raise MicrophoneError(f"No audio from the microphone for {MIC_STALL_S:.0f}s; is it still connected?")

    def clear(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    @contextmanager
    def paused(self, tail_s: float = 0.3):
        """Ignore the mic while we make noise, so the assistant doesn't hear itself."""
        self._muted = True
        try:
            yield
        finally:
            time.sleep(tail_s)
            self.clear()
            self._muted = False


class Speaker:
    """One output stream kept open for the app's lifetime, playing silence when idle.

    Opening and closing a stream per reply makes the speakers pop. Network audio also arrives
    in bursts, so playback waits for `prebuffer_s` of audio before starting, and if it runs dry
    mid-reply it pauses once to refill instead of stuttering.
    """

    def __init__(self, device: int | None, sample_rate: int, prebuffer_s: float):
        self.sample_rate = sample_rate
        self._prebuffer_bytes = int(prebuffer_s * sample_rate) * 2
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._buffering = True
        self._input_done = True
        self._on_start: Callable[[], None] | None = None
        self._drained = threading.Event()
        self._stream = sd.RawOutputStream(
            samplerate=sample_rate, device=device, channels=1, dtype="int16", callback=self._fill
        )

    def __enter__(self) -> "Speaker":
        self._stream.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stream.stop()
        self._stream.close()

    def _fill(self, outdata, frames, time_info, status) -> None:
        wanted = len(outdata)
        with self._lock:
            if self._buffering:
                if len(self._buffer) < self._prebuffer_bytes and not self._input_done:
                    outdata[:] = bytes(wanted)
                    return
                self._buffering = False
            n = min(wanted, len(self._buffer))
            outdata[:n] = self._buffer[:n]
            outdata[n:] = bytes(wanted - n)
            del self._buffer[:n]
            if n and self._on_start:
                self._on_start()
                self._on_start = None
            if not self._buffer:
                if self._input_done:
                    self._drained.set()
                elif n < wanted:
                    self._buffering = True  # Ran dry mid-reply: refill before resuming.

    def play_pcm_stream(
        self,
        chunks: Iterable[bytes],
        sample_rate: int,
        on_first_audio: Callable[[], None] | None = None,
    ) -> None:
        """Play 16-bit mono PCM as it arrives, and return once it has finished playing."""
        if sample_rate != self.sample_rate:
            raise ValueError(f"Speaker runs at {self.sample_rate} Hz, got {sample_rate} Hz audio")
        with self._lock:
            self._buffer.clear()
            self._buffering = True
            self._input_done = False
            self._on_start = on_first_audio
            self._drained.clear()
        pending = b""
        try:
            for chunk in chunks:
                pending += chunk
                usable = len(pending) - len(pending) % 2
                with self._lock:
                    self._buffer += pending[:usable]
                pending = pending[usable:]
        except BaseException:
            # The reply failed partway: cut the audio now, so nothing is still playing once the mic reopens.
            with self._lock:
                self._buffer.clear()
                self._input_done = True
            raise
        finally:
            with self._lock:
                self._input_done = True
        self._drained.wait()
        # The last samples are still in the device's own buffer when the callback hands them over.
        time.sleep(self._stream.latency)

    def chime(self, freq: float = 880.0, duration_s: float = 0.12, volume: float = 0.3) -> None:
        t = np.linspace(0, duration_s, int(self.sample_rate * duration_s), endpoint=False)
        fade = np.minimum(1, np.minimum(t, duration_s - t) / 0.01)
        tone = (volume * np.sin(2 * np.pi * freq * t) * fade * 32767).astype(np.int16)
        self.play_pcm_stream([tone.tobytes()], self.sample_rate)

    def error_tone(self) -> None:
        """Two falling notes: something went wrong, you'll need to ask again."""
        self.chime(freq=520.0, duration_s=0.12)
        self.chime(freq=350.0, duration_s=0.2)
