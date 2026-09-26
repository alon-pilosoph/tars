"""Speech to text: Deepgram's stream, and OpenAI transcribing the same recording when the stream fails."""

import json
import threading

import pytest

from voice_assistant import stt
from voice_assistant.stt import BufferedSession, DeepgramSession, FallbackTranscriber

from .conftest import ScriptedTranscriber


class FakeDeepgram:
    """A websocket that answers each Finalize with what it was told to have heard so far."""

    def __init__(self, heard=(), answer=True):
        self.heard, self.answer = list(heard), answer
        self.sent, self.inbox = [], []
        self.ready = threading.Condition()
        self.closed = False

    def __call__(self, url, additional_headers=None, open_timeout=None):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        with self.ready:
            self.closed = True
            self.ready.notify_all()

    def send(self, item):
        self.sent.append(item)
        if isinstance(item, str) and json.loads(item)["type"] == "Finalize" and self.answer:
            words = self.heard.pop(0) if self.heard else ""
            with self.ready:
                self.inbox.append(
                    json.dumps(
                        {
                            "type": "Results",
                            "is_final": True,
                            "from_finalize": True,
                            "channel": {"alternatives": [{"transcript": words}]},
                        }
                    )
                )
                self.ready.notify_all()

    def __iter__(self):
        while True:
            with self.ready:
                self.ready.wait_for(lambda: self.inbox or self.closed)
                if not self.inbox:
                    return
                message = self.inbox.pop(0)
            yield message


@pytest.fixture
def deepgram(monkeypatch):
    def use(fake):
        import websockets.sync.client

        monkeypatch.setattr(websockets.sync.client, "connect", fake)
        return DeepgramSession("wss://test", "key")

    return use


def test_the_words_so_far_and_then_the_rest(deepgram):
    session = deepgram(FakeDeepgram(["what's the weather", "in Haifa"]))
    session.feed(b"12")
    assert session.transcript() == "what's the weather"
    session.feed(b"34")
    assert session.transcript() == "what's the weather in Haifa"
    session.close()


def test_nothing_fed_means_nothing_heard_without_asking(deepgram):
    fake = FakeDeepgram(["never"])
    session = deepgram(fake)
    assert session.transcript() == ""
    session.close()


def test_a_connection_that_fails_raises_instead_of_returning_nothing(deepgram):
    def refuse(*args, **kwargs):
        raise ConnectionRefusedError("no route to Deepgram")

    session = deepgram(refuse)
    session.feed(b"12")
    with pytest.raises(ConnectionRefusedError):
        session.transcript()


def test_no_answer_times_out(deepgram, monkeypatch):
    monkeypatch.setattr(stt, "FINAL_TIMEOUT_S", 0.2)
    session = deepgram(FakeDeepgram(answer=False))
    session.feed(b"12")
    with pytest.raises(TimeoutError):
        session.transcript()
    session.close()


class BrokenStream:
    def session(self):
        return self

    def feed(self, pcm):
        pass

    def transcript(self):
        raise ConnectionError("the stream dropped")

    def close(self):
        pass


def test_a_dropped_stream_is_transcribed_by_the_backup_from_the_same_audio():
    heard = []

    class Backup:
        def session(self):
            return BufferedSession(lambda pcm: heard.append(pcm) or "from the backup")

    session = FallbackTranscriber(BrokenStream(), Backup()).session()
    session.feed(b"12")
    session.feed(b"34")
    assert session.transcript() == "from the backup" and heard == [b"1234"]


def test_a_working_stream_never_calls_the_backup():
    session = FallbackTranscriber(ScriptedTranscriber(["streamed"]), ScriptedTranscriber([])).session()
    session.feed(b"12")
    assert session.transcript() == "streamed"
