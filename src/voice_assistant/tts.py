"""Text to speech: OpenAI's voices over HTTP, or Deepgram's over websockets kept warm between sentences."""

import json
import queue
import threading
import time
from collections.abc import Iterator
from typing import Protocol
from urllib.parse import urlencode

from openai import OpenAI, Timeout

from .config import TTSConfig
from .stt import deepgram_connect

# A reply's audio normally starts within a second. After this long with nothing, TARS gives up and says so in a
# pre-made clip instead of going quiet for the client's full timeout.
STALL_S = 4.0
CONNECT_S = 3.0
SPEAK_URL = "wss://api.deepgram.com/{version}/speak"


class Voice(Protocol):
    sample_rate: int

    def stream(self, text: str) -> Iterator[bytes]:
        """Yields 16-bit mono PCM chunks as they're synthesized."""

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


class DeepgramSpeech:
    """Deepgram's streaming voices, Flux TTS (flux-*) and Aura-2 (aura-2-*), over a websocket.

    Opening a connection takes about 0.7 s and a warm one starts speaking in about 0.27 s (measured), so connections
    are opened ahead in warm() and reused: each sentence takes one and hands it back when done. Idle ones stay usable
    for minutes; past MAX_IDLE_S one is replaced. Connections are closed off the caller's thread, because closing one
    that has gone quiet waits for a handshake that never comes.
    """

    sample_rate = 24_000
    READY = 2  # connections open before the reply: its first sentence, and the one after
    KEEP = 3  # the most kept idle (a long reply synthesizes several sentences at once)
    MAX_IDLE_S = 100.0

    def __init__(self, api_key: str, cfg: TTSConfig):
        version = "v2" if cfg.model.startswith("flux-") else "v1"
        params = {"model": cfg.model, "encoding": "linear16", "sample_rate": self.sample_rate}
        self._url = f"{SPEAK_URL.format(version=version)}?{urlencode(params)}"
        # Aura-2 sends a sentence's audio, then Flushed; Flux sends Flushed, the audio, then SpeechMetadata.
        self._last = "SpeechMetadata" if version == "v2" else "Flushed"
        self._key = api_key
        self._idle: queue.LifoQueue = queue.LifoQueue()

    def warm(self) -> None:
        threading.Thread(target=self._top_up, daemon=True, name="deepgram-voice").start()

    def stream(self, text: str) -> Iterator[bytes]:
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

    def _audio(self, ws) -> Iterator[bytes]:
        while True:
            message = ws.recv(timeout=STALL_S)
            if isinstance(message, bytes):
                yield message
                continue
            event = json.loads(message)
            if event.get("type") == "Error":
                raise RuntimeError(f"Deepgram voice: {event.get('description') or event}")
            if event.get("type") == self._last:
                return

    @staticmethod
    def _send(ws, text: str) -> None:
        ws.send(json.dumps({"type": "Speak", "text": text}))
        ws.send(json.dumps({"type": "Flush"}))

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

    def _connect(self):
        return deepgram_connect(self._url, self._key, open_timeout=CONNECT_S, close_timeout=1)

    @staticmethod
    def _discard(ws) -> None:
        threading.Thread(target=ws.close, daemon=True, name="deepgram-voice-close").start()
