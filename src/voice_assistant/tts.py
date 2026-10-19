"""Text to speech: OpenAI's voices over HTTP, or Deepgram's and Cartesia's over websockets kept warm."""

import base64
import json
import queue
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Protocol
from urllib.parse import urlencode

from openai import OpenAI, Timeout

from .config import TTSConfig
from .errors import ServiceError
from .stt import deepgram_connect

# A reply's audio normally starts within a second. After this long with nothing, TARS gives up and says so in a
# pre-made clip instead of going quiet for the client's full timeout.
STALL_S = 4.0
CONNECT_S = 3.0
SPEAK_URL = "wss://{host}/{version}/speak"
CARTESIA_API = "https://api.cartesia.ai"
CARTESIA_URL = "wss://api.cartesia.ai/tts/websocket"
CARTESIA_VERSION = "2026-08-14"


class Voice(Protocol):
    sample_rate: int

    def stream(self, text: str) -> Iterator[bytes]:
        """Yields 16-bit mono PCM chunks as they're synthesized. A service that fails raises ServiceError (OpenAI's,
        OpenAIError)."""

    def warm(self) -> None:
        """Called when TARS wakes, so the reply doesn't wait to connect. May do nothing."""


class OpenAISpeech:
    # OpenAI's raw "pcm" format is 24 kHz, 16-bit, mono.
    sample_rate = 24_000

    def __init__(self, client: OpenAI, cfg: TTSConfig):
        self._client = client.with_options(timeout=Timeout(STALL_S, connect=CONNECT_S), max_retries=0)
        self._cfg = cfg
        # Only gpt-4o-mini-tts and newer accept delivery instructions: leave them empty for older models.
        self._extra = {"instructions": cfg.instructions} if cfg.instructions else {}

    def warm(self) -> None:
        pass  # shares the brain's OpenAI connection, which the brain warms

    def stream(self, text: str) -> Iterator[bytes]:
        with self._client.audio.speech.with_streaming_response.create(
            model=self._cfg.model,
            voice=self._cfg.voice,
            input=text,
            response_format="pcm",
            **self._extra,
        ) as response:
            yield from response.iter_bytes(4096)


class WarmSockets:
    """A streaming voice over websockets, kept warm between sentences.

    Opening a connection takes about 0.7 s and a warm one starts speaking in about 0.27 s (measured on Deepgram), so
    connections are opened ahead in warm() and reused: each sentence takes one and hands it back when done. Idle ones
    stay usable for minutes; past MAX_IDLE_S one is replaced. Connections are closed off the caller's thread, because
    closing one that has gone quiet waits for a handshake that never comes. At most AT_ONCE sentences synthesize at
    once, so a long reply never needs more connections than the service allows (Flux TTS in the EU: 5); the rest wait
    their turn, which costs nothing, since playing a sentence takes longer than making it. A subclass says how to
    connect, send a sentence and read its audio.
    """

    sample_rate = 24_000
    READY = 2  # connections open before the reply: its first sentence, and the one after
    KEEP = 3  # the most kept idle (a long reply synthesizes several sentences at once)
    AT_ONCE = 3
    MAX_IDLE_S = 100.0
    name = "voice"  # for its threads
    title = "Voice"  # for its errors

    def __init__(self):
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._turns = threading.BoundedSemaphore(self.AT_ONCE)

    def warm(self) -> None:
        threading.Thread(target=self._top_up, daemon=True, name=f"{self.name}-voice").start()

    def stream(self, text: str) -> Iterator[bytes]:
        from websockets.exceptions import WebSocketException

        with self._turns:
            try:
                yield from self._stream(text)
            except (WebSocketException, OSError, ValueError, KeyError) as e:  # ValueError, KeyError: a garbled message
                raise ServiceError(f"{self.title}: {e!r}") from e

    def _stream(self, text: str) -> Iterator[bytes]:
        from websockets.exceptions import ConnectionClosed

        for attempt in (1, 2):
            ws = self._take() if attempt == 1 else self._connect()
            started = done = False
            try:
                self._send(ws, text)
                for chunk in self._audio(ws):
                    started = True
                    yield chunk
                done = True
                return
            except (ConnectionClosed, OSError) as e:
                # A warm connection that died while idle fails before any audio, so it gets one fresh try. Never after
                # a stall (TimeoutError), which already cost STALL_S.
                if started or attempt == 2 or isinstance(e, TimeoutError):
                    raise
            finally:
                # A sentence cut short leaves its audio in flight on the connection; only a finished one is reused.
                self._keep(ws) if done else self._discard(ws)

    def _send(self, ws, text: str) -> None:
        raise NotImplementedError

    def _audio(self, ws) -> Iterator[bytes]:
        raise NotImplementedError

    def _connect(self):
        raise NotImplementedError

    def _take(self):
        fresh = self._fresh()
        for entry in reversed(fresh[1:]):  # the rest go back, newest on top
            self._idle.put(entry)
        return fresh[0][1] if fresh else self._connect()

    def _keep(self, ws) -> None:
        if self._idle.qsize() >= self.KEEP:
            self._discard(ws)
        else:
            self._idle.put((time.monotonic(), ws))

    def _top_up(self) -> None:
        fresh = self._fresh()
        for entry in reversed(fresh):
            self._idle.put(entry)
        try:
            for _ in range(self.READY - len(fresh)):
                self._idle.put((time.monotonic(), self._connect()))
        except Exception as e:  # noqa: BLE001 - only a head start; stream() connects on its own
            print(f"(couldn't open a connection to the voice ahead of time: {e!r})")

    def _fresh(self) -> list:
        """Takes out every idle connection young enough to use, newest first, and closes the older ones."""
        fresh = []
        while True:
            try:
                opened, ws = self._idle.get_nowait()
            except queue.Empty:
                return fresh
            if time.monotonic() - opened < self.MAX_IDLE_S:
                fresh.append((opened, ws))
            else:
                self._discard(ws)

    def _discard(self, ws) -> None:
        threading.Thread(target=ws.close, daemon=True, name=f"{self.name}-voice-close").start()


