"""A phone call as a body (phone.py): who's let in, the audio both ways, and a call through to the brain's side."""

import base64
import json
import re
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voice_assistant import phone
from voice_assistant.bodykeys import BodyKeys, Tickets
from voice_assistant.bodylink import BodyListener, welcome
from voice_assistant.phone import Call, Gate, Settings, make_app, mulaw_decode, mulaw_encode, signature

SETTINGS = Settings("secret", "https://tars.example", frozenset({"+15550100"}), "246810", "Robin")
WELCOME = json.dumps({"type": "welcome", "room": "r", "play_rate": 24000, "max_listen_s": 120})


def client(ticket=lambda: "t", url="ws://127.0.0.1:1", gate=None):
    return TestClient(make_app(SETTINGS, url, ticket, gate))


def post(web, path, signed=True, **fields):
    fields = {"From": "+15550100", "CallSid": "CA1", **fields}
    sig = signature("secret", SETTINGS.url + path, list(fields.items())) if signed else "no"
    return web.post(path, data=fields, headers={"X-Twilio-Signature": sig}).text


def test_mulaw_there_and_back():
    tone = (8000 * np.sin(np.arange(8000) / 5)).astype(np.int16)
    back = mulaw_decode(mulaw_encode(tone))
    assert np.max(np.abs(back.astype(int) - tone)) < 300
    assert mulaw_encode(np.zeros(1, np.int16)) == b"\xff" and mulaw_decode(b"\x00\x80").tolist() == [-32124, 32124]
    assert mulaw_decode(mulaw_encode(np.array([-32768, 32767], np.int16))).tolist() == [-32124, 32124]


def test_the_signature_is_twilios():  # the example in Twilio's own documentation
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675309", "Digits": "1234", "From": "+14158675309",
              "To": "+18005551212"}  # fmt: skip
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    assert signature("12345", url, list(params.items())) == "RSOYDt4T1cUTdK1PDd93/VVr8B8="


def test_only_twilio_and_only_an_allowed_caller():
    web = client()
    assert "<Gather" in post(web, "/voice")
    assert "<Reject" in post(web, "/voice", signed=False)
    assert "<Reject" in post(web, "/voice", From="+15550199")
    assert "Stream" not in post(web, "/pin", signed=False, Digits="246810")
    assert "Stream" not in post(web, "/pin", Digits="000000")
    assert '<Stream url="wss://tars.example/audio">' in post(web, "/pin", Digits="246810")


def test_wrong_pins_lock_it_and_a_token_is_good_once_for_its_own_call():
    now = [0.0]
    gate = Gate("246810", lambda: now[0])
    token = gate.token("246810", "CA1")
    assert not gate.redeem(token, "CA2") and not gate.redeem(token, "CA1")  # used up by the wrong call
    token = gate.token("246810", "CA1")
    assert gate.redeem(token, "CA1") and not gate.redeem(token, "CA1")
    token = gate.token("246810", "CA1")
    now[0] += phone.TOKEN_S + 1
    assert not gate.redeem(token, "CA1") and not gate.redeem("", "")
    for _ in range(phone.PIN_TRIES):
        assert gate.token("1", "CA1") is None
    assert gate.locked and gate.token("246810", "CA1") is None
    now[0] += phone.LOCKED_S + 1
    assert gate.token("246810", "CA1")


def test_audio_without_a_token_is_closed():
    with client().websocket_connect("/audio") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ", "callSid": "CA1"}}))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_the_settings_must_be_safe():
    env = {"TWILIO_AUTH_TOKEN": "x", "TARS_PHONE_URL": "https://a", "TARS_PHONE_ALLOWED": "+1",
           "TARS_PHONE_PIN": "123456"}  # fmt: skip
    assert Settings.read(env).person is None
    for change in ({"TARS_PHONE_PIN": "1234"}, {"TARS_PHONE_URL": "http://a"}, {"TARS_PHONE_ALLOWED": ""}):
        with pytest.raises(SystemExit):
            Settings.read({**env, **change})


def a_call():
    to_twilio, to_brain = [], []
    call = Call("MZ", to_twilio.append)
    call.joined(to_brain.append)
    call.brain(WELCOME)
    return call, to_twilio, to_brain


def test_the_whole_call_is_streamed_once_tars_is_there():
    call, _, sent = a_call()
    assert sent == [{"type": "wake", "how": "button", "score": 0, "preroll_ms": 0}]
    for _ in range(100):
        call.media(base64.b64encode(b"\xff" * 160).decode())
    audio = [m for m in sent if isinstance(m, bytes)]
    assert len(audio) >= 24 and {len(a) for a in audio} == {2560}


