"""The body link: a body on another machine (a second Pi, the Atom, a web page) that TARS hears and speaks through
over the network, while the conversation itself stays here, on the brain (docs/bodies.md).

A body only moves audio. It listens for the name (or a button), and when someone wants to talk it streams its
microphone up a WebSocket; the brain sends back the reply's audio and a few words of control (listen, stop, sleep).
Everything about taking turns (when someone's done, drafts in a pause, follow-ups, who's speaking) is the brain's,
the same code as for the Pi's own speakerphone: a network body is a `RemoteBody`, which is `LocalBody` with a
`RemoteMicrophone`, a `RemoteSpeaker` and a `RemoteTrigger` in place of the devices.

The wire (protocol 1). One WebSocket per body, opened by the body; text frames are JSON with a `type`, binary frames
are audio: up, 16 kHz mono 16-bit, any length (re-blocked here to 80 ms); down, mono 16-bit at `welcome`'s
`play_rate`.

    brain → challenge {nonce}                 body → hello {protocol, device, proof, kind, wake, echo_cancel, ...}
    brain → welcome {room, play_rate, max_listen_s}
    body → wake {how, score, preroll_ms}, then audio: the pre-roll (what had the name in it), then live
    brain → listen {on}   play {id, listening} + audio + play_end {id}   stop   sleep   mute {muted}   pong
    body → played {id, cut}   button {name}   gone_quiet   ping

`proof` is the nonce signed with the body's Ed25519 key (bodykeys.py): the key never crosses, and the brain keeps
only its public half. A wrong one closes the socket with no reason given, and an address that keeps failing waits
longer each time.

The reply's audio is sent as it's made, faster than it plays: a body must buffer at least 30 s of it
(MIN_BODY_BUFFER_S), and says `played` once the last of it has left its speaker.

Threads: the listener's thread for each connection reads the socket and feeds its `BodyConnection` (the mic's
queue, wakes, `played`); the conversation's thread calls the body (`read`, `play_pcm_stream`, `wait`); `stop()` may
come from any thread. A body that goes away raises `BodyGone` in whichever call is waiting on it. It's not an
`AudioDeviceError`: that ends TARS so systemd starts it again, and a laptop closing its lid mustn't.
"""

import json
import queue
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Protocol

import numpy as np

from .audio import BLOCK_SAMPLES, BLOCK_SECONDS, ECHO_TAIL_S, MIC_STALL_S, SPEAKER_STALL_S, silent_bytes
from .body import BodyGone, LocalBody
from .bodykeys import verify
from .verify import ANSWER, ASK, IGNORE
from .wake import DUE, Due

PROTOCOL = 1
WAKES = ("verified", "model", "none")  # hello's `wake`: what the body checks itself before a wake
HELLO_WAIT_S = 10.0  # how long a body has to answer the challenge
MAX_FRAME = 256 * 1024  # bytes: a frame bigger than this closes the connection (a second of audio is 32 KiB)
MAX_QUEUED_S = 30.0  # audio up that waits unread; past it, the oldest is dropped (a flood, or nobody reading)
MIN_BODY_BUFFER_S = 30.0  # what a body must be able to hold of a reply, which comes faster than it plays
MAX_LISTEN_S = 120.0  # welcome's max_listen_s: a conversation's cap on streaming, which the body enforces too
PREROLL_MAX_MS = 5000  # a wake's pre-roll longer than this is cut to its last part
FAIL_WAIT_S = 1.0  # after a failed hello from an address, each next one waits this much longer (up to FAIL_WAIT_MAX_S)
FAIL_WAIT_MAX_S = 30.0
WAKE_POLL_S = BLOCK_SECONDS  # how often a wait for a wake asks whether something's due


@dataclass
class Hello:
    """What a body said about itself when it connected."""

    device: str
    kind: str = ""
    wake: str = "none"  # WAKES
    echo_cancel: bool = False
    buttons: list[str] = field(default_factory=list)
    version: str = ""


