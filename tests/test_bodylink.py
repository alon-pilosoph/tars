"""The body link (bodylink.py), driven by a fake body that speaks the real protocol over a real local socket."""

import json
import threading
import time
import types
from concurrent.futures import Future
from pathlib import Path

import numpy as np
import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from voice_assistant import bodylink
from voice_assistant.audio import BLOCK_SAMPLES
from voice_assistant.bodylink import BodyGone, BodyListener, RemoteBody, proof
from voice_assistant.verify import ANSWER, ASK, IGNORE
from voice_assistant.wake import DUE

KEY = b"k" * 32
TRANSCRIPTS = Path(__file__).parent / "fixtures" / "bodylink"


@pytest.fixture
def listener():
    waits = []
    made = BodyListener(
        lambda device: KEY if device == "kitchen" else None,
        lambda hello: bodylink.welcome("kitchen", 24000),
        host="127.0.0.1",
        sleep=waits.append,
    ).start()
    made.waits = waits
    yield made
    made.close()


class Body:
    """A body, as the Atom or a laptop would be one: it answers the challenge and speaks the protocol."""

    def __init__(self, port: int, device="kitchen", key=KEY, wake="verified", echo_cancel=False, **hello):
        self.ws = connect(f"ws://127.0.0.1:{port}", legacy=True, max_size=None)
        self.challenge = json.loads(self.ws.recv(timeout=5))
        self.ws.send(json.dumps({"type": "hello", "protocol": 1, "device": device,
                                 "proof": proof(key, self.challenge["nonce"]), "kind": "test", "wake": wake,
                                 "echo_cancel": echo_cancel, **hello}))  # fmt: skip
        self.welcome = json.loads(self.ws.recv(timeout=5))
        self.heard: list = []  # every frame from the brain, in order: dicts, and bytes for audio

    def send(self, **message) -> None:
        self.ws.send(json.dumps(message))

    def stream(self, pcm: np.ndarray, frame: int = 960) -> None:
        """Audio up, in frames of any length (here, not 80 ms ones)."""
        data = pcm.astype(np.int16).tobytes()
        for start in range(0, len(data), frame * 2):
            self.ws.send(data[start : start + frame * 2])

    def next(self, timeout=5.0):
        got = self.ws.recv(timeout=timeout)
        got = got if isinstance(got, bytes) else json.loads(got)
        self.heard.append(got)
        return got

    def until(self, kind: str, timeout=5.0) -> dict:
        while True:
            got = self.next(timeout)
            if isinstance(got, dict) and got.get("type") == kind:
                return got

    def close(self) -> None:
        self.ws.close()


def speech(seconds: float, level=3000) -> np.ndarray:
    return (np.sin(np.arange(int(16000 * seconds)) / 5) * level).astype(np.int16)


# Letting a body in


def test_a_body_with_its_key_is_let_in_and_handed_over(listener):
    body = Body(listener.port, buttons=["stop"])
    assert body.welcome == {"type": "welcome", "room": "kitchen", "play_rate": 24000, "max_listen_s": 120.0}
    link = listener.connected("kitchen", timeout=2)
    assert link.hello.device == "kitchen" and link.hello.wake == "verified" and link.hello.buttons == ["stop"]
    body.close()


@pytest.mark.parametrize(
    "hello",
    [
        {"device": "kitchen", "key": b"x" * 32},  # a wrong key
        {"device": "garage"},  # one that isn't paired
        {"device": "kitchen", "protocol": 2},  # a protocol this brain doesn't speak
    ],
)
def test_a_body_that_cant_prove_itself_is_closed_without_a_reason(listener, hello):
    ws = connect(f"ws://127.0.0.1:{listener.port}", legacy=True)
    nonce = json.loads(ws.recv(timeout=5))["nonce"]
    key = hello.get("key", KEY)
    ws.send(json.dumps({"type": "hello", "protocol": hello.get("protocol", 1), "device": hello["device"],
                        "proof": proof(key, nonce)}))  # fmt: skip
    with pytest.raises(ConnectionClosed) as closed:
        ws.recv(timeout=5)
    assert not closed.value.rcvd or not closed.value.rcvd.reason  # no detail
    assert listener.connected("kitchen", timeout=0.1) is None


def test_audio_before_hello_or_no_hello_at_all_is_shut_out(listener, monkeypatch):
    ws = connect(f"ws://127.0.0.1:{listener.port}", legacy=True)
    ws.recv(timeout=5)
    ws.send(b"\0\0" * 640)
    with pytest.raises(ConnectionClosed):
        ws.recv(timeout=5)
    monkeypatch.setattr(bodylink, "HELLO_WAIT_S", 0.2)
    silent = connect(f"ws://127.0.0.1:{listener.port}", legacy=True)
    silent.recv(timeout=5)
    with pytest.raises(ConnectionClosed):
        silent.recv(timeout=5)


