"""The body's side of the body link (docs/bodies.md): `voice-assistant --body ws://<brain>:8766` makes this machine's
microphone and speaker a body for TARS in another room. It hears the name itself, with the same wake word and
double-check as the Pi (so it tells the brain `wake: verified`), streams what's said once it has, and plays the
reply; everything else is the brain's. It needs no model keys and no database: only its key, from --body-add, in
.env as TARS_BODY_KEY.

`BodyProtocol` is the body's half of the protocol with no devices in it: fed what the brain sends and what happens
here (a wake, a block of audio, a playback ending), it decides what to send and what to play. It's the reference the
ESP32 and the web page are held to, and it keeps the rules a body keeps whatever the brain says:

- nothing is streamed before its own wake: `listen on` is honoured only in a conversation it opened;
- a conversation streams for at most `max_listen_s` (the brain's, or BODY_CEILING_S, whichever is lower), then it
  stops, says `gone_quiet`, and goes back to waiting for the name;
- muted, the wake word isn't even listened for.

`run_body` is the rest: the devices, the connection (made again, with a growing wait, whenever it drops), and one
line said when someone calls TARS while the brain can't be reached.
"""

import json
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio import BLOCK_SAMPLES, BLOCK_SECONDS, SAMPLE_RATE
from .bodykeys import parse_key, sign
from .bodylink import PROTOCOL

BODY_CEILING_S = 120.0  # the most a conversation streams, whatever the brain says: the body's own cap
KEY_NAME = "TARS_BODY_KEY"  # the body's key, in .env
RETRY_S = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)  # waits between tries to reach the brain, the last one repeated
UNREACHABLE = Path(__file__).with_name("assets") / "body_unreachable.wav"  # said when the brain can't be reached


class Player:
    """What plays the brain's replies: a playback `start`s, gets `audio`, and `end`s (its last audio is in); `stop`
    cuts what's playing. Once a playback has played, the player calls BodyProtocol.played."""

    def start(self, play_id: int) -> None: ...

    def audio(self, data: bytes) -> None: ...

    def end(self, play_id: int) -> None: ...

    def stop(self) -> None: ...


@dataclass
class Welcome:
    room: str
    play_rate: int
    max_listen_s: float