class BodyConnection:
    """One connected body, as the conversation sees it: what it streamed, its wakes, its `played`, and a way to send.
    Fed by the connection's own thread (`take`); everything else is called from the conversation's."""

    def __init__(self, hello: Hello, send: Callable[[str | bytes], None], close: Callable[[], None]):
        self.hello = hello
        self._send, self._close = send, close
        self._send_lock = threading.Lock()
        self.blocks: queue.Queue[np.ndarray] = queue.Queue(maxsize=int(MAX_QUEUED_S / BLOCK_SECONDS))
        self._pending = np.zeros(0, np.int16)  # up audio not yet a whole block
        self.wakes: queue.Queue[tuple[str, float, int]] = queue.Queue()
        self.played: queue.Queue[tuple[int, bool]] = queue.Queue()
        self.buttons: queue.Queue[str] = queue.Queue()
        self.gone = threading.Event()
        self.quiet = threading.Event()  # the body stopped streaming at its own cap (gone_quiet)
        # Whether the body may stream: from its wake until it's sent to sleep. Audio otherwise is dropped as it comes,
        # so what follows a wake (the pre-roll) is never mixed with anything before it.
        self.awake = False

    # From the connection's thread

    def take(self, message: str | bytes) -> None:
        """One frame from the body. Raises ValueError for one that breaks the protocol, which closes it."""
        if isinstance(message, bytes):
            self._audio(message)
            return
        try:
            said = json.loads(message)
        except ValueError:
            raise ValueError("not JSON") from None
        if not isinstance(said, dict):
            raise ValueError("not an object")  # noqa: TRY004 - a protocol error, as the rest
        kind = said.get("type")
        if kind == "wake":
            how = said.get("how")
            if how not in ("word", "button"):
                raise ValueError(f"wake how {how!r}")
            preroll_ms = min(max(int(said.get("preroll_ms") or 0), 0), PREROLL_MAX_MS)
            self.quiet.clear()
            self.clear()
            self.awake = True
            self.wakes.put((how, float(said.get("score") or 0.0), preroll_ms))
        elif kind == "played":
            self.played.put((int(said.get("id", -1)), bool(said.get("cut"))))
        elif kind == "button":
            self.buttons.put(str(said.get("name", "")))
        elif kind == "gone_quiet":
            self.quiet.set()
        elif kind == "ping":
            self.send({"type": "pong"})
        # Anything else is a newer body's: ignored, as a newer brain's would be by it.

    def _audio(self, data: bytes) -> None:
        if len(data) % 2:
            raise ValueError("audio of an odd length")
        if not self.awake:
            return
        pcm = np.concatenate([self._pending, np.frombuffer(data, np.int16)])
        whole = len(pcm) // BLOCK_SAMPLES * BLOCK_SAMPLES
        for start in range(0, whole, BLOCK_SAMPLES):
            block = pcm[start : start + BLOCK_SAMPLES].copy()
            try:
                self.blocks.put_nowait(block)
            except queue.Full:  # nobody's reading, or a flood: the oldest goes
                try:
                    self.blocks.get_nowait()
                except queue.Empty:
                    pass
                self.blocks.put_nowait(block)
        self._pending = pcm[whole:]

    def lost(self) -> None:
        self.gone.set()

    # From the conversation's thread (send: from any)

    def send(self, message: dict | bytes) -> None:
        if self.gone.is_set():
            raise BodyGone(f"{self.hello.device} is gone")
        try:
            with self._send_lock:
                self._send(message if isinstance(message, bytes) else json.dumps(message))
        except Exception as e:  # noqa: BLE001 - the socket's own errors: closed, reset
            self.gone.set()
            raise BodyGone(f"{self.hello.device} is gone ({e!r})") from None

    def sleep(self) -> None:
        """Back to waiting for the name: the body is told, and what it streams from now on is dropped, with a wake it
        sent before it heard (it went to sleep on it: its audio stops)."""
        self.awake = False
        self.send({"type": "sleep"})
        self.clear()
        while True:
            try:
                self.wakes.get_nowait()
            except queue.Empty:
                break

    def clear(self) -> None:
        while True:
            try:
                self.blocks.get_nowait()
            except queue.Empty:
                break
        self._pending = np.zeros(0, np.int16)

    def close(self) -> None:
        self.gone.set()
        self._close()


