"""Speech to text: streamed to Deepgram while you talk, with OpenAI transcribing the same recording if that fails.
With Flux, Deepgram also decides when you've finished talking.

A session starts when TARS starts listening, before anyone speaks, so a streaming service can connect ahead. It's fed
the audio as it's recorded, asked for the words so far (at a pause, and at the end), and closed.
"""

import json
import queue
import threading
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlencode

from .audio import SAMPLE_RATE, wav_bytes
from .config import STTConfig

if TYPE_CHECKING:
    from openai import OpenAI

DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"
FLUX_URL = "wss://api.deepgram.com/v2/listen"
CONNECT_S = 5.0
# How sure Flux must be that the turn is over, and how sure for an early "maybe" (where a draft starts).
FLUX_EOT, FLUX_EAGER_EOT = 0.7, 0.5
# Flux ends a turn after this much silence whatever it thinks; the recorder's backstop is a little sooner.
FLUX_TIMEOUT_MS = 3000
# Once the recording is over, Flux sends the words it has and closes within about a quarter second.
FLUX_LAST_WORDS_S = 1.0
FINAL_TIMEOUT_S = 5.0
# Deepgram closes a stream that gets no audio for 10 s; while nobody speaks, this keeps it open.
KEEPALIVE_S = 4.0


class TurnState(StrEnum):
    LISTENING = "listening"
    MAYBE_DONE = "maybe_done"  # worth starting an answer
    DONE = "done"
    FAILED = "failed"  # the service is gone: the recorder's own rules take over


class Session(Protocol):
    # Where the turn stands, for a service that decides when the speaker is done; None for one that doesn't.
    turn_state: Callable[[], TurnState] | None

    def feed(self, pcm: bytes) -> None:
        """16 kHz mono int16 audio, as it's recorded."""

    def transcript(self) -> str:
        """The words in everything fed so far. More audio may follow until finish()."""

    def finish(self) -> None:
        """The recording is over. May do nothing."""

    def close(self) -> None: ...


class Transcriber(Protocol):
    def session(self) -> Session: ...


def deepgram_connect(url: str, key: str, open_timeout: float = CONNECT_S, **kwargs):
    from websockets.sync.client import connect

    return connect(url, additional_headers={"Authorization": f"Token {key}"}, open_timeout=open_timeout, **kwargs)


class BufferedSession:
    """For services that take a finished recording: keeps the audio and sends it all when asked."""

    turn_state = None

    def __init__(self, transcribe: Callable[[bytes], str]):
        self._transcribe = transcribe
        self._audio = bytearray()

    def feed(self, pcm: bytes) -> None:
        self._audio += pcm

    def transcript(self) -> str:
        return self._transcribe(bytes(self._audio))

    def finish(self) -> None:
        pass

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
            file=("speech.wav", wav_bytes(pcm), "audio/wav"),
            language=self._cfg.language or None,
        )
        return result.text.strip()


class DeepgramTranscriber:
    """Deepgram's streaming speech to text, recognized while you talk, so the transcript is ready a moment after
    you stop."""

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

    turn_state = None

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

    def finish(self) -> None:
        pass  # each transcript() flushes what Deepgram has heard

    def close(self) -> None:
        self._outbox.put(None)

    def _run(self, url: str, key: str) -> None:
        try:
            with deepgram_connect(url, key) as ws:
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


class FluxTranscriber:
    """Deepgram's Flux: transcribes while you talk and also decides when you're done, from how you sound and what
    you've said."""

    def __init__(self, api_key: str):
        self._key = api_key
        params = {
            "model": "flux-general-en",
            "encoding": "linear16",
            "sample_rate": SAMPLE_RATE,
            "eot_threshold": FLUX_EOT,
            "eager_eot_threshold": FLUX_EAGER_EOT,
            "eot_timeout_ms": FLUX_TIMEOUT_MS,
        }
        self._url = f"{FLUX_URL}?{urlencode(params)}"

    def session(self) -> Session:
        return FluxSession(self._url, self._key)