def test_an_address_that_keeps_failing_waits_longer_each_time(listener):
    for _ in range(3):
        ws = connect(f"ws://127.0.0.1:{listener.port}", legacy=True)
        ws.recv(timeout=5)
        ws.send(json.dumps({"type": "hello", "protocol": 1, "device": "kitchen", "proof": "nope"}))
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=5)
    assert listener.waits == [1.0, 2.0]
    Body(listener.port).close()  # one that gets in clears the slate
    ws = connect(f"ws://127.0.0.1:{listener.port}", legacy=True)
    ws.recv(timeout=5)
    assert listener.waits == [1.0, 2.0, 3.0]


def test_an_address_that_isnt_allowed_is_shut_out_before_anything():
    closed = BodyListener(lambda d: KEY, lambda h: {}, allowed=lambda address: False, host="127.0.0.1").start()
    try:
        ws = connect(f"ws://127.0.0.1:{closed.port}", legacy=True)
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=5)
    finally:
        closed.close()


def test_a_newer_connection_from_the_same_body_takes_over(listener):
    first = Body(listener.port)
    old = listener.connected("kitchen", timeout=2)
    second = Body(listener.port)
    _until(lambda: old.gone.is_set())
    new = listener.connected("kitchen", timeout=2)
    assert new is not old and not new.gone.is_set()
    with pytest.raises(ConnectionClosed):
        first.ws.recv(timeout=5)
    second.close()


# The microphone


def body_and(listener, **kw) -> tuple[Body, RemoteBody]:
    body = Body(listener.port, **kw)
    link = listener.connected(body.welcome["room"], timeout=2)
    return body, RemoteBody(link, 24000, recorder=None)


def woken(body: Body, remote: RemoteBody, audio=None, how="button", preroll_ms=0, score=0.0):
    """The brain waits for a wake; the body, once put to sleep, wakes and streams `audio`. What the wait returned."""
    result = {}
    thread = threading.Thread(target=lambda: result.update(how=remote.trigger.wait(remote.mic)))
    thread.start()
    body.until("sleep")
    body.send(type="wake", how=how, score=score, preroll_ms=preroll_ms)
    if audio is not None:
        body.stream(audio)
    thread.join(5)
    return result["how"]


def test_audio_comes_in_80_ms_blocks_whatever_frames_it_came_in_and_only_once_awake(listener):
    body, remote = body_and(listener)
    body.stream(speech(1.0))  # asleep: dropped as it comes
    time.sleep(0.2)
    assert woken(body, remote) is None
    body.stream(speech(0.5), frame=333)
    blocks = [remote.mic.read() for _ in range(6)]  # 0.5 s is 6 whole blocks and a part
    assert all(len(b) == BLOCK_SAMPLES for b in blocks)
    assert np.array_equal(np.concatenate(blocks), speech(0.5)[: 6 * BLOCK_SAMPLES])
    body.close()


def test_a_body_that_goes_mid_recording_or_stops_sending_is_body_gone(listener, monkeypatch):
    monkeypatch.setattr(bodylink, "MIC_STALL_S", 0.3)
    body, remote = body_and(listener)
    woken(body, remote)
    with pytest.raises(BodyGone, match="sent nothing"):
        remote.mic.read()
    body.close()
    with pytest.raises(BodyGone, match="gone"):
        remote.mic.read()


def test_a_body_that_stops_at_its_own_cap_says_so(listener):
    body, remote = body_and(listener)
    woken(body, remote)
    body.send(type="gone_quiet")
    with pytest.raises(BodyGone, match="its own limit"):
        remote.mic.read()
    body.close()


def test_a_flood_of_audio_keeps_only_the_newest_30_seconds(listener):
    body, remote = body_and(listener)
    woken(body, remote)
    body.stream(speech(40.0), frame=16000)
    _until(lambda: remote.link.blocks.full())
    assert remote.link.blocks.qsize() == int(bodylink.MAX_QUEUED_S / 0.08)
    body.close()


def test_a_frame_too_big_or_broken_closes_the_body(listener):
    body, remote = body_and(listener)
    body.ws.send(b"\0" * (bodylink.MAX_FRAME + 2))
    with pytest.raises(ConnectionClosed):
        body.ws.recv(timeout=5)
    _until(lambda: remote.link.gone.is_set())
    for broken in ("not json", "[1, 2]", json.dumps({"type": "wake", "how": "telepathy"})):
        body, remote = body_and(listener)
        body.ws.send(broken)
        _until(lambda remote=remote: remote.link.gone.is_set())


