"""A phone call as a body: `python -m voice_assistant.phone`, on the brain's own machine. calls.py holds its
conversation.

Twilio calls this small web server when the number rings, and it answers only a caller it's told to, who then enters a
PIN. The call's audio comes over a WebSocket (Twilio's Media Streams: 8 kHz mu-law, both ways), and goes to TARS over
the body link, let in by a one-time ticket as the Talk page is. So a call is never "at home": nothing said on it
unlocks a door, and nothing TARS says by itself goes to it.

Settings are in .env: TWILIO_AUTH_TOKEN, TARS_PHONE_URL (this server's public https:// address), TARS_PHONE_ALLOWED
(the caller's number, +E.164; commas between several), TARS_PHONE_PIN (6 digits or more), TARS_PHONE_PERSON (who the
PIN's holder is; empty: told by voice). Only /voice, /pin and /audio are served, on 127.0.0.1: a tunnel reaches it.
"""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import queue
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from xml.sax.saxutils import quoteattr

import numpy as np

from .audio import BLOCK_SAMPLES, Resampler
from .bodylink import PHONE, PROTOCOL

PHONE_RATE = 8000
UNHEARD_BLOCKS = 125  # 10 s of the caller, kept until TARS is there
FRAME_BYTES = 160  # 20 ms of mu-law
TOKEN_S = 30.0  # how long the audio stream has to open after the PIN
PIN_TRIES = 5  # wrong PINs before every call is refused, for LOCKED_S
LOCKED_S = 3600.0
MAX_CALL_S = 1860.0  # a little past calls.MAX_CALL_S, which says goodbye first
START_S = 5.0  # how long a new audio stream has to say which call it is
MAX_BODY = 16 * 1024


# Audio


def mulaw_decode(data: bytes) -> np.ndarray:
    u = ~np.frombuffer(data, np.uint8)
    size = (((u & 0x0F).astype(np.int32) << 3) + 0x84) << ((u & 0x70) >> 4).astype(np.int32)
    return np.where(u & 0x80, 0x84 - size, size - 0x84).astype(np.int16)


def mulaw_encode(pcm: np.ndarray) -> bytes:
    x = pcm.astype(np.int32)
    size = np.minimum(np.abs(x), 32635) + 0x84
    exponent = np.floor(np.log2(size)).astype(np.int32) - 7
    coded = np.where(x < 0, 0x80, 0) | (exponent << 4) | ((size >> (exponent + 3)) & 0x0F)
    return (~coded & 0xFF).astype(np.uint8).tobytes()


# Who may call


def signature(token: str, url: str, params: list[tuple[str, str]]) -> str:
    """Twilio's X-Twilio-Signature for a form posted to `url`."""
    said = url + "".join(k + v for k, v in sorted(set(params)))
    return base64.b64encode(hmac.new(token.encode(), said.encode(), hashlib.sha1).digest()).decode()


class Gate:
    """Who's let through: the PIN, locked after too many wrong ones, and a token for the audio stream that's good
    once, shortly, and for its own call only."""

    def __init__(self, pin: str, clock: Callable[[], float] = time.monotonic):
        self._pin, self._clock = pin, clock
        self._wrong, self._locked_until = 0, 0.0
        self._tokens: dict[str, tuple[str, float]] = {}

    @property
    def locked(self) -> bool:
        return self._clock() < self._locked_until

    def token(self, pin: str, call: str) -> str | None:
        """A token for `call`'s audio, for the right PIN; None for a wrong one, or while locked."""
        if self.locked:
            return None
        if not hmac.compare_digest(pin.encode(), self._pin.encode()):
            self._wrong += 1
            if self._wrong >= PIN_TRIES:
                self._wrong, self._locked_until = 0, self._clock() + LOCKED_S
            return None
        self._wrong = 0
        token = secrets.token_urlsafe(24)
        self._tokens[token] = (call, self._clock() + TOKEN_S)
        return token

    def redeem(self, token: str, call: str) -> bool:
        whose, until = self._tokens.pop(token, ("", 0.0))
        return bool(whose) and whose == call and self._clock() < until


def wrong(why: str) -> SystemExit:
    """A mistake in the settings: said, and the exit code that keeps systemd from trying again (EX_CONFIG)."""
    print(f"(phone: {why})", flush=True)
    return SystemExit(78)


