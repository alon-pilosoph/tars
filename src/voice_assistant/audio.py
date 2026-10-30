"""Audio devices: one microphone stream read in 80 ms blocks, one speaker stream that stays open, and the 16 kHz WAV
format every recording is kept in. A device that only works at its own rate (the Anker PowerConf takes 48 kHz and
nothing else on Linux, where nothing converts for it) is opened at that rate, and TARS converts. A device that stops
working raises AudioDeviceError, which ends the process so a supervisor can restart it."""

import io
import math
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


def decode_recording(data: bytes) -> np.ndarray:
    """A recording in any format libsndfile reads (Telegram's voice messages are Opus in Ogg, at 48 kHz), as the
    16 kHz mono 16-bit samples every recording is kept in. ValueError if it can't be read."""
    import soundfile
    from scipy.signal import resample_poly

    try:
        samples, rate = soundfile.read(io.BytesIO(data), dtype="float32", always_2d=True)
    except soundfile.LibsndfileError as e:
        raise ValueError(f"can't read the recording ({e.error_string})") from None
    except (RuntimeError, TypeError) as e:
        raise ValueError(f"can't read the recording ({e})") from None
    mono = samples.mean(axis=1)
    if rate != SAMPLE_RATE:
        divisor = np.gcd(int(rate), SAMPLE_RATE)
        mono = resample_poly(mono, SAMPLE_RATE // divisor, int(rate) // divisor)
    return (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)


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
    raise NoDeviceError(f"No {kind} device matching {name!r}. {PLUG_IN}")


def device_names() -> tuple[list[str], int, int]:
    """Every device's name, by index, and the system's default input and output."""
    default_in, default_out = sd.default.device
    return [d["name"] for d in sd.query_devices()], default_in, default_out


def device_rate(device: int | None, kind: str, wanted: int) -> int:
    """The rate to open `device` (None: the system's default) at for `kind` ("input" or "output") audio: `wanted` if
    it takes it, its own default rate if not. One that can't be asked about at all gets `wanted`, so opening it says
    what's wrong."""
    check = sd.check_input_settings if kind == "input" else sd.check_output_settings
    try:
        check(device=device, samplerate=wanted, channels=1, dtype="int16")
        return wanted
    except sd.PortAudioError, ValueError:
        pass
    try:
        return int(sd.query_devices(device, kind)["default_samplerate"])
    except sd.PortAudioError, ValueError, KeyError:
        return wanted


class Resampler:
    """Converts a stream of audio from one rate to another a block at a time, with no seams between the blocks: the
    polyphase filter scipy's resample_poly designs (a Kaiser window over 10 zero crossings), keeping the input it still
    needs from one block to the next. Its output lags its input by about 10 zero crossings: under a millisecond."""

    def __init__(self, rate_in: int, rate_out: int):
        from scipy.signal import firwin

        divisor = math.gcd(rate_in, rate_out)
        self.up, self.down = rate_out // divisor, rate_in // divisor
        half = 10 * max(self.up, self.down)
        taps = firwin(2 * half + 1, 1 / max(self.up, self.down), window=("kaiser", 5.0)) * self.up
        self._per_phase = math.ceil(len(taps) / self.up)
        taps = np.pad(taps, (0, self._per_phase * self.up - len(taps)))
        # Row p holds the taps an output at phase p uses: taps[p], taps[p + up], ..., one per input sample back.
        self._phases = taps.reshape(self._per_phase, self.up).T
        self._kept = np.zeros(self._per_phase - 1)  # the input before this block that the next outputs still reach
        self._fed = 0  # input samples so far
        self._next = 0  # where the next output falls, in samples at up times the input's rate

    def __call__(self, block: np.ndarray) -> np.ndarray:
        """`block` (16-bit samples) at the input's rate, as 16-bit samples at the output's."""
        signal = np.concatenate([self._kept, block.astype(np.float64)])
        self._fed += len(block)
        last = self._fed - 1  # the newest input sample there is
        count = max(0, (last * self.up + self.up - 1 - self._next) // self.down + 1)
        at = self._next + self.down * np.arange(count)
        newest = at // self.up - (self._fed - len(signal))  # each output's newest input sample, in `signal`
        back = newest[:, None] - np.arange(self._per_phase)[None, :]
        out = np.einsum("ij,ij->i", signal[back], self._phases[at % self.up])
        self._next += self.down * count
        self._kept = signal[len(signal) - (self._per_phase - 1) :]
        return np.clip(np.rint(out), -32768, 32767).astype(np.int16)

    def flush(self) -> np.ndarray:
        """What's still in the filter once the input has ended."""
        return self(np.zeros(self._per_phase, np.int16))


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


class NoDeviceError(AudioDeviceError):
    """The device to open isn't there. Restarting won't bring it back, so the process exits for good; on the Pi,
    plugging the speakerphone in starts TARS again (deploy/99-tars-speakerphone.rules)."""


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


PLUG_IN = "Plug in the speakerphone, or set it in config.toml [audio] (--list-devices)."
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
        rate = device_rate(device, "input", SAMPLE_RATE)
        # A device at its own rate is read in 80 ms of its samples, and what they make at 16 kHz handed out in blocks.
        self._resample = Resampler(rate, SAMPLE_RATE) if rate != SAMPLE_RATE else None
        self._pending = np.zeros(0, np.int16)
        try:
            self._stream = sd.RawInputStream(
                samplerate=rate,
                blocksize=round(BLOCK_SAMPLES * rate / SAMPLE_RATE),
                device=device,
                channels=1,
                dtype="int16",
                callback=self._on_audio,
            )
        except sd.PortAudioError as e:  # none plugged in
            raise NoDeviceError(f"No microphone to open ({e}). {PLUG_IN}") from None

    def _on_audio(self, indata, frames, time_info, status) -> None:
        block = np.frombuffer(indata, dtype=np.int16).copy()
        if self._resample is None:
            self._take(block)
            return
        self._pending = np.concatenate([self._pending, self._resample(block)])
        while len(self._pending) >= BLOCK_SAMPLES:
            self._take(self._pending[:BLOCK_SAMPLES])
            self._pending = self._pending[BLOCK_SAMPLES:]

    def _take(self, block: np.ndarray) -> None:
        """One 80 ms block at 16 kHz."""
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

    def stop(self) -> None:
        """From another thread (a stop button): what's playing stops now, and the rest of it isn't taken; the next
        play_pcm_stream plays as usual."""
        with self._lock:
            self._stopped = True
            self._buffer.clear()

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
    stuttering. `sample_rate` is the audio's; a device that won't play it is opened at its own, and the audio converted
    as it arrives.
    """

    def __init__(self, device: int | None, sample_rate: int, prebuffer_s: float, echo: EchoCanceller | None = None):
        """`echo`: told everything played, silence too, so it can take it back out of what the mic hears."""
        self.sample_rate = sample_rate
        self._rate = device_rate(device, "output", sample_rate)  # the device's: what the buffer holds
        self._echo = echo
        self._prebuffer_bytes = int(prebuffer_s * self._rate) * 2
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._buffering = True
        self._input_done = True
        self._on_start: Callable[[], None] | None = None
        self._drained = threading.Event()
        self._stopped = False  # stop(): what's playing now ends here
        try:
            self._stream = sd.RawOutputStream(
                samplerate=self._rate, device=device, channels=1, dtype="int16", callback=self._fill
            )
        except sd.PortAudioError as e:
            raise NoDeviceError(f"No speaker to open ({e}). {PLUG_IN}") from None

    def __enter__(self) -> Self:
        self._stream.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stream.stop()
        self._stream.close()

    def _fill(self, outdata, frames, time_info, status) -> None:
        self._fill_from_buffer(outdata)
        if self._echo:  # every frame, silence too: the canceller needs them all, in time
            self._echo.played(bytes(outdata), self._rate)

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

    def stop(self) -> None:
        """From another thread (a stop button): what's playing stops now, and the rest of it isn't taken; the next
        play_pcm_stream plays as usual."""
        with self._lock:
            self._stopped = True
            self._buffer.clear()

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
            self._stopped = False
        resample = Resampler(sample_rate, self._rate) if sample_rate != self._rate else None
        pending, heard = b"", False
        try:
            for chunk in chunks:
                pending += chunk
                usable = len(pending) - len(pending) % 2
                if not heard:  # voices start with silence; skip it so TARS starts with its first sound
                    skip = silent_bytes(pending[:usable])
                    heard = skip < usable
                    pending, usable = pending[skip:], usable - skip
                ready = pending[:usable]
                if resample is not None:
                    ready = resample(np.frombuffer(ready, np.int16)).tobytes()
                with self._lock:
                    if self._stopped:
                        break
                    self._buffer += ready
                pending = pending[usable:]
            if resample is not None:
                with self._lock:
                    if not self._stopped:
                        self._buffer += resample.flush().tobytes()
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
                left_s = len(self._buffer) / 2 / self._rate
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