class BodyProtocol:
    """The body's half of the protocol. `send(message)`: a dict as a text frame, bytes as audio. `clock`: seconds,
    for the cap. Thread-safe: the connection's thread feeds `brain`, the microphone's `woke` and `heard`, the
    player's `played`."""

    def __init__(
        self, send: Callable[[dict | bytes], None], player: Player, clock: Callable[[], float] = time.monotonic
    ):
        self._send, self._player, self._clock = send, player, clock
        self._lock = threading.Lock()
        self.welcome: Welcome | None = None
        self.awake = False  # in a conversation this body opened, until the brain sends it to sleep (or the cap)
        self.listening = False  # streaming: awake, and the brain hasn't said `listen off`
        self.muted = False
        self.playing: int | None = None
        self._streamed_s = 0.0

    # What the brain sends

    def brain(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            with self._lock:
                playing = self.playing
            if playing is not None:
                self._player.audio(message)
            return
        said = json.loads(message)
        kind = said.get("type")
        if kind == "welcome":
            self.welcome = Welcome(str(said.get("room", "")), int(said["play_rate"]), float(said["max_listen_s"]))
        elif kind == "listen":
            with self._lock:
                # Only inside a conversation it opened: never from sleep, whatever the brain says.
                self.listening = bool(said.get("on")) and self.awake
        elif kind == "play":
            with self._lock:
                self.playing = int(said["id"])
            self._player.start(self.playing)
        elif kind == "play_end":
            self._player.end(int(said["id"]))
        elif kind == "stop":
            self._player.stop()
        elif kind == "sleep":
            with self._lock:
                self.awake = self.listening = False
        elif kind == "mute":
            with self._lock:
                self.muted = bool(said.get("muted"))
                if self.muted:
                    self.awake = self.listening = False

    # What happens here

    def woke(self, how: str, score: float, preroll: np.ndarray) -> bool:
        """Someone called TARS (`how`: word or button), with `preroll` the audio that had the name in it: the
        conversation starts, and the pre-roll goes up first. False if muted or not connected."""
        preroll = preroll[len(preroll) % BLOCK_SAMPLES :]  # whole 80 ms blocks, the newest: the brain reads it so
        with self._lock:
            if self.muted or self.welcome is None:
                return False
            self.awake = self.listening = True
            self._streamed_s = len(preroll) / SAMPLE_RATE
        preroll_ms = round(len(preroll) / SAMPLE_RATE * 1000)
        self._send({"type": "wake", "how": how, "score": round(score, 3), "preroll_ms": preroll_ms})
        if len(preroll):
            self._send(preroll.astype(np.int16).tobytes())
        return True

    def heard(self, block: np.ndarray) -> None:
        """A block from the microphone: sent if the body is streaming, until the conversation's cap."""
        with self._lock:
            if not self.listening:
                return
            self._streamed_s += len(block) / SAMPLE_RATE
            cap = min(BODY_CEILING_S, self.welcome.max_listen_s if self.welcome else BODY_CEILING_S)
            over = self._streamed_s > cap + 1e-6  # 0.08 s blocks add up a hair over the round number
            if over:
                self.awake = self.listening = False
        if over:
            self._send({"type": "gone_quiet"})
            return
        self._send(block.astype(np.int16).tobytes())

    def played(self, play_id: int, cut: bool) -> None:
        with self._lock:
            if self.playing == play_id:
                self.playing = None
        self._send({"type": "played", "id": play_id, "cut": cut})

    def lost(self) -> None:
        """The connection dropped: whatever was going on is over."""
        with self._lock:
            self.awake = self.listening = False
            self.welcome, self.playing = None, None
        self._player.stop()


def hello(device: str, seed: bytes, nonce: str, echo_cancel: bool, kind: str = "pi", version: str = "") -> dict:
    """The body's answer to the brain's challenge."""
    return {"type": "hello", "protocol": PROTOCOL, "device": device, "proof": sign(seed, nonce), "kind": kind,
            "wake": "verified", "echo_cancel": echo_cancel, "buttons": [], "version": version}  # fmt: skip


class SpeakerPlayer:
    """The brain's replies on this machine's speaker (audio.Speaker), each playback on a thread of its own."""

    def __init__(self, speaker, rate: int):
        self.speaker, self.rate = speaker, rate
        self.protocol: BodyProtocol | None = None
        self._chunks: queue.Queue[bytes | None] | None = None

    def start(self, play_id: int) -> None:
        chunks: queue.Queue[bytes | None] = queue.Queue()
        self._chunks = chunks

        def pieces() -> Iterator[bytes]:
            while (piece := chunks.get()) is not None:
                yield piece

        def play() -> None:
            try:
                cut = bool(self.speaker.play_pcm_stream(pieces(), self.rate))
            except Exception as e:  # noqa: BLE001 - said, and reported as cut so the brain carries on
                print(f"(body: couldn't play the reply: {e!r})", flush=True)
                cut = True
            if self.protocol:
                try:
                    self.protocol.played(play_id, cut)
                except Exception as e:  # noqa: BLE001 - the brain went meanwhile
                    print(f"(body: couldn't say it played: {e!r})", flush=True)

        threading.Thread(target=play, daemon=True, name=f"play-{play_id}").start()

    def audio(self, data: bytes) -> None:
        if self._chunks is not None:
            self._chunks.put(data)

    def end(self, play_id: int) -> None:
        if self._chunks is not None:
            self._chunks.put(None)

    def stop(self) -> None:
        self.speaker.stop()
        if self._chunks is not None:
            self._chunks.put(None)


def run_body(cfg, root: Path, url: str, key: str) -> None:
    """--body URL: this machine as a body for the brain at `url`, until stopped."""
    from websockets.sync.client import connect

    from .audio import Microphone, Speaker, find_device
    from .verify import ANSWER
    from .wiring import make_echo_canceller, make_trigger

    device, seed = parse_key(key)
    echo = make_echo_canceller(cfg)
    rate = 24000  # until the brain says its voice's rate
    speaker = Speaker(find_device(cfg.audio.output_device, "output"), rate, cfg.audio.playback_prebuffer_s, echo)
    connection: dict = {"ws": None}

    def send(message: dict | bytes) -> None:
        ws = connection["ws"]
        if ws is None:
            return
        try:
            ws.send(message if isinstance(message, bytes) else json.dumps(message))
        except Exception:  # noqa: BLE001, S110 - the reader notices it's gone, and the link is made again
            pass

    player = SpeakerPlayer(speaker, rate)
    protocol = BodyProtocol(send, player)
    player.protocol = protocol

    def keep_connected() -> None:
        tries = 0
        while True:
            try:
                with connect(url, max_size=None, compression=None, open_timeout=10) as ws:
                    nonce = json.loads(ws.recv(timeout=10))["nonce"]
                    ws.send(json.dumps(hello(device, seed, nonce, bool(echo and echo.working))))
                    connection["ws"] = ws
                    for message in ws:
                        known = protocol.welcome is not None
                        protocol.brain(message)
                        if not known and protocol.welcome is not None:
                            print(f"(body {device}: connected to the brain, room {protocol.welcome.room})", flush=True)
                            if protocol.welcome.play_rate != player.rate:  # every voice TARS has is 24 kHz
                                print(f"(body: the brain's voice is {protocol.welcome.play_rate} Hz; this body "
                                      f"plays {player.rate} Hz, so its replies won't play)", flush=True)  # fmt: skip
                            tries = 0
            except Exception as e:  # noqa: BLE001 - any failure to reach it, or a dropped link: tried again
                if not tries:
                    print(f"(body {device}: the brain can't be reached ({type(e).__name__}); trying again)", flush=True)
            connection["ws"] = None
            protocol.lost()
            time.sleep(RETRY_S[min(tries, len(RETRY_S) - 1)])
            tries += 1

    threading.Thread(target=keep_connected, daemon=True, name="body-link").start()
    with Microphone(find_device(cfg.audio.input_device, "input"), echo) as mic, speaker:
        if echo:
            echo.set_delay(mic.latency + speaker.latency)
        trigger, idle = make_trigger(cfg, False, root, None)
        print(f"(body {device}: {idle})", flush=True)
        while True:
            if protocol.muted:
                mic.discard()
                time.sleep(BLOCK_SECONDS)
                continue
            how = trigger.wait(mic, due=lambda: protocol.muted)
            if how != ANSWER:
                continue
            audio = trigger.last_audio if trigger.last_audio is not None else np.zeros(0, np.int16)
            if not protocol.woke("word", 1.0, audio):
                if protocol.welcome is None and UNREACHABLE.exists():
                    _say_unreachable(speaker)
                continue
            while protocol.awake:
                protocol.heard(mic.read())


def _say_unreachable(speaker) -> None:
    """The one line a body says when it's called and the brain can't be reached."""
    import soundfile as sf

    audio, rate = sf.read(UNREACHABLE, dtype="int16")
    if rate != speaker.sample_rate:
        print("(body: the brain can't be reached)", flush=True)
        return
    speaker.play_pcm_stream([audio.tobytes()], rate)
