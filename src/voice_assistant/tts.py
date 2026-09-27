import json
import queue
import threading
import time
from collections.abc import Iterator
from typing import Protocol

from openai import OpenAI, Timeout

from .config import TTSConfig

# A reply's audio normally starts within a second. If nothing arrives for this long, give up: TARS says so in its
# own (pre-made) voice instead of going quiet for the client's full timeout.
STALL_S = 4.0
CONNECT_S = 3.0


class Voice(Protocol):
    sample_rate: int

    def stream(self, text: str) -> Iterator[bytes]:
        """Yield 16-bit mono PCM chunks as they're synthesized."""

    def warm(self) -> None:
        """Get ready to speak, when TARS wakes, so the reply doesn't wait to connect. May do nothing."""


class OpenAISpeech:
    # OpenAI's raw "pcm" format is 24 kHz, 16-bit, mono.
    sample_rate = 24_000

    def __init__(self, client: OpenAI, cfg: TTSConfig):
        self._client = client.with_options(timeout=Timeout(STALL_S, connect=CONNECT_S), max_retries=0)
        self._cfg = cfg
        # Only gpt-4o-mini-tts and newer accept delivery instructions; don't send them to older models.
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

    Opening a connection takes about 0.7 s from here and a warm one starts speaking in about 0.27 s, so a few are
    kept open: each sentence takes one and hands it back for the next. They stay usable idle for minutes; one older
    than MAX_IDLE_S, or one that turns out to be closed, is replaced.
    """

    sample_rate = 24_000
    READY = 2  # sentences are synthesized two at a time: speech.StreamedReply works on the next while one plays
    MAX_IDLE_S = 100.0

    def __init__(self, api_key: str, cfg: TTSConfig):
        version = "v2" if cfg.model.startswith("flux-") else "v1"
        self._url = f"wss://api.deepgram.com/{version}/speak?model={cfg.model}&encoding=linear16&sample_rate=24000"
        # Aura-2 sends a sentence's audio, then Flushed; Flux sends Flushed, the audio, then SpeechMetadata.
        self._last = "SpeechMetadata" if version == "v2" else "Flushed"
        self._headers = {"Authorization": f"Token {api_key}"}
        self._idle: queue.LifoQueue = queue.LifoQueue()

    def warm(self) -> None:
        for _ in range(max(0, self.READY - self._idle.qsize())):
            threading.Thread(target=self._put_new, daemon=True, name="deepgram-voice").start()

    def stream(self, text: str) -> Iterator[bytes]:
        ws = self._take()
        done = False
        try:
            try:
                self._send(ws, text)
            except Exception:  # noqa: BLE001 - a warm connection that died meanwhile: one fresh try
                ws.close()
                ws = self._connect()
                self._send(ws, text)
            yield from self._audio(ws)
            done = True
        finally:
            # A sentence cut short leaves its audio in flight on the connection; only a finished one is reused.
            if done:
                self._idle.put((time.monotonic(), ws))
            else:
                ws.close()

    def _audio(self, ws) -> Iterator[bytes]:
        while True:
            message = ws.recv(timeout=STALL_S)  # TimeoutError: a stalled voice, reported as a failed reply
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
        while True:
            try:
                opened, ws = self._idle.get_nowait()
            except queue.Empty:
                return self._connect()
            if time.monotonic() - opened < self.MAX_IDLE_S:
                return ws
            ws.close()

    def _connect(self):
        from websockets.sync.client import connect

        return connect(self._url, additional_headers=self._headers, open_timeout=CONNECT_S)

    def _put_new(self) -> None:
        try:
            self._idle.put((time.monotonic(), self._connect()))
        except Exception as e:  # noqa: BLE001 - only a head start: the sentence connects itself if this failed
            print(f"(couldn't open a connection to the voice ahead of time: {e!r})")