def test_what_the_body_heard_while_tars_spoke_is_dropped_and_it_is_told_to_listen_again(listener):
    body, remote = body_and(listener)
    with remote.mic.paused(tail_s=0):
        pass
    assert body.until("listen") == {"type": "listen", "on": False}
    assert body.until("listen") == {"type": "listen", "on": True}
    body.close()


# The speaker


def reply(seconds_of_silence=0.2, seconds=0.5) -> list[bytes]:
    silence = np.zeros(int(24000 * seconds_of_silence), np.int16).tobytes()
    sound = (np.sin(np.arange(int(24000 * seconds)) / 3) * 8000).astype(np.int16).tobytes()
    return [silence, sound[:7], sound[7:]]  # an odd split: whole samples only go out


def play_in_thread(remote: RemoteBody, chunks, **kw):
    result, first = {}, []

    def run():
        try:
            result["cut"] = remote.play(chunks, 24000, on_first_audio=lambda: first.append(time.monotonic()), **kw)
        except Exception as e:  # noqa: BLE001 - the test looks at it
            result["error"] = e

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result, first


def test_a_reply_is_sent_from_its_first_sound_and_returns_once_the_body_played_it(listener):
    body, remote = body_and(listener)
    thread, result, first = play_in_thread(remote, reply())
    assert body.until("listen")["on"] is False  # it doesn't hear itself
    play = body.until("play")
    audio = b""
    while isinstance(got := body.next(), bytes):
        audio += got
    assert got == {"type": "play_end", "id": play["id"]} and play["listening"] is False
    assert int(24000 * 0.5) * 2 - len(audio) < 100 and audio[:2] != b"\0\0"  # the leading silence never went
    assert len(first) == 1 and thread.is_alive()  # it waits for the body
    body.send(type="played", id=play["id"], cut=False)
    thread.join(5)
    assert result == {"cut": False}
    assert body.until("listen")["on"] is True
    body.close()


def test_a_reply_cut_at_the_body_or_stopped_here_returns_true(listener):
    body, remote = body_and(listener)
    thread, result, _ = play_in_thread(remote, reply())
    play = body.until("play")
    body.until("play_end")
    body.send(type="played", id=play["id"], cut=True)
    thread.join(5)
    assert result == {"cut": True}

    gate = threading.Event()

    def slow():
        yield reply()[1]
        gate.wait(5)
        yield reply()[2]

    thread, result, _ = play_in_thread(remote, slow())
    play = body.until("play")
    remote.stop()
    gate.set()
    assert body.until("stop") == {"type": "stop"}
    body.until("play_end")
    body.send(type="played", id=play["id"], cut=False)
    thread.join(5)
    assert result == {"cut": True}
    body.close()


def test_a_body_that_never_says_it_played_or_goes_mid_reply_is_body_gone(listener, monkeypatch):
    monkeypatch.setattr(bodylink, "SPEAKER_STALL_S", 0.3)
    body, remote = body_and(listener)
    thread, result, _ = play_in_thread(remote, reply())
    body.until("play_end")
    thread.join(5)
    assert isinstance(result["error"], BodyGone) and "never said" in str(result["error"])
    body.close()

    body, remote = body_and(listener)

    def endless():
        while True:
            yield reply()[1]
            time.sleep(0.01)

    thread, result, _ = play_in_thread(remote, endless())
    body.until("play")
    body.close()
    thread.join(5)
    assert isinstance(result["error"], BodyGone)


def test_a_body_that_cancels_its_own_echo_keeps_listening_while_it_plays(listener):
    body, remote = body_and(listener, echo_cancel=True)
    assert remote.hears_while_speaking
    thread, _, _ = play_in_thread(remote, reply(), listening=True)
    play = body.until("play")
    assert play["listening"] is True and not any(isinstance(m, dict) and m["type"] == "listen" for m in body.heard)
    body.until("play_end")
    body.send(type="played", id=play["id"], cut=False)
    thread.join(5)
    body.close()


# The wake


class FakeChecker:
    def __init__(self, outcomes):
        self.outcomes, self.checked = list(outcomes), []
        self.last_audio = self.after_name = None

    def confirm(self, audio, score):
        self.checked.append((len(audio), score))
        outcome = self.outcomes.pop(0)
        if outcome != IGNORE:
            self.last_audio, self.after_name = audio, self.find_after_name(audio)
        return outcome

    def find_after_name(self, audio):
        found = Future()
        found.set_result(audio[-1600:])
        return found


