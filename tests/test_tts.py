"""The websocket voices: connections kept warm and reused, and each service's end of a sentence."""

import base64
import json
import threading
import time

import pytest
from websockets.exceptions import ConnectionClosed

from voice_assistant.config import TTSConfig
from voice_assistant.tts import CartesiaSpeech, DeepgramSpeech


class FakeSocket:
    """Answers each Speak the way the model family does: Aura-2 audio then Flushed; Flux Flushed, audio, metadata."""

    def __init__(self, flux: bool, dead: bool = False):
        self.flux, self.dead, self.closed = flux, dead, False
        self.inbox: list = []
        self.spoken: list[str] = []

    def send(self, message):
        if self.dead:
            raise ConnectionClosed(None, None)
        event = json.loads(message)
        if event["type"] == "Speak":
            self.spoken.append(event["text"])
            audio = [event["text"].encode()[:4], event["text"].encode()[4:]]
            flushed = json.dumps({"type": "Flushed"})
            self.pending = (
                [flushed, *audio, json.dumps({"type": "SpeechMetadata"})]
                if self.flux
                else [json.dumps({"type": "Metadata"}), *audio, flushed]
            )
        elif event["type"] == "Flush":
            self.inbox += self.pending

    def recv(self, timeout=None):
        return self.inbox.pop(0)

    def close(self):
        self.closed = True


def voice(model: str, sockets: list) -> DeepgramSpeech:
    v = DeepgramSpeech("key", TTSConfig(provider="deepgram", model=model))
    v._connect = lambda: sockets.pop(0)
    v._discard = lambda ws: ws.close()  # the real one closes on another thread
    return v


@pytest.mark.parametrize("model, flux", [("flux-cliff-en", True), ("aura-2-zeus-en", False)])
def test_a_sentence_is_its_audio_and_the_connection_is_used_again(model, flux):
    socket = FakeSocket(flux)
    v = voice(model, [socket])
    assert b"".join(v.stream("Hello there.")) == b"Hello there."
    assert b"".join(v.stream("Again.")) == b"Again."
    assert socket.spoken == ["Hello there.", "Again."] and not socket.closed


def test_a_sentence_cut_short_never_leaves_its_audio_on_a_reused_connection():
    first, second = FakeSocket(True), FakeSocket(True)
    v = voice("flux-cliff-en", [first, second])
    chunks = v.stream("Cut me off.")
    next(chunks)
    chunks.close()  # the reply was stopped mid-sentence
    assert first.closed
    assert b"".join(v.stream("Next.")) == b"Next." and second.spoken == ["Next."]


def test_a_warm_connection_that_died_is_replaced_once():
    dead, fresh = FakeSocket(True, dead=True), FakeSocket(True)
    v = voice("flux-cliff-en", [fresh])
    v._idle.put((time.monotonic(), dead))
    assert b"".join(v.stream("Still here.")) == b"Still here." and dead.closed


def test_an_old_idle_connection_is_not_trusted():
    old, fresh = FakeSocket(True), FakeSocket(True)
    v = voice("flux-cliff-en", [fresh])
    v._idle.put((-1000.0, old))
    assert b"".join(v.stream("Hi.")) == b"Hi." and old.closed and old.spoken == []


def test_a_deepgram_error_ends_the_reply_with_the_reason():
    socket = FakeSocket(True)
    socket.send = lambda message: socket.inbox.append(json.dumps({"type": "Error", "description": "bad model"}))
    v = voice("flux-nobody-en", [socket])
    with pytest.raises(RuntimeError, match="bad model"):
        b"".join(v.stream("Hi."))
    assert socket.closed


class DiesOnRecv(FakeSocket):
    """Takes the text, then turns out closed: the server dropped it while it sat idle."""

    def recv(self, timeout=None):
        raise ConnectionClosed(None, None)


class Stalls(FakeSocket):
    def recv(self, timeout=None):
        raise TimeoutError("nothing for 4 s")


def test_a_warm_connection_found_closed_before_any_audio_is_replaced():
    dead, fresh = DiesOnRecv(True), FakeSocket(True)
    v = voice("flux-cliff-en", [fresh])
    v._idle.put((time.monotonic(), dead))
    assert b"".join(v.stream("Still here.")) == b"Still here." and dead.closed


def test_a_stalled_voice_is_not_retried():
    stalled, spare = Stalls(True), FakeSocket(True)
    v = voice("flux-cliff-en", [stalled, spare])
    with pytest.raises(TimeoutError):
        b"".join(v.stream("Hi."))
    assert stalled.closed and spare.spoken == []  # no second 4 s wait


