"""Speech to text: Deepgram's stream, and OpenAI transcribing the same recording when the stream fails; and
whole recordings (a voice message), to Deepgram's endpoint for them, then OpenAI."""

import json
import threading
import types

import httpx
import numpy as np
import pytest

from voice_assistant import stt
from voice_assistant.audio import decode_recording
from voice_assistant.errors import ServiceError
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
    with pytest.raises(ServiceError, match="no route to Deepgram") as e:
        session.transcript()
    assert isinstance(e.value.__cause__, ConnectionRefusedError)


def test_no_answer_times_out(deepgram, monkeypatch):
    monkeypatch.setattr(stt, "FINAL_TIMEOUT_S", 0.2)
    session = deepgram(FakeDeepgram(answer=False))
    session.feed(b"12")
    with pytest.raises(ServiceError, match="no transcript in time"):
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


def test_once_the_stream_fails_the_backup_writes_down_the_rest_without_waiting_on_it_again():
    class Stalled(BrokenStream):
        tries = 0

        def transcript(self):
            self.tries += 1  # each one a FINAL_TIMEOUT_S wait, on a stream that's connected but stalled
            raise ServiceError("Deepgram didn't finish the transcript in time")

    stalled = Stalled()
    session = FallbackTranscriber(stalled, ScriptedTranscriber(["what's the", "what's the time"])).session()
    session.feed(b"12")
    assert session.transcript() == "what's the"  # at a pause
    session.feed(b"34")
    assert session.transcript() == "what's the time" and stalled.tries == 1  # at the end


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


class FluxThatGoesQuiet(FakeFlux):
    """Never ends the turn, and stays open after the recording is over: no last words, and no close."""

    def __exit__(self, *exc):
        pass


def going_quiet(monkeypatch, has_backup):
    """A Flux session that heard "turn off the kitchen" and then never ends the turn, nor closes."""
    monkeypatch.setattr(stt, "FLUX_LAST_WORDS_S", 0.05)
    flux = connected(monkeypatch, lambda url, key: FluxSession(url, key, has_backup=has_backup))
    session = flux(FluxThatGoesQuiet([("StartOfTurn", "turn off the"), ("Update", "turn off the kitchen")]))
    session.feed(b"\0\0")
    session.feed(b"\0\0")
    wait_for_text(session, "turn off the kitchen")
    return session


def test_flux_that_never_sends_the_last_words_hands_the_whole_recording_to_the_backup(monkeypatch):
    fallback = with_backup(going_quiet(monkeypatch, has_backup=True), "turn off the kitchen lights")
    fallback.finish()  # the recorder's backstop ended the turn, and Flux never says it's over
    assert fallback.transcript() == "turn off the kitchen lights"


def test_without_a_backup_flux_that_never_sends_the_last_words_gives_the_words_it_has(monkeypatch):
    session = going_quiet(monkeypatch, has_backup=False)
    session.finish()
    assert session.transcript() == "turn off the kitchen"


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
    with pytest.raises(ServiceError, match="no Flux today"):
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


# A whole recording: a voice message on Telegram.


def recorded(handler):
    """A DeepgramFileTranscriber over a pretend Deepgram that answers with `handler(request)`."""
    from voice_assistant.config import STTConfig

    cfg = STTConfig(provider="flux", deepgram_host="api.eu.deepgram.com")
    return stt.DeepgramFileTranscriber("dg-key", cfg, httpx.Client(transport=httpx.MockTransport(handler)))


def test_a_whole_recording_goes_to_deepgrams_endpoint_for_them_on_its_eu_servers():
    seen = []

    def handler(request):
        seen.append(request)
        alternatives = [{"transcript": " remind me to call mum at six "}]
        return httpx.Response(200, json={"results": {"channels": [{"alternatives": alternatives}]}})

    pcm = np.zeros(1600, dtype=np.int16)
    assert stt.transcribe_recording(recorded(handler), pcm) == "remind me to call mum at six"
    (request,) = seen
    assert request.url.host == "api.eu.deepgram.com" and request.url.path == "/v1/listen"
    assert request.url.params["model"] == "nova-3"  # Flux has no endpoint for whole recordings
    assert request.headers["Authorization"] == "Token dg-key" and request.content.startswith(b"RIFF")


