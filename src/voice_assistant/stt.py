import io
import json
import queue
import threading
import wave
from typing import Protocol
from urllib.parse import urlencode

from openai import OpenAI

from .audio import SAMPLE_RATE
from .config import STTConfig

# How long to wait for the last words once the audio has ended.
FINAL_TIMEOUT_S = 5.0


class Session(Protocol):
    """One utterance's transcription: fed 16 kHz mono int16 audio while the person talks."""

    def feed(self, pcm: bytes) -> None: ...

    def peek(self) -> str:
        """The transcript of everything fed so far; more audio may follow."""

    def finish(self) -> str:
        """The whole transcript, once the audio has ended."""

    def cancel(self) -> None:
        """Nobody spoke after all: drop it."""


class Transcriber(Protocol):
    def session(self) -> Session:
        """Called when listening starts, before anyone speaks, so a streaming service can connect ahead."""

    def transcribe(self, pcm: bytes) -> str: ...


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buf.getvalue()


class BufferedSession:
    """For services that take a finished recording: keep the audio, send it all at the end."""

    def __init__(self, transcriber: Transcriber):
        self._transcriber = transcriber
        self._audio = bytearray()

    def feed(self, pcm: bytes) -> None:
        self._audio += pcm

    def peek(self) -> str:
        return self._transcriber.transcribe(bytes(self._audio))

    def finish(self) -> str:
        return self.peek()

    def cancel(self) -> None:
        self._audio.clear()


class OpenAITranscriber:
    def __init__(self, client: OpenAI, cfg: STTConfig):
        self._client = client
        self._cfg = cfg

    def session(self) -> Session:
        return BufferedSession(self)

    def transcribe(self, pcm: bytes) -> str:
        result = self._client.audio.transcriptions.create(
            model=self._cfg.model,
            file=("speech.wav", pcm_to_wav(pcm), "audio/wav"),
            language=self._cfg.language or None,
        )
        return result.text.strip()


class DeepgramTranscriber:
    """Deepgram's streaming speech-to-text: the words are recognized while you talk, so the transcript is ready a
    moment after you stop instead of after an upload and a pass over the whole recording."""

    URL = "wss://api.deepgram.com/v1/listen"

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
        self._url = f"{self.URL}?{urlencode(params)}"

    def session(self) -> Session:
        return DeepgramSession(self._url, self._key)

    def transcribe(self, pcm: bytes) -> str:
        s = self.session()
        s.feed(pcm)
        return s.finish()


class DeepgramSession:
    """Connects in the background as soon as it's made; audio fed before the connection is up waits in a queue."""

    def __init__(self, url: str, key: str):
        self._outbox: queue.Queue[bytes | str | None] = queue.Queue()
        self._final: list[str] = []
        # Each Finalize is answered by a result marked from_finalize; peek() waits for the answer to its own.
        self._answered = threading.Condition()
        self._asked = self._got = 0
        self._closed = False
        self._error: Exception | None = None
        self._fed = False
        threading.Thread(target=self._run, args=(url, key), daemon=True).start()

    def feed(self, pcm: bytes) -> None:
        self._fed = True
        self._outbox.put(pcm)

    def peek(self) -> str:
        if not self._fed:
            return ""
        with self._answered:
            self._asked += 1
            wanted = self._asked
        self._outbox.put(json.dumps({"type": "Finalize"}))
        with self._answered:
            if not self._answered.wait_for(lambda: self._got >= wanted or self._closed, FINAL_TIMEOUT_S):
                self._error = self._error or TimeoutError("Deepgram didn't finish the transcript in time")
            if self._error:
                raise self._error
            return " ".join(self._final).strip()

    def finish(self) -> str:
        try:
            return self.peek()
        finally:
            self.cancel()

    def cancel(self) -> None:
        self._outbox.put(None)

    def _run(self, url: str, key: str) -> None:
        from websockets.sync.client import connect

        try:
            with connect(url, additional_headers={"Authorization": f"Token {key}"}, open_timeout=5) as ws:
                threading.Thread(target=self._receive, args=(ws,), daemon=True).start()
                while (item := self._outbox.get()) is not None:
                    ws.send(item)
                ws.send(json.dumps({"type": "CloseStream"}))
        except Exception as e:  # noqa: BLE001 - whatever broke the connection is reported by peek()
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
        except Exception as e:  # noqa: BLE001 - a dropped connection; peek() reports it if an answer was pending
            self._close(e)
        finally:
            self._close(None)

    def _close(self, error: Exception | None) -> None:
        with self._answered:
            if self._got < self._asked:
                self._error = self._error or error or ConnectionError("Deepgram closed before finishing")
            self._closed = True
            self._answered.notify_all()


class FallbackTranscriber:
    """A streaming transcriber with a backup: the audio is also kept, and if the stream fails, the backup
    transcribes the recording instead."""

    def __init__(self, transcriber: Transcriber, backup: Transcriber):
        self._transcriber, self._backup = transcriber, backup

    def session(self) -> Session:
        return _FallbackSession(self._transcriber.session(), BufferedSession(self._backup))

    def transcribe(self, pcm: bytes) -> str:
        return self._backup.transcribe(pcm)


class _FallbackSession:
    def __init__(self, main: Session, backup: BufferedSession):
        self._main, self._backup = main, backup

    def feed(self, pcm: bytes) -> None:
        self._main.feed(pcm)
        self._backup.feed(pcm)

    def peek(self) -> str:
        try:
            return self._main.peek()
        except Exception as e:  # noqa: BLE001 - whatever broke the stream, the recording is still here
            print(f"(speech to text failed: {e}; using the backup)")
            return self._backup.peek()

    def finish(self) -> str:
        try:
            return self._main.finish()
        except Exception as e:  # noqa: BLE001 - whatever broke the stream, the recording is still here
            print(f"(speech to text failed: {e}; using the backup)")
            return self._backup.finish()

    def cancel(self) -> None:
        self._main.cancel()
        self._backup.cancel()