def test_no_more_than_a_few_idle_connections_are_kept():
    sockets = [FakeSocket(True) for _ in range(6)]
    v = voice("flux-cliff-en", list(sockets))
    v._turns = threading.BoundedSemaphore(6)  # more in flight than KEEP, as with a service that allows it
    streams = [v.stream(f"Sentence {i}.") for i in range(6)]
    for s_ in streams:
        next(s_)  # six sentences synthesizing at once, like a long reply
    for s_ in streams:
        b"".join(s_)
    assert v._idle.qsize() == DeepgramSpeech.KEEP and sum(s_.closed for s_ in sockets) == 6 - DeepgramSpeech.KEEP


def test_warming_replaces_connections_too_old_to_trust():
    old = FakeSocket(True)
    v = voice("flux-cliff-en", [FakeSocket(True), FakeSocket(True)])
    v._idle.put((-1000.0, old))
    v._top_up()
    assert old.closed and v._idle.qsize() == DeepgramSpeech.READY


class CartesiaSocket:
    """Answers each request the way Cartesia does: base64 chunks for its context, then done."""

    def __init__(self, error: str = ""):
        self.error, self.closed = error, False
        self.inbox: list = []
        self.requests: list[dict] = []

    def send(self, message):
        request = json.loads(message)
        self.requests.append(request)
        if self.error:
            self.inbox.append(json.dumps({"type": "error", "message": self.error}))
            return
        audio = request["transcript"].encode()
        self.inbox += [
            json.dumps({"type": "chunk", "data": base64.b64encode(part).decode(), "context_id": request["context_id"]})
            for part in (audio[:4], audio[4:])
        ]
        self.inbox.append(json.dumps({"type": "done", "done": True, "context_id": request["context_id"]}))

    def recv(self, timeout=None):
        return self.inbox.pop(0)

    def close(self):
        self.closed = True


def cartesia(sockets: list) -> CartesiaSpeech:
    v = CartesiaSpeech("key", TTSConfig(provider="cartesia", model="sonic-3.6", cartesia_voice="voice-id"))
    v._connect = lambda: sockets.pop(0)
    v._discard = lambda ws: ws.close()
    return v


def test_cartesia_speaks_each_sentence_as_its_own_context_on_a_reused_connection():
    socket = CartesiaSocket()
    v = cartesia([socket])
    assert b"".join(v.stream("Hello there.")) == b"Hello there."
    assert b"".join(v.stream("Again.")) == b"Again."
    first, second = socket.requests
    assert first["transcript"] == "Hello there." and first["context_id"] != second["context_id"]
    assert (first["model_id"], first["voice"], first["continue"]) == ("sonic-3.6", "voice-id", False)
    assert first["output_format"] == {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24_000}
    assert not socket.closed


def test_a_cartesia_error_ends_the_reply_with_the_reason():
    socket = CartesiaSocket(error="voice not found")
    with pytest.raises(RuntimeError, match="voice not found"):
        b"".join(cartesia([socket]).stream("Hi."))
    assert socket.closed


class Slow(FakeSocket):
    """Speaks only when the test lets it, so several sentences are in flight at once."""

    def __init__(self, gate, busy):
        super().__init__(True)
        self.gate, self.busy = gate, busy

    def send(self, message):
        if json.loads(message)["type"] == "Speak":
            self.busy.append(self)
            self.gate.wait()
        super().send(message)


def test_no_more_than_a_few_sentences_synthesize_at_once_and_the_rest_wait_their_turn():
    gate, busy = threading.Event(), []
    v = voice("flux-wes-en", [Slow(gate, busy) for _ in range(6)])
    results: list[bytes] = []
    threads = [
        threading.Thread(target=lambda i=i: results.append(b"".join(v.stream(f"Sentence {i}.")))) for i in range(6)
    ]
    for t in threads:
        t.start()
    time.sleep(0.2)
    in_flight = len(busy)
    gate.set()
    for t in threads:
        t.join(timeout=5)
    assert in_flight == DeepgramSpeech.AT_ONCE
    assert sorted(results) == sorted(f"Sentence {i}.".encode() for i in range(6))


def test_cartesia_makes_no_more_sentences_at_once_than_its_free_plan_allows():
    assert cartesia([])._turns._value == CartesiaSpeech.AT_ONCE == 2
