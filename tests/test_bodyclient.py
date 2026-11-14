"""The body's side of the body link (bodyclient.py): its rules on its own, then against the brain's side over a real
socket, so the two halves agree."""

import json
import threading
import time
import types

import numpy as np
import pytest
from websockets.sync.client import connect

from voice_assistant import bodyclient
from voice_assistant.bodyclient import BodyProtocol, hello
from voice_assistant.bodykeys import BodyKeys, parse_key
from voice_assistant.bodylink import BodyListener, RemoteBody, welcome

WELCOME = json.dumps({"type": "welcome", "room": "Kitchen", "play_rate": 24000, "max_listen_s": 10.0})


class FakePlayer:
    def __init__(self):
        self.did = []

    def start(self, play_id):
        self.did.append(("start", play_id))

    def audio(self, data):
        self.did.append(("audio", len(data)))

    def end(self, play_id):
        self.did.append(("end", play_id))

    def stop(self):
        self.did.append(("stop",))


def body(welcomed=True):
    sent, player = [], FakePlayer()
    protocol = BodyProtocol(sent.append, player)
    if welcomed:
        protocol.brain(WELCOME)
    return protocol, sent, player


def block(level=1000):
    return np.full(1280, level, np.int16)


def kinds(sent):
    return [m["type"] if isinstance(m, dict) else f"audio {len(m)}" for m in sent]


def test_nothing_is_streamed_before_its_own_wake_whatever_the_brain_says():
    protocol, sent, _ = body()
    protocol.heard(block())
    protocol.brain(json.dumps({"type": "listen", "on": True}))  # from sleep: not honoured
    protocol.heard(block())
    assert sent == [] and not protocol.listening


def test_a_wake_sends_its_pre_roll_then_streams_until_the_brain_says_listen_off_or_sleep():
    protocol, sent, _ = body()
    assert protocol.woke("word", 0.93, np.zeros(16000, np.int16))
    protocol.heard(block())
    protocol.brain(json.dumps({"type": "listen", "on": False}))
    protocol.heard(block())
    protocol.brain(json.dumps({"type": "listen", "on": True}))  # inside its own conversation: honoured
    protocol.heard(block())
    protocol.brain(json.dumps({"type": "sleep"}))
    protocol.heard(block())
    assert kinds(sent) == ["wake", "audio 30720", "audio 2560", "audio 2560"]  # the pre-roll in whole blocks
    assert sent[0] == {"type": "wake", "how": "word", "score": 0.93, "preroll_ms": 960}


def test_a_conversation_stops_streaming_at_its_cap_and_says_gone_quiet():
    protocol, sent, _ = body()  # the brain's cap: 10 s
    protocol.woke("button", 0, np.zeros(0, np.int16))
    for _ in range(130):  # 10.4 s
        protocol.heard(block())
    assert kinds(sent).count("gone_quiet") == 1 and kinds(sent).count("audio 2560") == 125
    assert not protocol.awake


def test_the_bodys_own_ceiling_holds_whatever_the_brain_allows(monkeypatch):
    monkeypatch.setattr(bodyclient, "BODY_CEILING_S", 1.0)
    protocol, sent, _ = body(welcomed=False)
    protocol.brain(json.dumps({"type": "welcome", "room": "K", "play_rate": 24000, "max_listen_s": 3600}))
    protocol.woke("button", 0, np.zeros(0, np.int16))
    for _ in range(20):
        protocol.heard(block())
    assert kinds(sent).count("audio 2560") == 12 and "gone_quiet" in kinds(sent)


def test_muted_or_not_connected_it_doesnt_wake():
    protocol, sent, _ = body()
    protocol.brain(json.dumps({"type": "mute", "muted": True}))
    assert not protocol.woke("word", 1, np.zeros(0, np.int16)) and sent == []
    unconnected, sent, _ = body(welcomed=False)
    assert not unconnected.woke("word", 1, np.zeros(0, np.int16)) and sent == []


def test_a_reply_goes_to_the_player_and_its_end_is_said():
    protocol, sent, player = body()
    protocol.brain(json.dumps({"type": "play", "id": 7, "listening": False}))
    protocol.brain(b"\1\0" * 100)
    protocol.brain(json.dumps({"type": "play_end", "id": 7}))
    protocol.played(7, False)
    protocol.brain(json.dumps({"type": "stop"}))
    assert player.did == [("start", 7), ("audio", 200), ("end", 7), ("stop",)]
    assert sent == [{"type": "played", "id": 7, "cut": False}]
    protocol.brain(b"\1\0" * 10)  # audio with nothing playing: not played
    assert player.did[-1] == ("stop",)


def test_a_dropped_link_ends_the_conversation():
    protocol, _, player = body()
    protocol.woke("word", 1, np.zeros(0, np.int16))
    protocol.lost()
    assert not protocol.awake and protocol.welcome is None and player.did == [("stop",)]


# Against the brain's side


@pytest.fixture
def brain(log):
    keys = BodyKeys(log.store)
    key = keys.add("Kitchen")
    listener = BodyListener(keys.public_key, lambda h: welcome("Kitchen", 24000, 10.0), host="127.0.0.1").start()
    yield listener, key
    listener.close()


