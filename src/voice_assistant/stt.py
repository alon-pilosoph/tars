"""Speech to text: streamed to Deepgram while you talk, with OpenAI transcribing the same recording if that fails.

A session starts when TARS starts listening, before anyone speaks, so a streaming service can connect ahead; it's
fed the audio as it's recorded, asked for the words so far (at a pause, and at the end), and closed.
"""

import io
import json
import queue
import threading
import wave
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlencode

from openai import OpenAI

from .audio import SAMPLE_RATE
from .config import STTConfig

DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"
# How long to wait for the last words once asked for them.
FINAL_TIMEOUT_S = 5.0
# Deepgram closes a stream that gets no audio for 10 s; while nobody speaks, this keeps it open.
KEEPALIVE_S = 4.0


class Session(Protocol):
    def feed(self, pcm: bytes) -> None:
        """16 kHz mono int16 audio, as it's recorded."""

    def transcript(self) -> str:
        """The words in everything fed so far. More audio may follow."""

    def close(self) -> None: ...


class Transcriber(Protocol):
    def session(self) -> Session: ...


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buf.getvalue()


class BufferedSession:
    """For services that take a finished recording: keep the audio, send it all when asked."""

    def __init__(self, transcribe: Callable[[bytes], str]):
        self._transcribe = transcribe
        self._audio = bytearray()

    def feed(self, pcm: bytes) -> None:
        self._audio += pcm

    def transcript(self) -> str:
        return self._transcribe(bytes(self._audio))

    def close(self) -> None:
        self._audio.clear()


class OpenAITranscriber:
    def __init__(self, client: OpenAI, cfg: STTConfig):
        self._client = client
        self._cfg = cfg

    def session(self) -> Session:
        return BufferedSession(self._transcribe)

    def _transcribe(self, pcm: bytes) -> str:
        result = self._client.audio.transcriptions.create(
            model=self._cfg.model,
            file=("speech.wav", pcm_to_wav(pcm), "audio/wav"),
            language=self._cfg.language or None,
        )
        return result.text.strip()


class DeepgramTranscriber:
    """Deepgram's streaming speech-to-text: the words are recognized while you talk, so the transcript is ready a
    moment after you stop instead of after an upload and a pass over the whole recording."""

    def __init__(self, api_key: str, cfg: STTConfig):
        self._key = api_key
        params = {
            "model": cfg.model,
            "encoding": "linear16",
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "smart_format": "true",
            "punctuate": "true",
        }
        if cfg.language:
            params["language"] = cfg.language
        self._url = f"{DEEPGRAM_URL}?{urlencode(params)}"

    def session(self) -> Session:
        return DeepgramSession(self._url, self._key)


class DeepgramSession:
    """Connects in the background as soon as it's made; audio fed before the connection is up waits in a queue.

    Asking for the transcript sends Finalize, which flushes what Deepgram has heard; each Finalize is answered by a
    result marked from_finalize, so transcript() waits for the answer to its own. Anything that goes wrong (the
    connection, a timeout) raises there, so the caller can fall back.
    """

    def __init__(self, url: str, key: str):
        self._outbox: queue.Queue[bytes | str | None] = queue.Queue()
        self._final: list[str] = []
        self._answered = threading.Condition()
        self._asked = self._got = 0
        self._closed = False
        self._error: Exception | None = None
        self._fed = False
        threading.Thread(target=self._run, args=(url, key), daemon=True, name="deepgram").start()

    def feed(self, pcm: bytes) -> None:
        self._fed = True
        self._outbox.put(pcm)

    def transcript(self) -> str:
        if not self._fed:
            return ""
        with self._answered:
            self._asked += 1
            wanted = self._asked
        self._outbox.put(json.dumps({"type": "Finalize"}))
        with self._answered:
            self._answered.wait_for(lambda: self._got >= wanted or self._closed, FINAL_TIMEOUT_S)
            if self._got < wanted:
                raise self._error or TimeoutError("Deepgram didn't finish the transcript in time")
            return " ".join(self._final).strip()

    def close(self) -> None:
        self._outbox.put(None)

    def _run(self, url: str, key: str) -> None:
        try:
            from websockets.sync.client import connect

            with connect(url, additional_headers={"Authorization": f"Token {key}"}, open_timeout=5) as ws:
                threading.Thread(target=self._receive, args=(ws,), daemon=True, name="deepgram-receive").start()
                while True:
                    try:
                        item = self._outbox.get(timeout=KEEPALIVE_S)
                    except queue.Empty:
                        item = json.dumps({"type": "KeepAlive"})
                    if item is None:
                        break
                    ws.send(item)
                ws.send(json.dumps({"type": "CloseStream"}))
        except Exception as e:  # noqa: BLE001 - reported by transcript(), which the fallback catches
            self._close(e)

    def _receive(self, ws) -> None:
        try:
            for message in ws:
                data = json.loads(message)
                if data.get("type") != "Results":
                    continue
                text = data["channel"]["alternatives"][0]["transcript"] if data.get("is_final") else ""
                with self._answered:
                    if text:
                        self._final.append(text)
                    if data.get("from_finalize"):
                        self._got += 1
                        self._answered.notify_all()
            self._close(None)
        except Exception as e:  # noqa: BLE001 - reported by transcript(), which the fallback catches
            self._close(e)

    def _close(self, error: Exception | None) -> None:
        with self._answered:
            self._error = self._error or error or ConnectionError("Deepgram closed the stream")
            self._closed = True
            self._answered.notify_all()


class FallbackTranscriber:
    """A streaming transcriber with a backup: the audio is also kept, and if the stream fails, the backup
    transcribes the recording instead."""

    def __init__(self, transcriber: Transcriber, backup: Transcriber):
        self._transcriber, self._backup = transcriber, backup

    def session(self) -> Session:
        return _FallbackSession(self._transcriber.session(), self._backup.session())


class _FallbackSession:
    def __init__(self, main: Session, backup: Session):
        self._main, self._backup = main, backup

    def feed(self, pcm: bytes) -> None:
        self._main.feed(pcm)
        self._backup.feed(pcm)

    def transcript(self) -> str:
        try:
            return self._main.transcript()
        except Exception as e:  # noqa: BLE001 - whatever broke the stream, the recording is still here
            print(f"(speech to text failed: {e!r}; using the backup)")
            return self._backup.transcript()

    def close(self) -> None:
        self._main.close()
        self._backup.close()
