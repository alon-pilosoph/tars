"""Audio devices: one microphone stream read in 80 ms blocks, one speaker stream that stays open, and the 16 kHz WAV
format every recording is kept in. A device that stops working raises AudioDeviceError, which ends the process so a
supervisor can restart it."""

import io
import queue
import threading
import time
import wave
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Self

import numpy as np
import sounddevice as sd

if TYPE_CHECKING:
    from .echo import EchoCanceller

SAMPLE_RATE = 16_000
BLOCK_SAMPLES = 1280  # 80 ms, the frame size openWakeWord is built around
BLOCK_SECONDS = BLOCK_SAMPLES / SAMPLE_RATE
# -50 dBFS: inaudible, like a voice's trailing padding and the effect's ring-out.
INAUDIBLE = 100


def wav_bytes(pcm: bytes | np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm if isinstance(pcm, bytes) else np.asarray(pcm, dtype=np.int16).tobytes())
    return buf.getvalue()


def save_wav(path: Path, pcm: bytes | np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav_bytes(pcm))


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as f:
        return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)


def _loud(pcm: bytes | bytearray) -> np.ndarray:
    return np.flatnonzero(np.abs(np.frombuffer(pcm, np.int16).astype(np.int32)) > INAUDIBLE)


def audible_bytes(pcm: bytes | bytearray) -> int:
    """Byte length of 16-bit PCM up to its last audible sample."""
    loud = _loud(pcm)
    return 2 * (int(loud[-1]) + 1) if len(loud) else 0


def silent_bytes(pcm: bytes | bytearray) -> int:
    """Byte length of 16-bit PCM before its first audible sample (all of it, if none is)."""
    loud = _loud(pcm)
    return 2 * int(loud[0]) if len(loud) else len(pcm)


def find_device(name: str, kind: str) -> int | None:
    """By name substring, so config survives index changes across machines and boots."""
    if not name:
        return None
    channels_key = "max_input_channels" if kind == "input" else "max_output_channels"
    for index, device in enumerate(sd.query_devices()):
        if name.lower() in device["name"].lower() and device[channels_key] > 0:
            return index
    raise SystemExit(f"No {kind} device matching {name!r}. Run with --list-devices to see what's available.")


def device_names() -> tuple[list[str], int, int]:
    """Every device's name, by index, and the system's default input and output."""
    default_in, default_out = sd.default.device
    return [d["name"] for d in sd.query_devices()], default_in, default_out


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


class AudioDeviceError(RuntimeError):
    """A device stopped working (unplugged, or the driver died). Not recoverable in-process: exit and restart."""


class MicrophoneError(AudioDeviceError):
    pass


def chime(sample_rate: int) -> bytes:
    """Two soft rising notes, played before a reminder so its first words don't land in silence."""
    parts = []
    for freq, seconds in ((660.0, 0.14), (880.0, 0.22)):
        t = np.arange(int(sample_rate * seconds)) / sample_rate
        envelope = np.minimum(1.0, t / 0.01) * np.exp(-t * 9)  # a quick attack, then it rings out
        parts.append(0.3 * envelope * np.sin(2 * np.pi * freq * t))
    parts.append(np.zeros(int(sample_rate * 0.15)))
    return (np.concatenate(parts) * 32767).astype(np.int16).tobytes()


class SpeakerError(AudioDeviceError):
    pass


# Blocks arrive every 80 ms, so this long without one means the device is gone.
MIC_STALL_S = 3.0
# Playback that's this far behind the audio it was given has stopped: the device no longer asks for more.
SPEAKER_STALL_S = 3.0