class DeepgramSpeech(WarmSockets):
    """Deepgram's streaming voices, Flux TTS (flux-*) and Aura-2 (aura-2-*)."""

    name, title = "deepgram", "Deepgram voice"

    def __init__(self, api_key: str, cfg: TTSConfig):
        super().__init__()
        version = "v2" if cfg.model.startswith("flux-") else "v1"
        params = {"model": cfg.model, "encoding": "linear16", "sample_rate": self.sample_rate}
        self._url = f"{SPEAK_URL.format(host=cfg.deepgram_host, version=version)}?{urlencode(params)}"
        # Aura-2 sends a sentence's audio, then Flushed; Flux sends Flushed, the audio, then SpeechMetadata.
        self._last = "SpeechMetadata" if version == "v2" else "Flushed"
        self._key = api_key

    def _send(self, ws, text: str) -> None:
        ws.send(json.dumps({"type": "Speak", "text": text}))
        ws.send(json.dumps({"type": "Flush"}))

    def _audio(self, ws) -> Iterator[bytes]:
        while True:
            message = ws.recv(timeout=STALL_S)
            if isinstance(message, bytes):
                yield message
                continue
            event = json.loads(message)
            if event.get("type") == "Error":
                raise ServiceError(f"{self.title}: {event.get('description') or event}")
            if event.get("type") == self._last:
                return

    def _connect(self):
        return deepgram_connect(self._url, self._key, open_timeout=CONNECT_S, close_timeout=1)


class CartesiaSpeech(WarmSockets):
    """Cartesia's Sonic voices: each sentence is its own context, its audio base64 chunks until "done"."""

    name, title = "cartesia", "Cartesia voice"
    AT_ONCE = 2  # the free plan's limit on sentences being made at once

    def __init__(self, api_key: str, cfg: TTSConfig):
        super().__init__()
        self._url = f"{CARTESIA_URL}?{urlencode({'cartesia_version': CARTESIA_VERSION})}"
        self._key = api_key
        self._request = {
            "model_id": cfg.model,
            "voice": cfg.cartesia_voice,
            "language": "en",
            "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": self.sample_rate},
        }

    def _send(self, ws, text: str) -> None:
        ws.send(json.dumps({**self._request, "transcript": text, "context_id": uuid.uuid4().hex, "continue": False}))

    def _audio(self, ws) -> Iterator[bytes]:
        while True:
            event = json.loads(ws.recv(timeout=STALL_S))
            kind = event.get("type")
            if kind == "chunk":
                yield base64.b64decode(event["data"])
            elif kind == "error":
                raise ServiceError(f"{self.title}: {event.get('message') or event.get('title') or event}")
            elif kind == "done" or event.get("done"):
                return

    def _connect(self):
        from websockets.sync.client import connect

        return connect(self._url, additional_headers={"X-API-Key": self._key}, open_timeout=CONNECT_S, close_timeout=1)