class FluxSession:
    """Connects in the background as soon as it's made, like DeepgramSession, and follows the turn as Flux calls it
    (turn_state()). Flux closes a connection that's sent a KeepAlive, so none is sent; it keeps an idle one open
    well past the few seconds TARS waits for a follow-up."""

    def __init__(self, url: str, key: str):
        self._outbox: queue.Queue[bytes | None] = queue.Queue()
        self._changed = threading.Condition()
        self._state, self._text = TurnState.LISTENING, ""
        self._error: Exception | None = None
        self._fed = self._finished = self._closed = False
        threading.Thread(target=self._run, args=(url, key), daemon=True, name="flux").start()

    def feed(self, pcm: bytes) -> None:
        self._fed = True
        self._outbox.put(pcm)

    def turn_state(self) -> TurnState:
        with self._changed:
            return TurnState.FAILED if self._failed() else self._state

    def finish(self) -> None:
        """The recording is over, whoever decided it: Flux gets no more audio, sends what it heard and closes."""
        with self._changed:
            self._finished = True
        self._outbox.put(None)

    def transcript(self) -> str:
        """The words so far, at once, until the recording is finished; then the turn's last words, which Flux sends
        within a moment. "" if nobody said anything. Raises if Flux failed before the turn was over, even with some
        words in: they may be only the start, and the backup has the whole recording."""
        if not self._fed:
            return ""
        with self._changed:
            if self._finished and self._state != TurnState.DONE:
                self._changed.wait_for(
                    lambda: self._state == TurnState.DONE or self._closed or self._error is not None, FLUX_LAST_WORDS_S
                )
            if self._failed():
                raise self._error
            if self._text or self._closed or not self._finished:
                return self._text.strip()
            raise TimeoutError("Flux didn't send the last words in time")

    def _failed(self) -> bool:
        return self._error is not None and self._state != TurnState.DONE

    def close(self) -> None:
        self._outbox.put(None)

    def _run(self, url: str, key: str) -> None:
        try:
            with deepgram_connect(url, key) as ws:
                threading.Thread(target=self._receive, args=(ws,), daemon=True, name="flux-receive").start()
                while (item := self._outbox.get()) is not None:
                    ws.send(item)
                ws.send(json.dumps({"type": "CloseStream"}))
        except Exception as e:  # noqa: BLE001 - FAILED for the recorder; transcript() raises for the fallback
            self._fail(e)

    def _receive(self, ws) -> None:
        try:
            for message in ws:
                event = json.loads(message)
                if event.get("type") != "TurnInfo":
                    continue
                with self._changed:
                    if self._state == TurnState.DONE:
                        continue  # the turn is over: whatever comes next belongs to someone else's turn
                    self._text = event.get("transcript") or self._text
                    kind = event.get("event")
                    if kind == "EagerEndOfTurn":
                        self._state = TurnState.MAYBE_DONE
                    elif kind == "TurnResumed":
                        self._state = TurnState.LISTENING
                    elif kind == "EndOfTurn":
                        self._state = TurnState.DONE
                    self._changed.notify_all()
            with self._changed:
                self._closed = True
                self._changed.notify_all()
        except Exception as e:  # noqa: BLE001 - FAILED for the recorder; transcript() raises for the fallback
            self._fail(e)

    def _fail(self, error: Exception) -> None:
        with self._changed:
            self._error = self._error or error
            self._changed.notify_all()


class FallbackTranscriber:
    """A streaming transcriber whose audio is also kept, so a backup can transcribe the recording if the stream
    fails."""

    def __init__(self, transcriber: Transcriber, backup: Transcriber):
        self._transcriber, self._backup = transcriber, backup

    def session(self) -> Session:
        return _FallbackSession(self._transcriber.session(), self._backup.session())


class _FallbackSession:
    def __init__(self, main: Session, backup: Session):
        self._main, self._backup = main, backup
        # A main session that decides when the turn is over keeps doing so; if it fails, the recorder's own
        # silence rule ends the turn and the backup transcribes.
        self.turn_state = main.turn_state

    def finish(self) -> None:
        self._main.finish()

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