def test_a_verified_wake_is_trusted_and_its_pre_roll_kept(listener):
    body = Body(listener.port, wake="verified")
    remote = RemoteBody(listener.connected("kitchen", timeout=2), 24000, None, FakeChecker([]))
    assert woken(body, remote, speech(1.6), how="word", preroll_ms=1600, score=0.9) == ANSWER
    assert len(remote.trigger.last_audio) == 16000 * 1.6 and remote.trigger.checker.checked == []
    assert remote.trigger.after_name.result() is not None
    assert body.heard[0] == {"type": "sleep"}  # waiting starts by putting it to sleep
    body.close()


def test_a_model_wake_is_double_checked_here_and_one_that_isnt_tars_goes_back_to_sleep(listener):
    body = Body(listener.port, wake="model")
    checker = FakeChecker([IGNORE, ASK])
    remote = RemoteBody(listener.connected("kitchen", timeout=2), 24000, None, checker)
    result = {}
    thread = threading.Thread(target=lambda: result.update(wake=remote.wait(lambda: False)))
    thread.start()
    body.until("sleep")
    body.send(type="wake", how="word", score=0.7, preroll_ms=800)
    body.stream(speech(0.8))
    body.until("sleep")  # not TARS: back to sleep, still waiting
    body.send(type="wake", how="word", score=0.8, preroll_ms=800)
    body.stream(speech(0.8))
    thread.join(5)
    assert result["wake"].how == ASK and checker.checked == [(12800, 0.7), (12800, 0.8)]
    body.close()


def test_a_button_wakes_it_with_nothing_to_check_and_a_due_reminder_ends_the_wait(listener):
    body, remote = body_and(listener, wake="none")
    assert woken(body, remote) is None
    assert remote.wait(lambda: True).how == DUE
    body.close()
    with pytest.raises(BodyGone):
        remote.wait(lambda: False)


# A whole conversation, through the real Assistant


class BlocksRecorder:
    """Records until the body's audio has been quiet for three blocks: enough of UtteranceRecorder's job to show the
    mapping, without its models."""

    trailing_silence_s = 0.24

    def record(self, mic, **kw):
        blocks, quiet = [], 0
        while quiet < 3:
            block = mic.read()
            blocks.append(block)
            quiet = quiet + 1 if np.abs(block).max() < 100 else 0
        return np.concatenate(blocks).tobytes()


def test_a_conversation_through_a_network_body_is_heard_answered_and_played(listener):
    from voice_assistant.assistant import Assistant

    from .conftest import RecordingBrain, ScriptedTranscriber

    body = Body(listener.port, wake="none")
    remote = RemoteBody(listener.connected("kitchen", timeout=2), 24000, BlocksRecorder())
    voice = types.SimpleNamespace(sample_rate=24000, stream=lambda text: iter(reply(0.05, 0.3)), warm=lambda: None)
    brain = RecordingBrain(["It's sunny."])
    assistant = Assistant(remote, ScriptedTranscriber(["what's the weather"]), brain, voice)

    def the_body():
        body.until("sleep")
        body.send(type="wake", how="button", score=0, preroll_ms=0)
        body.stream(np.concatenate([speech(0.8), np.zeros(4000, np.int16)]))
        play = body.until("play")
        body.until("play_end")
        body.send(type="played", id=play["id"], cut=False)

    talking = threading.Thread(target=the_body)
    talking.start()
    wake = remote.wait(lambda: False)
    assistant.converse(0.0, wake=wake)
    talking.join(5)
    body.until("listen")  # after the reply, the body listens again for a follow-up
    assert brain.asked == ["what's the weather"]
    shape = ["challenge", "welcome"] + [m["type"] if isinstance(m, dict) else "audio" for m in body.heard]
    shape = [kind for i, kind in enumerate(shape) if kind != "audio" or shape[i - 1] != "audio"]
    expected = json.loads((TRANSCRIPTS / "button-turn.json").read_text())
    assert shape == expected["brain_sends"]
    body.close()


def test_a_body_gone_in_any_phase_leaves_the_listener_ready_for_the_next(listener):
    for phase in ("handshake", "recording", "playback"):
        if phase == "handshake":
            ws = connect(f"ws://127.0.0.1:{listener.port}", legacy=True)
            ws.recv(timeout=5)
            ws.close()
        else:
            body, remote = body_and(listener)
            woken(body, remote)
            if phase == "playback":
                thread, result, _ = play_in_thread(remote, reply())
                body.until("play")
            body.close()
            with pytest.raises(BodyGone):
                remote.mic.read()
            if phase == "playback":
                thread.join(5)
                assert isinstance(result["error"], BodyGone)
    fresh = Body(listener.port)
    assert listener.connected("kitchen", timeout=2) is not None
    fresh.close()


def _until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)
