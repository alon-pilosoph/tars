"""The Deepgram voice: connections kept warm and reused, and each model family's end-of-sentence."""

import json
import time

import pytest
from websockets.exceptions import ConnectionClosed

from voice_assistant.config import TTSConfig
from voice_assistant.tts import DeepgramSpeech


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
    return v


@pytest.mark.parametrize("model, flux", [("flux-cliff-en", True), ("aura-2-zeus-en", False)])
def test_a_sentence_is_its_audio_and_the_connection_is_used_again(model, flux):
    socket = FakeSocket(flux)
    v = voice(model, [socket])
    assert b"".join(v.stream("Hello there.")) == b"Hello there."
    assert b"".join(v.stream("Again.")) == b"Again."  # the same connection: no second one was needed
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