@dataclass
class Settings:
    auth_token: str
    url: str
    allowed: frozenset[str]
    pin: str
    person: str | None = None

    @classmethod
    def read(cls, env: dict) -> Settings:
        def need(name: str) -> str:
            if not (value := (env.get(name) or "").strip()):
                raise wrong(f"{name} is missing from .env")
            return value

        url = need("TARS_PHONE_URL").rstrip("/")
        pin = need("TARS_PHONE_PIN")
        if urlsplit(url).scheme != "https":
            raise wrong("TARS_PHONE_URL must be https://")
        if not (pin.isascii() and pin.isdigit() and len(pin) >= 6):
            raise wrong("TARS_PHONE_PIN must be 6 digits or more")
        allowed = frozenset(n.strip() for n in need("TARS_PHONE_ALLOWED").split(",") if n.strip())
        person = (env.get("TARS_PHONE_PERSON") or "").strip() or None
        return cls(need("TWILIO_AUTH_TOKEN"), url, allowed, pin, person)


# One call


class Call:
    """The call's audio between Twilio and TARS. `to_twilio(message)`, and `to_brain(message)` once there's a link
    (joined). TARS puts marks in what it says: what's before one has played once Twilio gives it back. `stop` clears
    what Twilio still holds, and Twilio then gives back the marks it dropped.

    It starts with the right PIN, before Twilio's audio is here: what TARS says by then waits for the stream
    (`attach`), and what the caller says before TARS is there waits for it, so neither's first words are lost."""

    def __init__(self, stream: str = "", to_twilio: Callable[[dict], None] | None = None):
        self.stream, self._to_twilio = stream, to_twilio
        self._unsent: list[dict] = []  # before there's a stream
        self._unheard: deque[bytes] = deque(maxlen=UNHEARD_BLOCKS)  # before TARS is there
        self._there = False  # TARS said welcome: audio goes to it
        self.hung_up = threading.Event()
        self._to_brain: Callable[[dict | bytes], None] | None = None
        self._lock = threading.RLock()
        self._up = Resampler(PHONE_RATE, 16000)
        self._pending = np.zeros(0, np.int16)
        self._rate = 24000
        self._down: Resampler | None = None
        self._odd = self._frame = b""
        self._waiting: set[int] = set()  # marks Twilio hasn't given back
        self._cut: set[int] = set()

    def _send(self, event: str, **fields) -> None:
        with self._lock:
            message = {"event": event, "streamSid": self.stream, **fields}
            if self._to_twilio is None:
                self._unsent.append(message)
            else:
                self._to_twilio(message)

    def attach(self, stream: str, to_twilio: Callable[[dict], None]) -> None:
        """Twilio's stream is here."""
        with self._lock:
            self.stream, self._to_twilio = stream, to_twilio
            for message in self._unsent:
                to_twilio({**message, "streamSid": stream})
            self._unsent.clear()

    # From Twilio

    def media(self, payload: str) -> None:
        pcm = self._up(mulaw_decode(base64.b64decode(payload, validate=True)))
        self._pending = np.concatenate([self._pending, pcm])
        while len(self._pending) >= BLOCK_SAMPLES:
            block, self._pending = self._pending[:BLOCK_SAMPLES], self._pending[BLOCK_SAMPLES:]
            with self._lock:
                if self._there:
                    self._to_brain(block.tobytes())
                else:
                    self._unheard.append(block.tobytes())

    def mark(self, name: str) -> None:
        if not name.isdigit():
            return
        play_id = int(name)
        with self._lock:
            cut = play_id in self._cut
            self._cut.discard(play_id)
            self._waiting.discard(play_id)
        if self._to_brain:
            self._to_brain({"type": "played", "id": play_id, "cut": cut})

    def key(self, digit: str) -> None:
        """Star cuts TARS short."""
        if digit == "*":
            self.stop()
            if self._to_brain:
                self._to_brain({"type": "button", "name": "stop"})

    # From TARS

    def joined(self, to_brain: Callable[[dict | bytes], None]) -> None:
        self._to_brain = to_brain

    def lost(self) -> None:
        with self._lock:
            self._to_brain, self._there = None, False
        self.stop()

    def brain(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            self.audio(message)
            return
        said = json.loads(message)
        kind = said.get("type")
        if kind == "welcome":
            self._rate = int(said["play_rate"])
            if self._to_brain:  # the PIN is the caller asking to talk; from here the call's audio goes to TARS
                with self._lock:
                    self._to_brain({"type": "wake", "how": "button", "score": 0, "preroll_ms": 0})
                    for block in self._unheard:
                        self._to_brain(block)
                    self._unheard.clear()
                    self._there = True
        elif kind == "play":
            self.start()
        elif kind == "mark":
            self.put_mark(int(said["id"]))
        elif kind == "stop":
            self.stop()
        elif kind == "hangup":
            self.hung_up.set()

    def start(self) -> None:
        with self._lock:
            self._down = Resampler(self._rate, PHONE_RATE)
            self._odd = self._frame = b""

    def audio(self, data: bytes) -> None:
        with self._lock:
            if self._down is None:  # nothing's playing, or it was stopped
                return
            data = self._odd + data
            whole = len(data) // 2 * 2
            self._odd = data[whole:]
            self._frame += mulaw_encode(self._down(np.frombuffer(data[:whole], np.int16)))
            self._flush(FRAME_BYTES)

    def _flush(self, at_least: int) -> None:
        while self._frame and len(self._frame) >= at_least:
            frame, self._frame = self._frame[:FRAME_BYTES], self._frame[FRAME_BYTES:]
            self._send("media", media={"payload": base64.b64encode(frame).decode()})

    def put_mark(self, mark: int) -> None:
        with self._lock:
            if self._down is None:  # stopped meanwhile
                return
            self._flush(1)
            self._waiting.add(mark)
            self._send("mark", mark={"name": str(mark)})

    def stop(self) -> None:
        with self._lock:
            self._cut |= self._waiting
            self._down, self._frame = None, b""
            self._send("clear")


def link_to_brain(call: Call, url: str, ticket: Callable[[], str], over: threading.Event) -> None:
    """The call's link to TARS, made again if it drops, until the call is over."""
    from websockets.sync.client import connect

    failed = 0
    while not over.is_set() and failed < 3:
        try:
            with connect(url, max_size=None, compression=None, open_timeout=5) as ws:
                json.loads(ws.recv(timeout=5))  # the challenge, which a ticket doesn't answer
                hello = {"type": "hello", "protocol": PROTOCOL, "ticket": ticket(), "kind": PHONE, "echo_cancel": True}
                ws.send(json.dumps(hello))
                outbox: queue.Queue[dict | bytes | None] = queue.Queue()

                def sending(ws=ws, outbox=outbox) -> None:  # its own thread: Twilio's audio never waits on TARS
                    with contextlib.suppress(Exception):  # gone: the reader below finds out
                        while (message := outbox.get()) is not None:
                            ws.send(message if isinstance(message, bytes) else json.dumps(message))

                threading.Thread(target=sending, daemon=True, name="call-up").start()
                call.joined(outbox.put)
                try:
                    while not over.is_set() and not call.hung_up.is_set():
                        try:
                            call.brain(ws.recv(timeout=0.5))
                            failed = 0
                        except TimeoutError:
                            continue
                finally:
                    outbox.put(None)
        except Exception as e:  # noqa: BLE001 - whatever it was, the link is made again
            print(f"(phone: the link to TARS ended: {type(e).__name__})", flush=True)
        call.lost()
        if call.hung_up.is_set():
            break
        failed += 1
        over.wait(1.0)
    over.set()


# The server


def make_app(settings: Settings, brain_url: str, ticket: Callable[[], str], gate: Gate | None = None):
    from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    gate = gate or Gate(settings.pin)
    busy = asyncio.Lock()
    starting: dict[str, tuple[Call, threading.Event]] = {}  # by call: let in, its audio not here yet

    def xml(inside: str) -> Response:
        return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{inside}</Response>', media_type="text/xml")

    async def form(request: Request, path: str) -> dict[str, str] | None:
        """What Twilio posted, from an allowed caller; None for anyone else."""
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY:
                return None
        try:
            params = parse_qsl(body.decode(), keep_blank_values=True)
        except UnicodeDecodeError:
            return None
        given = request.headers.get("x-twilio-signature", "")
        right = signature(settings.auth_token, settings.url + path, params)
        if not hmac.compare_digest(right.encode(), given.encode()):
            return None
        said = dict(params)
        return said if said.get("From") in settings.allowed else None

    @app.post("/voice")
    async def voice(request: Request):
        if await form(request, "/voice") is None or gate.locked:
            return xml('<Reject reason="rejected"/>')
        action = quoteattr(settings.url + "/pin")
        return xml(f'<Gather input="dtmf" action={action} method="POST" finishOnKey="#" timeout="10"><Say>Pin?</Say>'
                   "</Gather><Hangup/>")  # fmt: skip

    @app.post("/pin")
    async def pin(request: Request):
        said = await form(request, "/pin")
        token = gate.token(said.get("Digits", ""), said.get("CallSid", "")) if said else None
        if token is None or busy.locked():
            return xml("<Hangup/>")
        start(said["CallSid"])
        audio = quoteattr("wss" + settings.url.removeprefix("https") + "/audio")
        parameter = f'<Parameter name="token" value={quoteattr(token)}/>'
        return xml(f"<Connect><Stream url={audio}>{parameter}</Stream></Connect>")

    def start(sid: str) -> None:
        """TARS joins the call now, not once its audio is here."""
        call, over = Call(), threading.Event()
        starting[sid] = (call, over)

        def unused() -> None:
            if starting.get(sid, (None,))[0] is call:
                del starting[sid]
                over.set()

        asyncio.get_running_loop().call_later(TOKEN_S, unused)
        threading.Thread(target=link_to_brain, args=(call, brain_url, ticket, over), daemon=True, name="call").start()

    @app.websocket("/audio")
    async def audio(ws: WebSocket):
        await ws.accept()
        try:
            async with asyncio.timeout(START_S):
                while (first := json.loads(await ws.receive_text())).get("event") != "start":
                    pass
            begun = first["start"]
            sid, stream = str(begun["callSid"]), str(begun["streamSid"])
            good = gate.redeem(str(begun.get("customParameters", {}).get("token", "")), sid)
            good = good and sid in starting
        except TimeoutError, ValueError, KeyError, TypeError, AttributeError, WebSocketDisconnect:
            good = False
        if not good or busy.locked():
            with contextlib.suppress(RuntimeError):
                await ws.close()
            return
        async with busy:
            await carry(ws, stream, *starting.pop(sid))

    async def carry(ws: WebSocket, stream: str, call: Call, over: threading.Event) -> None:
        loop, out = asyncio.get_running_loop(), asyncio.Queue()

        def to_twilio(message: dict) -> None:
            with contextlib.suppress(RuntimeError):  # the server is stopping
                loop.call_soon_threadsafe(out.put_nowait, message)

        call.attach(stream, to_twilio)

        async def sending() -> None:
            while True:
                await ws.send_text(json.dumps(await out.get()))

        async def receiving() -> None:
            while True:
                said = json.loads(await ws.receive_text())
                event = said.get("event")
                if event == "media":
                    call.media(said["media"]["payload"])
                elif event == "mark":
                    call.mark(str(said["mark"]["name"]))
                elif event == "dtmf":
                    call.key(str(said["dtmf"]["digit"]))
                elif event == "stop":
                    return

        async def ended() -> None:
            while not over.is_set():
                await asyncio.sleep(0.5)

        tasks = [asyncio.create_task(t()) for t in (sending, receiving, ended)]
        try:
            await asyncio.wait(tasks, timeout=MAX_CALL_S, return_when=asyncio.FIRST_COMPLETED)
        finally:
            over.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(RuntimeError):
                await ws.close()

    return app


def main() -> None:
    import argparse

    import scipy.signal  # noqa: F401 - over two seconds on a Pi, the first time: not in someone's first call
    import uvicorn
    import websockets.sync.client  # noqa: F401 - the same
    from dotenv import dotenv_values

    from .bodykeys import Tickets
    from .config import load_config
    from .wiring import make_event_log

    parser = argparse.ArgumentParser(prog="python -m voice_assistant.phone")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    root = Path(__file__).parents[2]
    settings = Settings.read(dotenv_values(root / ".env"))
    cfg = load_config(root / "config.toml")
    if not cfg.bodies.enabled:
        raise wrong("a phone call is a body: it needs [bodies] enabled")
    tickets = Tickets(make_event_log(cfg, root).store)
    app = make_app(settings, f"ws://127.0.0.1:{cfg.bodies.port}", lambda: tickets.issue(settings.person))
    uvicorn.run(app, host="127.0.0.1", port=args.port, ws_max_size=64 * 1024, access_log=False)


if __name__ == "__main__":
    main()