def say(call, play_id, seconds=1.0):
    call.brain(json.dumps({"type": "play", "id": play_id}))
    audio = (np.sin(np.arange(int(24000 * seconds)) / 20) * 5000).astype(np.int16).tobytes()
    call.brain(audio[:1001])
    call.brain(audio[1001:])
    call.brain(json.dumps({"type": "mark", "id": play_id}))


def test_a_sentence_is_sent_in_frames_and_has_played_at_its_mark():
    call, twilio, sent = a_call()
    say(call, 7)
    frames = [base64.b64decode(m["media"]["payload"]) for m in twilio if m["event"] == "media"]
    assert 7950 <= sum(map(len, frames)) < 8200 and {len(f) for f in frames[:-1]} == {160}
    assert twilio[-1]["mark"] == {"name": "7"} and len(sent) == 1
    call.mark("7")
    assert sent[1:] == [{"type": "played", "id": 7, "cut": False}]


def test_a_stop_clears_twilio_and_what_it_still_held_was_cut():
    call, twilio, sent = a_call()
    say(call, 1)
    say(call, 2)
    call.mark("1")
    call.brain(json.dumps({"type": "play", "id": 3}))
    call.brain(json.dumps({"type": "stop"}))
    before = len(twilio)
    call.brain(bytes(4800))  # what was on its way
    call.brain(json.dumps({"type": "mark", "id": 3}))
    assert twilio[before - 1]["event"] == "clear" and len(twilio) == before
    call.mark("2")  # Twilio gives back what it dropped
    assert [(m["id"], m["cut"]) for m in sent[1:]] == [(1, False), (2, True)]
    say(call, 4)
    assert twilio[-1]["mark"] == {"name": "4"}


def test_star_stops_tars_and_hangup_ends_the_call():
    call, twilio, sent = a_call()
    call.key("*")
    assert twilio[-1]["event"] == "clear" and sent[-1] == {"type": "button", "name": "stop"}
    call.brain(json.dumps({"type": "hangup"}))
    assert call.hung_up.is_set()


def test_a_call_through_to_the_brain(log):
    tickets = Tickets(log.store)
    listener = BodyListener(
        BodyKeys(log.store).public_key, lambda h: welcome("r", 24000), host="127.0.0.1", redeem=tickets.redeem
    ).start()
    web = client(lambda: tickets.issue("Robin"), f"ws://127.0.0.1:{listener.port}")
    token = re.search(r'value="([^"]+)"', post(web, "/pin", Digits="246810"))[1]
    done = threading.Event()
    frame = base64.b64encode(b"\xff" * 160).decode()
    try:
        with web.websocket_connect("/audio") as ws:
            start = {"streamSid": "MZ", "callSid": "CA1", "customParameters": {"token": token}}
            ws.send_text(json.dumps({"event": "start", "start": start}))
            hello = listener.pages.get(timeout=5)
            assert hello.web and hello.kind == "phone" and hello.person == "Robin"
            link = listener.connected(hello.device, timeout=5)

            def talking():
                while not done.is_set():
                    ws.send_text(json.dumps({"event": "media", "media": {"payload": frame}}))
                    time.sleep(0.02)

            thread = threading.Thread(target=talking, daemon=True)
            thread.start()
            assert link.wakes.get(timeout=5)[0] == "button"
            assert len(link.blocks.get(timeout=5)) == 1280
            link.send({"type": "play", "id": 1})
            link.send(np.full(4800, 900, np.int16).tobytes())
            link.send({"type": "mark", "id": 1})
            while (said := json.loads(ws.receive_text()))["event"] != "mark":
                assert said["event"] == "media"
            done.set()
            thread.join()
            ws.send_text(json.dumps({"event": "mark", "mark": {"name": said["mark"]["name"]}}))
            assert link.played.get(timeout=5) == (1, False)
            ws.send_text(json.dumps({"event": "stop"}))
        assert link.gone.wait(5)  # hanging up closes the room
    finally:
        done.set()
        listener.close()