class RemoteMicrophone:
    """The body's microphone: what it streams, in 80 ms blocks. Only read while the body is streaming (after a wake);
    a body that stops then, or goes away, raises BodyGone."""

    def __init__(self, link: BodyConnection):
        self.link = link
        self.echo = SimpleNamespace(working=link.hello.echo_cancel)  # hears_while_speaking: the body's own canceller
        self.latency = 0.0

    def read(self) -> np.ndarray:
        deadline = time.monotonic() + MIC_STALL_S
        while True:
            if self.link.gone.is_set():
                raise BodyGone(f"{self.link.hello.device} is gone")
            try:
                return self.link.blocks.get(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
            except queue.Empty:
                if self.link.quiet.is_set():
                    raise BodyGone(f"{self.link.hello.device} stopped streaming at its own limit") from None
                if time.monotonic() >= deadline:
                    raise BodyGone(f"{self.link.hello.device} sent nothing for {MIC_STALL_S:.0f}s") from None

    def clear(self) -> None:
        self.link.clear()

    def discard(self) -> None:
        """For while nobody listens: what came is dropped. A body asleep sends nothing, which is fine; one that's
        gone is said."""
        if self.link.gone.is_set():
            raise BodyGone(f"{self.link.hello.device} is gone")
        self.link.clear()

    @contextmanager
    def paused(self, tail_s: float = ECHO_TAIL_S) -> Iterator[None]:
        """While TARS speaks: the body stops streaming, and what it sent meanwhile is dropped."""
        self.link.send({"type": "listen", "on": False})
        try:
            yield
        finally:
            time.sleep(tail_s)
            self.link.clear()
            if not self.link.gone.is_set():
                self.link.send({"type": "listen", "on": True})


class RemoteSpeaker:
    """The body's speaker. A reply is sent as it's made; play_pcm_stream returns once the body says it has played.
    `sample_rate`: the voice's, which the body was told in `welcome`."""

    def __init__(self, link: BodyConnection, sample_rate: int):
        self.link, self.sample_rate = link, sample_rate
        self.latency = 0.0
        self._ids = iter(range(1, 2**31))
        self._stopped = threading.Event()

    def stop(self) -> None:
        """From any thread: what's playing stops now, there and here."""
        self._stopped.set()
        try:
            self.link.send({"type": "stop"})
        except BodyGone:
            pass  # nothing's playing on a body that's gone

    def play_pcm_stream(
        self,
        chunks: Iterable[bytes],
        sample_rate: int,
        on_first_audio: Callable[[], None] | None = None,
        listening: bool = False,
    ) -> bool:
        """Plays 16-bit mono PCM on the body from its first sound, and returns once it's played there: True if it was
        cut short (stop(), or at the body). Raises BodyGone if the body goes, or never says it played."""
        if sample_rate != self.sample_rate:
            raise ValueError(f"the body plays {self.sample_rate} Hz, got {sample_rate} Hz audio")
        play_id = next(self._ids)
        self._stopped.clear()
        while not self.link.played.empty():  # an old play's answer, after a stop
            self.link.played.get_nowait()
        self.link.send({"type": "play", "id": play_id, "listening": listening})
        sent, heard, pending = 0, False, b""
        try:
            for chunk in chunks:
                if self._stopped.is_set():
                    break
                pending += chunk
                usable = len(pending) - len(pending) % 2
                if not heard:  # voices start with silence: TARS starts with its first sound
                    skip = silent_bytes(pending[:usable])
                    heard = skip < usable
                    pending, usable = pending[skip:], usable - skip
                    if heard and on_first_audio:
                        on_first_audio()
                if usable:
                    self.link.send(pending[:usable])
                    sent += usable
                pending = pending[usable:]
        finally:
            if not self.link.gone.is_set():
                self.link.send({"type": "play_end", "id": play_id})
        return self._played(play_id, sent / 2 / sample_rate)

    def _played(self, play_id: int, seconds: float) -> bool:
        """Waits for the body's `played` for this play: as long as it takes to play, and SPEAKER_STALL_S more."""
        deadline = time.monotonic() + seconds + SPEAKER_STALL_S
        while True:
            left = deadline - time.monotonic()
            if self.link.gone.is_set():
                raise BodyGone(f"{self.link.hello.device} is gone")
            if left <= 0:
                raise BodyGone(f"{self.link.hello.device} never said it played (after {seconds:.1f}s of audio)")
            try:
                got, cut = self.link.played.get(timeout=min(0.1, left))
            except queue.Empty:
                continue
            if got == play_id:
                return cut or self._stopped.is_set()


class Checker(Protocol):
    """The double-check a `model` body's wake gets here, and what followed the name: verify.VerifiedTrigger's
    `confirm` and `find_after_name`, with its `last_audio` and `after_name` after a confirm."""

    last_audio: np.ndarray | None
    after_name: Future[np.ndarray] | None

    def confirm(self, audio: np.ndarray, score: float) -> str: ...

    def find_after_name(self, audio: np.ndarray) -> Future[np.ndarray]: ...


class RemoteTrigger:
    """Waits for the body's `wake`. What the body checked itself (hello's `wake`) decides what's checked here:
    `verified`, nothing (the pre-roll is still kept, for speaker ID, and where the name ends found); `model`, the
    double-check, as the Pi's own wake word gets (`checker`); `none`, a button: nothing to check or keep."""

    last_audio: np.ndarray | None = None
    after_name: Future[np.ndarray] | None = None

    def __init__(self, link: BodyConnection, checker: Checker | None = None):
        self.link, self.checker = link, checker

    def wait(self, mic: RemoteMicrophone, due: Due | None = None) -> str | None:
        """ANSWER (or None, for a button) once someone wants to talk, ASK when the wake sounded close, DUE when `due`
        says something's due. The body is sent back to sleep first: whatever was before is over."""
        self.last_audio = self.after_name = None
        self.link.sleep()
        while True:
            try:
                how, score, preroll_ms = self.link.wakes.get(timeout=WAKE_POLL_S)
            except queue.Empty:
                if self.link.gone.is_set():
                    raise BodyGone(f"{self.link.hello.device} is gone") from None
                if due and due():
                    return DUE
                continue
            preroll = _preroll(mic, preroll_ms)
            if how == "button" or self.link.hello.wake == "none":
                return None
            if self.link.hello.wake == "verified" or self.checker is None:
                self.last_audio = preroll
                self.after_name = self.checker.find_after_name(preroll) if self.checker and len(preroll) else None
                return ANSWER
            outcome = self.checker.confirm(preroll, score)
            if outcome == IGNORE:
                self.link.sleep()
                continue
            self.last_audio, self.after_name = self.checker.last_audio, self.checker.after_name
            return outcome if outcome in (ANSWER, ASK) else ANSWER


def _preroll(mic: RemoteMicrophone, preroll_ms: int) -> np.ndarray:
    """The pre-roll that follows a wake: its first `preroll_ms`, in whole blocks."""
    blocks = round(preroll_ms / 1000 / BLOCK_SECONDS)
    return np.concatenate([mic.read() for _ in range(blocks)]) if blocks else np.zeros(0, np.int16)


class RemoteBody(LocalBody):
    """A network body: LocalBody's turn-taking with the body's microphone, speaker and wake. Its play tells the body
    whether to keep listening while it plays (only one that cancels its own echo)."""

    def __init__(self, link: BodyConnection, speaker_rate: int, recorder, checker: Checker | None = None):
        mic = RemoteMicrophone(link)
        super().__init__(mic, RemoteSpeaker(link, speaker_rate), RemoteTrigger(link, checker), recorder)
        self.link = link

    def play(self, audio, sample_rate, on_first_audio=None, listening=False) -> bool:
        if listening and self.hears_while_speaking:
            return bool(self.speaker.play_pcm_stream(audio, sample_rate, on_first_audio, listening=True))
        with self.mic.paused():
            return bool(self.speaker.play_pcm_stream(audio, sample_rate, on_first_audio))


class BodyListener:
    """The brain's door for network bodies: a threaded WebSocket server. A body is let in once it proves its device
    key; then it's handed to whoever waits for that device (`connected`), and the connection's thread feeds it until
    it goes. A newer connection from the same device replaces the older one.

    `key_for(device)`: the device's public key, or None for one that isn't paired (or was revoked). `allowed(address)`:
    whether an address may connect at all (the home network, for plain ws://). `welcome`: what's told a body that's
    let in (room, play_rate, max_listen_s). `seen(device)`: called when one is let in."""

    def __init__(
        self,
        key_for: Callable[[str], bytes | None],
        welcome: Callable[[Hello], dict],
        allowed: Callable[[str], bool] = lambda address: True,
        seen: Callable[[str], None] = lambda device: None,
        host: str = "0.0.0.0",  # nosec B104 - a body dials in from another machine; allowed() decides who
        port: int = 0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._key_for, self._welcome, self._allowed, self._seen = key_for, welcome, allowed, seen
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._bodies: dict[str, BodyConnection] = {}
        self._arrived = threading.Condition(self._lock)
        self._failures: dict[str, int] = {}
        from websockets.sync.server import serve

        self._server = serve(self._handle, host, port, max_size=MAX_FRAME, compression=None)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="body-listener")

    def start(self) -> BodyListener:
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        with self._lock:
            bodies = list(self._bodies.values())
        for body in bodies:
            body.close()

    def connected(self, device: str, timeout: float | None = None) -> BodyConnection | None:
        """The device's connection once it's here (and not gone); None if it doesn't come within `timeout`."""
        deadline = None if timeout is None else self._clock() + timeout
        with self._arrived:
            while True:
                link = self._bodies.get(device)
                if link is not None and not link.gone.is_set():
                    return link
                left = None if deadline is None else deadline - self._clock()
                if left is not None and left <= 0:
                    return None
                self._arrived.wait(left if left is not None else 1.0)

    def any_connected(self, timeout: float | None = None) -> BodyConnection | None:
        """The body that connected last (and is still here), once there's one; None if none comes within `timeout`.
        Until rooms (P4), a brain holds conversations with one body at a time."""
        deadline = None if timeout is None else self._clock() + timeout
        with self._arrived:
            while True:
                here = [link for link in self._bodies.values() if not link.gone.is_set()]
                if here:
                    return here[-1]
                left = None if deadline is None else deadline - self._clock()
                if left is not None and left <= 0:
                    return None
                self._arrived.wait(left if left is not None else 1.0)

    def _handle(self, ws) -> None:
        address = str(ws.remote_address[0]) if ws.remote_address else ""
        if not self._allowed(address):
            ws.close()
            return
        with self._lock:
            failed = self._failures.get(address, 0)
        if failed:
            self._sleep(min(FAIL_WAIT_S * failed, FAIL_WAIT_MAX_S))
        hello = self._shake_hands(ws)
        if hello is None:
            with self._lock:
                self._failures[address] = self._failures.get(address, 0) + 1
            ws.close()
            return
        with self._lock:
            self._failures.pop(address, None)
        link = BodyConnection(hello, ws.send, ws.close)
        try:
            ws.send(json.dumps({"type": "welcome", **self._welcome(hello)}))
        except Exception:  # noqa: BLE001 - it went at once: nothing to hand over
            return
        with self._arrived:
            old = self._bodies.get(hello.device)
            self._bodies[hello.device] = link
            self._arrived.notify_all()
        if old is not None:
            old.close()
        print(f"(body {hello.device} ({hello.kind or 'unknown'}) connected from {address})", flush=True)
        try:
            self._seen(hello.device)
        except Exception as e:  # noqa: BLE001 - only the record of it
            print(f"(body {hello.device}: couldn't note it connected: {e!r})", flush=True)
        try:
            for message in ws:
                link.take(message)
        except ValueError as e:
            print(f"(body {hello.device}: {e}; closed)", flush=True)
        except Exception as e:  # noqa: BLE001 - the socket's own: closed, reset, too big
            print(f"(body {hello.device}: the connection ended: {type(e).__name__})", flush=True)
        finally:
            link.lost()
            ws.close()
            with self._arrived:
                if self._bodies.get(hello.device) is link:
                    del self._bodies[hello.device]
                self._arrived.notify_all()
            print(f"(body {hello.device} went away)", flush=True)

    def _shake_hands(self, ws) -> Hello | None:
        """The challenge, and the body's hello: who it is, and its proof. None for anything wrong, without saying
        what."""
        nonce = secrets.token_hex(16)
        try:
            ws.send(json.dumps({"type": "challenge", "nonce": nonce}))
            said = json.loads(ws.recv(timeout=HELLO_WAIT_S))
        except Exception:  # noqa: BLE001 - silent, gone, not JSON, binary: no hello
            return None
        if not isinstance(said, dict) or said.get("type") != "hello" or said.get("protocol") != PROTOCOL:
            return None
        device, given = said.get("device"), said.get("proof")
        if not isinstance(device, str) or not device or not isinstance(given, str):
            return None
        public = self._key_for(device)
        if public is None or not verify(public, nonce, given):
            return None
        wake = said.get("wake", "none")
        buttons = said.get("buttons") or []
        return Hello(
            device,
            kind=str(said.get("kind", ""))[:40],
            wake=wake if wake in WAKES else "none",
            echo_cancel=bool(said.get("echo_cancel")),
            buttons=[str(b)[:20] for b in buttons][:10] if isinstance(buttons, list) else [],
            version=str(said.get("version", ""))[:40],
        )


def welcome(room: str, play_rate: int, max_listen_s: float = MAX_LISTEN_S) -> dict:
    return {"room": room, "play_rate": play_rate, "max_listen_s": max_listen_s}