@pytest.mark.parametrize(
    "answer", [httpx.Response(503), httpx.Response(200, text="not json"), httpx.Response(200, json={"results": {}})]
)
def test_a_whole_recording_deepgram_cant_write_down_is_the_service_failing(answer):
    with pytest.raises(ServiceError, match="Deepgram speech to text"):
        stt.transcribe_recording(recorded(lambda request: answer), np.zeros(1600, dtype=np.int16))


def test_a_whole_recording_deepgram_cant_take_goes_to_the_backup():
    deepgram = recorded(lambda request: httpx.Response(503))
    pcm = np.zeros(1600, dtype=np.int16)
    assert stt.transcribe_recording(FallbackTranscriber(deepgram, ScriptedTranscriber(["heard"])), pcm) == "heard"


def test_a_recording_is_read_as_16_khz_samples_whatever_it_was():
    import io

    import soundfile

    t = np.linspace(0, 1.5, 72_000, endpoint=False)
    tone = (0.5 * np.sin(2 * np.pi * 300 * t)).astype("float32")
    voice = io.BytesIO()
    soundfile.write(voice, tone, 48_000, format="OGG", subtype="OPUS")  # as Telegram sends a voice message
    pcm = decode_recording(voice.getvalue())
    assert pcm.dtype == np.int16 and abs(len(pcm) - 24_000) < 400
    middle = pcm[4_000:-4_000].astype(np.float64) / 32767  # Opus overshoots a little at the edges
    assert 0.3 < np.sqrt(np.mean(middle**2)) < 0.42  # the same loudness, near enough: a 0.5 sine's RMS is 0.354
    with pytest.raises(ValueError, match="can't read the recording") as e:
        decode_recording(b"not audio at all")
    assert "BytesIO" not in str(e.value)


def test_voice_messages_go_to_deepgram_then_openai_as_requests_at_home_do(tmp_path, monkeypatch):
    from pathlib import Path

    from voice_assistant.__main__ import make_recording_transcriber
    from voice_assistant.config import load_config, with_keys

    asked = []
    deepgram_up = [True]

    def deepgram(request):
        asked.append("deepgram")
        if not deepgram_up[0]:
            return httpx.Response(503)
        return httpx.Response(200, json={"results": {"channels": [{"alternatives": [{"transcript": "by Deepgram"}]}]}})

    def openai_transcribes(**kw):
        asked.append("openai")
        return types.SimpleNamespace(text="by OpenAI")

    openai = types.SimpleNamespace(audio=types.SimpleNamespace(transcriptions=types.SimpleNamespace(create=None)))
    openai.audio.transcriptions.create = openai_transcribes
    openai.with_options = lambda **kw: openai
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(deepgram)))
    shipped = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    env = tmp_path / ".env"
    env.write_text("DEEPGRAM_API_KEY=x\nOPENAI_API_KEY=x\n")
    pcm = np.zeros(1600, dtype=np.int16)

    def heard(keys: set[str], client) -> str:
        cfg, _ = with_keys(shipped, keys | {"CARTESIA_API_KEY", "GROQ_API_KEY"})
        return stt.transcribe_recording(make_recording_transcriber(cfg, env, client), pcm)

    both = {"DEEPGRAM_API_KEY", "OPENAI_API_KEY"}
    assert heard(both, openai) == "by Deepgram" and asked == ["deepgram"]
    deepgram_up[0] = False
    assert heard(both, openai) == "by OpenAI" and asked[1:] == ["deepgram", "openai"]
    assert heard({"OPENAI_API_KEY"}, openai) == "by OpenAI" and asked[-1] == "openai"
    deepgram_up[0] = True
    assert heard({"DEEPGRAM_API_KEY"}, None) == "by Deepgram"