class FluxServer:
    """Deepgram's side of a stream: sends `events`, then keeps what it's sent."""

    def __init__(self, events, then_close=False):
        from websockets.sync.server import serve

        self.got, self.told = bytearray(), []

        def handle(ws):
            for event in events:
                ws.send(json.dumps(event))
            if then_close:
                return
            for message in ws:
                if isinstance(message, bytes):
                    self.got.extend(message)
                else:
                    self.told.append(json.loads(message))

        self.server = serve(handle, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def flux_call(monkeypatch, server):
    import queue

    from websockets.sync.client import connect

    from voice_assistant import stt
    from voice_assistant.config import STTConfig

    monkeypatch.setattr(stt, "deepgram_connect", lambda url, key, **kw: connect(server.url))
    heard = queue.Queue()
    return stt.FluxCall("key", STTConfig(), lambda *event: heard.put(event), ("TARS",)), heard


def test_flux_hears_the_whole_call_on_one_stream(monkeypatch):
    said = [("StartOfTurn", "hi"), ("EndOfTurn", "hi there"), ("StartOfTurn", "and"), ("EndOfTurn", "and again")]
    server = FluxServer(
        [
            {"type": "Connected"},
            *(
                {
                    "type": "TurnInfo",
                    "event": e,
                    "transcript": f"{t} ",
                    "words": [{"word": t, "confidence": 0.9, "end": 0.02}],
                }
                for e, t in said
            ),
        ]
    )
    call, heard = flux_call(monkeypatch, server)
    call.feed(b"ab" * 1280)
    assert [heard.get(timeout=5)[:2] for _ in said] == said
    assert call.waited is not None and call.waited < 0.1
    call.expect(("eleven",))
    deadline = time.monotonic() + 5
    while len(server.got) < 2560 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(server.got) == 2560
    while not server.told and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.told[0] == {"type": "Configure", "keyterms": ["eleven", "TARS"]}
    call.close()
    time.sleep(0.2)
    assert heard.empty()  # closed by us: no failure
    server.server.shutdown()


def test_a_flux_stream_that_ends_by_itself_is_told_once(monkeypatch):
    server = FluxServer([], then_close=True)
    call, heard = flux_call(monkeypatch, server)
    assert heard.get(timeout=5)[0] == "Failed"
    call.feed(b"ab" * 1280)
    time.sleep(0.2)
    assert heard.empty()
    server.server.shutdown()


def test_a_call_is_handed_to_its_conversation_in_a_room_of_its_own(log, monkeypatch):
    import dataclasses
    import queue
    import types
    from pathlib import Path

    from voice_assistant import wiring
    from voice_assistant.config import BodiesConfig, load_config
    from voice_assistant.rooms import Rooms

    cfg = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    cfg = dataclasses.replace(cfg, bodies=BodiesConfig(enabled=True, host="127.0.0.1", port=0))
    monkeypatch.setattr(wiring, "make_trigger", lambda *a: (None, ""))
    monkeypatch.setattr("voice_assistant.recorder.make_recorder", lambda cfg, root: None)
    port, real = {}, BodyListener.start

    def started(self):
        port["n"] = self.port
        return real(self)

    monkeypatch.setattr(BodyListener, "start", started)
    held, woken = queue.Queue(), []
    wiring.serve_rooms(
        cfg, Path("."), log.store, 24000, Rooms("home"),
        lambda body, room, page: types.SimpleNamespace(room=room, page=page, journal=None),
        lambda assistant, idle: woken.append(idle),
        lambda assistant, link: held.put((assistant, link.blocks.get(timeout=5))),
    )  # fmt: skip
    call, over = Call("MZ", lambda m: None), threading.Event()
    tickets = Tickets(log.store)
    args = (call, f"ws://127.0.0.1:{port['n']}", lambda: tickets.issue("robin"), over)
    threading.Thread(target=phone.link_to_brain, args=args, daemon=True).start()
    try:
        for _ in range(500):
            call.media(base64.b64encode(b"\xff" * 160).decode())
            time.sleep(0.005)
            if not held.empty():
                break
        assistant, block = held.get(timeout=5)
        assert assistant.room == "Robin on the phone" and assistant.page.kind == "phone" and len(block) == 1280
        assert woken == []
    finally:
        over.set()


def test_what_is_said_before_tars_or_the_stream_is_there_is_kept():
    call, sent, twilio = Call(), [], []
    for _ in range(50):  # a second
        call.media(base64.b64encode(b"\xff" * 160).decode())
    call.joined(sent.append)
    assert sent == []  # not before its welcome, which opens the way
    call.brain(WELCOME)
    assert sent[0]["type"] == "wake" and sum(len(m) for m in sent[1:]) >= 12 * 2560
    say(call, 1)
    call.attach("MZ9", twilio.append)
    assert twilio[-1]["event"] == "mark" and {m["streamSid"] for m in twilio} == {"MZ9"}
    n = len(twilio)
    say(call, 2)
    assert twilio[-1]["mark"] == {"name": "2"} and len(twilio) > n


def test_what_waits_is_bounded():
    call = Call()
    for _ in range(60 * 50):
        call.media(base64.b64encode(b"\xff" * 160).decode())
    assert len(call._unheard) == phone.UNHEARD_BLOCKS


def test_tars_joins_at_the_pin_and_leaves_if_the_audio_never_comes(monkeypatch):
    joined, left = threading.Event(), threading.Event()

    def link(call, url, ticket, over):
        joined.set()
        if over.wait(5):
            left.set()

    monkeypatch.setattr(phone, "link_to_brain", link)
    monkeypatch.setattr(phone, "TOKEN_S", 0.3)
    with client() as web:
        post(web, "/pin", Digits="000000")
        assert not joined.is_set()
        post(web, "/pin", Digits="246810")
        assert joined.wait(2) and left.wait(3)
