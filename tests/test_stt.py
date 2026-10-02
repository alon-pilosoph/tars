"""Speech to text: Deepgram's stream, and OpenAI transcribing the same recording when the stream fails."""

import json
import threading
import types

import pytest

from voice_assistant import stt
from voice_assistant.stt import BufferedSession, DeepgramSession, FallbackTranscriber, FluxSession, TurnState

from .conftest import ScriptedTranscriber


class FakeDeepgram:
    """A websocket that answers each Finalize with what it was told to have heard so far."""

    def __init__(self, heard=(), answer=True):
        self.heard, self.answer = list(heard), answer
        self.sent, self.inbox = [], []
        self.ready = threading.Condition()
        self.closed = False

    def __call__(self, url, additional_headers=None, open_timeout=None, **kwargs):
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


def connected(monkeypatch, session_class):
    """Opens a session of `session_class` on whatever socket (or failing connect) it's given."""

    def use(fake):
        import websockets.sync.client

        monkeypatch.setattr(websockets.sync.client, "connect", fake)
        return session_class("wss://test", "key")

    return use


@pytest.fixture
def deepgram(monkeypatch):
    return connected(monkeypatch, DeepgramSession)


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


def test_a_quiet_stream_is_kept_open(deepgram, monkeypatch):
    monkeypatch.setattr(stt, "KEEPALIVE_S", 0.05)
    fake = FakeDeepgram()
    session = deepgram(fake)
    for _ in range(100):
        if '{"type": "KeepAlive"}' in fake.sent:
            break
        threading.Event().wait(0.01)
    session.close()
    assert '{"type": "KeepAlive"}' in fake.sent


class BrokenStream:
    def session(self):
        return self

    def feed(self, pcm):
        pass

    turn_state = None

    def transcript(self):
        raise ConnectionError("the stream dropped")

    def finish(self):
        pass

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


class FakeFlux(FakeDeepgram):
    """A Flux websocket: sends the turn events it's given, one each time a block of audio arrives."""

    def __init__(self, events):
        super().__init__()
        self.events = list(events)

    def send(self, item):
        self.sent.append(item)
        if isinstance(item, bytes) and self.events:
            kind, words = self.events.pop(0)
            with self.ready:
                self.inbox.append(json.dumps({"type": "TurnInfo", "event": kind, "transcript": words}))
                self.ready.notify_all()


class FluxThatDrops(FakeFlux):
    """Sends its events, then the connection breaks on the next audio."""

    def send(self, item):
        if isinstance(item, bytes) and not self.events:
            raise ConnectionResetError("Flux went away")
        super().send(item)


@pytest.fixture
def flux(monkeypatch):
    return connected(monkeypatch, FluxSession)


def wait_for(session, state):
    for _ in range(200):
        if session.turn_state() == state:
            return
        threading.Event().wait(0.01)
    raise AssertionError(f"never reached {state}, stuck at {session.turn_state()}")


def wait_for_text(session, text):
    for _ in range(200):
        if session.transcript() == text:
            return
        threading.Event().wait(0.01)
    raise AssertionError(f"never heard {text!r}")


def test_flux_follows_the_turn_and_its_words(flux):
    session = flux(
        FakeFlux(
            [
                ("StartOfTurn", "what's"),
                ("Update", "what's the capital"),
                ("EagerEndOfTurn", "what's the capital of"),
                ("TurnResumed", "what's the capital of"),
                ("EndOfTurn", "what's the capital of Australia"),
                ("StartOfTurn", "somebody else"),
            ]
        )
    )
    for _ in range(3):
        session.feed(b"\0\0")
    wait_for(session, TurnState.MAYBE_DONE)
    assert session.transcript() == "what's the capital of"  # a draft gets the words so far, right away
    session.feed(b"\0\0")
    wait_for(session, TurnState.LISTENING)
    session.feed(b"\0\0")
    wait_for(session, TurnState.DONE)
    session.feed(b"\0\0")  # the next turn's words don't overwrite this one's
    assert session.transcript() == "what's the capital of Australia"
    session.close()


def test_a_recording_that_ends_before_flux_does_gets_flux_s_last_words(flux):
    session = flux(FakeFlux([("StartOfTurn", "what should"), ("Update", "what should I cook, um")]))
    session.feed(b"\0\0")
    session.feed(b"\0\0")
    wait_for_text(session, "what should I cook, um")  # a draft reads the words so far without waiting
    session.finish()  # the backstop ended the turn: Flux sends what it has and closes
    assert session.transcript() == "what should I cook, um"


def test_a_turn_with_no_words_is_empty_not_an_error(flux):
    session = flux(FakeFlux([]))
    session.feed(b"\0\0")
    session.finish()
    assert session.transcript() == ""


def test_flux_failing_hands_over_to_the_recorder_and_the_backup(flux):
    def broken(url, additional_headers=None, open_timeout=None, **kwargs):
        raise ConnectionRefusedError("no Flux today")

    session = flux(broken)
    session.feed(b"\0\0")
    wait_for(session, TurnState.FAILED)
    session.finish()
    with pytest.raises(ConnectionRefusedError):
        session.transcript()


def with_backup(session, backup_text):
    """`session` with a backup that transcribes the recording as `backup_text`."""
    main = types.SimpleNamespace(session=lambda: session)
    backup = types.SimpleNamespace(session=lambda: BufferedSession(lambda pcm: backup_text))
    return FallbackTranscriber(main, backup).session()


def test_flux_failing_after_some_words_asks_the_backup_for_all_of_them(flux):
    session = flux(FluxThatDrops([("StartOfTurn", "what's the weather"), ("Update", "what's the weather in")]))
    fallback = with_backup(session, "what's the weather in Haifa")
    for _ in range(3):
        fallback.feed(b"\0\0")
    wait_for(session, TurnState.FAILED)
    assert fallback.turn_state() == TurnState.FAILED  # the recorder's own rules end the turn
    assert fallback.transcript() == "what's the weather in Haifa"  # a draft's words, from the whole recording
    fallback.finish()
    assert fallback.transcript() == "what's the weather in Haifa"


def test_the_fallback_session_passes_flux_s_turns_through(flux):
    fallback = with_backup(flux(FakeFlux([("EndOfTurn", "what time is it")])), "the backup")
    fallback.feed(b"\0\0")
    wait_for(fallback, TurnState.DONE)
    fallback.finish()
    assert fallback.transcript() == "what time is it"
    assert with_backup(BufferedSession(lambda pcm: ""), "").turn_state is None