class Microphone:
    def __init__(self, device: int | None, echo: EchoCanceller | None = None):
        """`echo`: takes TARS's own sound out of what's heard, so the mic can stay open while TARS makes one."""
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self._muted = False
        self.echo = echo
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SAMPLES,
            device=device,
            channels=1,
            dtype="int16",
            callback=self._on_audio,
        )

    def _on_audio(self, indata, frames, time_info, status) -> None:
        block = np.frombuffer(indata, dtype=np.int16).copy()
        if self.echo:  # even while muted, so it keeps learning the room
            block = self.echo.heard(block)
        if self._muted:
            return
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            pass  # the consumer fell behind; dropping audio beats unbounded latency

    def __enter__(self) -> Self:
        self._stream.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stream.stop()
        self._stream.close()

    @property
    def latency(self) -> float:
        return self._stream.latency

    def read(self) -> np.ndarray:
        try:
            return self._queue.get(timeout=MIC_STALL_S)
        except queue.Empty:
            raise MicrophoneError(
                f"No audio from the microphone for {MIC_STALL_S:.0f}s; is it still connected?"
            ) from None

    def clear(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    @contextmanager
    def paused(self, tail_s: float = 0.3):
        """Ignores the mic while TARS makes noise, so it doesn't hear itself."""
        self._muted = True
        try:
            yield
        finally:
            time.sleep(tail_s)
            self.clear()
            self._muted = False


class Speaker:
    """One output stream kept open for the app's lifetime, playing silence when idle.

    Opening and closing a stream per reply makes the speakers pop. Network audio arrives in bursts, so playback waits
    for `prebuffer_s` of audio before starting, and if it runs dry mid-reply it pauses once to refill instead of
    stuttering.
    """

    def __init__(self, device: int | None, sample_rate: int, prebuffer_s: float, echo: EchoCanceller | None = None):
        """`echo`: told everything played, silence too, so it can take it back out of what the mic hears."""
        self.sample_rate = sample_rate
        self._echo = echo
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

    def __enter__(self) -> Self:
        self._stream.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stream.stop()
        self._stream.close()

    def _fill(self, outdata, frames, time_info, status) -> None:
        self._fill_from_buffer(outdata)
        if self._echo:  # every frame, silence too: the canceller needs them all, in time
            self._echo.played(bytes(outdata), self.sample_rate)

    def _fill_from_buffer(self, outdata) -> None:
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
                    self._buffering = True

    @property
    def latency(self) -> float:
        return self._stream.latency

    def play_pcm_stream(
        self,
        chunks: Iterable[bytes],
        sample_rate: int,
        on_first_audio: Callable[[], None] | None = None,
    ) -> None:
        """Play 16-bit mono PCM as it arrives, from its first sound, and return once its last sound has played."""
        if sample_rate != self.sample_rate:
            raise ValueError(f"Speaker runs at {self.sample_rate} Hz, got {sample_rate} Hz audio")
        with self._lock:
            self._buffer.clear()
            self._buffering = True
            self._input_done = False
            self._on_start = on_first_audio
            self._drained.clear()
        pending, heard = b"", False
        try:
            for chunk in chunks:
                pending += chunk
                usable = len(pending) - len(pending) % 2
                if not heard:  # voices start with silence; skip it so TARS starts with its first sound
                    skip = silent_bytes(pending[:usable])
                    heard = skip < usable
                    pending, usable = pending[skip:], usable - skip
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
                # Voices end with silence; dropping it frees the mic as soon as TARS stops making a sound.
                del self._buffer[audible_bytes(self._buffer) :]
                left_s = len(self._buffer) / 2 / self.sample_rate
        if not self._drained.wait(left_s + SPEAKER_STALL_S):
            with self._lock:
                self._buffer.clear()
            raise SpeakerError(
                f"The speaker stopped playing (nothing for {SPEAKER_STALL_S:.0f}s); is it still connected?"
            )
        # The last samples are still in the device's own buffer when the callback hands them over.
        time.sleep(self._stream.latency)

    def chime(self, freq: float = 880.0, duration_s: float = 0.12, volume: float = 0.3) -> None:
        t = np.linspace(0, duration_s, int(self.sample_rate * duration_s), endpoint=False)
        fade = np.minimum(1, np.minimum(t, duration_s - t) / 0.01)
        tone = (volume * np.sin(2 * np.pi * freq * t) * fade * 32767).astype(np.int16)
        self.play_pcm_stream([tone.tobytes()], self.sample_rate)