class AutoPlayer(FakePlayer):
    """Plays at once: the playback ends as soon as its last audio is in."""

    protocol = None

    def end(self, play_id):
        super().end(play_id)
        threading.Thread(target=lambda: self.protocol.played(play_id, False)).start()


def test_a_whole_turn_between_the_two_halves(brain):
    from voice_assistant.assistant import Assistant

    from .conftest import RecordingBrain, ScriptedTranscriber
    from .test_bodylink import BlocksRecorder, reply

    listener, key = brain
    device, seed = parse_key(key)
    ws = connect(f"ws://127.0.0.1:{listener.port}", max_size=None, legacy=True)
    player = AutoPlayer()
    protocol = BodyProtocol(lambda m: ws.send(m if isinstance(m, bytes) else json.dumps(m)), player)
    player.protocol = protocol
    nonce = json.loads(ws.recv(timeout=5))["nonce"]
    ws.send(json.dumps(hello(device, seed, nonce, echo_cancel=False)))

    def read():
        for message in ws:
            protocol.brain(message)

    threading.Thread(target=read, daemon=True).start()
    link = listener.connected(device, timeout=5)
    remote = RemoteBody(link, 24000, BlocksRecorder())
    voice = types.SimpleNamespace(sample_rate=24000, stream=lambda text: iter(reply(0.05, 0.3)), warm=lambda: None)
    llm = RecordingBrain(["It's sunny."])
    assistant = Assistant(remote, ScriptedTranscriber(["what's the weather"]), llm, voice)

    def speak():
        deadline = time.monotonic() + 5
        while protocol.welcome is None and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)  # the brain's sleep first
        protocol.woke("word", 0.95, np.full(4000, 2000, np.int16))
        speech = np.concatenate([np.full(12800, 3000, np.int16), np.zeros(6400, np.int16)])
        for start in range(0, len(speech), 1280):
            protocol.heard(speech[start : start + 1280])
        while protocol.awake:  # room tone, while the brain listens on
            protocol.heard(np.zeros(1280, np.int16))
            time.sleep(0.02)

    talking = threading.Thread(target=speak, daemon=True)
    talking.start()
    wake = remote.wait(lambda: False)
    assistant.converse(0.0, wake=wake)
    assert (
        llm.asked == ["what's the weather"] and wake.how == "answer" and len(wake.audio) == 3840
    )  # whole blocks, the newest
    assert ("start", 1) in player.did and ("end", 1) in player.did
    remote.wait(lambda: True)  # the conversation's over: the brain puts the body to sleep
    deadline = time.monotonic() + 5
    while protocol.awake and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not protocol.awake
    ws.close()


def test_the_brain_serves_whichever_body_comes_and_waits_again_when_it_goes(log, monkeypatch):
    import dataclasses
    from pathlib import Path

    from voice_assistant import wiring
    from voice_assistant.config import BodiesConfig, load_config

    keys = BodyKeys(log.store)
    key = keys.add("Kitchen")
    device, seed = parse_key(key)
    cfg = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    cfg = dataclasses.replace(cfg, bodies=BodiesConfig(enabled=True, host="127.0.0.1", port=0))
    monkeypatch.setattr(wiring, "make_trigger", lambda *a: (None, ""))
    monkeypatch.setattr("voice_assistant.recorder.make_recorder", lambda cfg, root: None)
    port = {}
    real = BodyListener.start

    def started(self):
        port["n"] = self.port
        return real(self)

    monkeypatch.setattr(BodyListener, "start", started)
    bodies = wiring.network_bodies(cfg, Path("."), log.store, 24000, None)
    got = {}
    first = threading.Thread(target=lambda: got.update(first=next(bodies)))
    first.start()

    def come():
        deadline = time.monotonic() + 5
        while "n" not in port and time.monotonic() < deadline:
            time.sleep(0.01)
        ws = connect(f"ws://127.0.0.1:{port['n']}", legacy=True)
        nonce = json.loads(ws.recv(timeout=5))["nonce"]
        ws.send(json.dumps(hello(device, seed, nonce, echo_cancel=False)))
        assert json.loads(ws.recv(timeout=5))["room"] == "Kitchen"
        return ws

    ws = come()
    first.join(5)
    body, idle = got["first"]
    assert idle == "Waiting for its name in the Kitchen..." and body.link.hello.device == device
    ws.close()
    deadline = time.monotonic() + 5
    while not body.link.gone.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    again = threading.Thread(target=lambda: got.update(again=next(bodies)))
    again.start()
    ws = come()
    again.join(5)
    assert got["again"][0].link is not body.link and not got["again"][0].link.gone.is_set()
    ws.close()


def test_the_status_page_expects_the_voice_assistant_for_a_body_in_another_room():
    import dataclasses
    from pathlib import Path

    from voice_assistant.config import AudioConfig, BodiesConfig, load_config
    from voice_assistant.health import expected

    cfg = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    chat_only = dataclasses.replace(cfg, audio=AudioConfig(enabled=False))
    assert "assistant" not in expected(chat_only)
    assert "assistant" in expected(dataclasses.replace(chat_only, bodies=BodiesConfig(enabled=True)))
