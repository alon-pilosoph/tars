"""The remote's talk and stop buttons and the speakerphone's mute, as the assistant takes them: talk wakes it as its
name does; stop cuts what it's saying and ends the conversation, quietly, with no follow-up; muted, it doesn't wake."""

import threading
import time

import pytest

import voice_assistant.assistant as assistant_module
from voice_assistant.assistant import ALWAYS, PAUSE
from voice_assistant.audio import MicrophoneError
from voice_assistant.body import Wake
from voice_assistant.wake import DUE

from .conftest import Asleep, make_assistant


def waking(assistant, *steps):
    """Its body's waits do `steps` in turn: "talk" presses the talk button and waits on `due` as the wake word's wait
    does; "name" is TARS's name heard. Then Asleep."""

    def talk(due):
        assistant.talk()
        return Wake(DUE if due() else None)

    assistant.body.wakes = [Wake() if step == "name" else talk for step in steps]


@pytest.fixture
def conversations(monkeypatch):
    def run(assistant):
        started = []
        monkeypatch.setattr(assistant, "converse", lambda follow_up_s, **kw: started.append(kw.get("greet")))
        monkeypatch.setattr(assistant, "prepare_phrases", lambda: None)
        with pytest.raises(Asleep):
            assistant.run_forever("Say 'hey tars'...")
        return started

    return run


def test_the_talk_button_wakes_it_and_it_greets_at_once(conversations):
    assistant, _ = make_assistant([], [])
    waking(assistant, "talk", "name")
    assert conversations(assistant) == [ALWAYS, PAUSE]  # the button, then its name as usual


def test_the_talk_button_greets_nobody_by_name_since_a_press_carries_no_voice(monkeypatch):
    """The wake model's last audio is the last time TARS's name was said, maybe by someone else: a press isn't anyone's
    voice. Who's asking is still told from what's said next (speaker ID on the request)."""
    import numpy as np

    assistant, _ = make_assistant([], [])
    last_name = np.ones(16_000, np.int16)  # "hey TARS", said by Stacey a while ago

    def talk(due):
        assistant.talk()
        due()
        return Wake(None, audio=last_name)  # what the body's wait hands back: the trigger's last audio

    assistant.body.wakes = [talk]
    woken = []
    monkeypatch.setattr(assistant, "converse", lambda follow_up_s, **kw: woken.append(kw))
    monkeypatch.setattr(assistant, "prepare_phrases", lambda: None)
    with pytest.raises(Asleep):
        assistant.run_forever("Say 'hey tars'...")
    (kw,) = woken
    assert kw["greet"] == ALWAYS and kw.get("wake", Wake()).audio is None


def test_muted_it_doesn_t_even_listen_for_its_name_and_unmuting_listens_again(conversations):
    assistant, _ = make_assistant([], [])
    assistant.mute(True)
    assert assistant.talk().startswith("ignored: muted")
    waking(assistant, "name")
    threading.Timer(0.3, assistant.mute, [False]).start()  # the speakerphone unmuted, a moment later
    started = time.monotonic()
    assert conversations(assistant) == [PAUSE]  # then its name woke it, as usual
    assert assistant.body.waits == 2  # the wake model was asked only once unmuted: its name, then Asleep
    assert 0.3 <= time.monotonic() - started < 1.0  # back at once, not at the next look


def test_muted_what_s_due_is_still_said(monkeypatch):
    assistant, _ = make_assistant([], [])
    assistant.mute(True)
    due = iter([False, False, True])
    monkeypatch.setattr(assistant, "_due", lambda: next(due, False))
    monkeypatch.setattr(assistant_module, "MUTED_POLL_S", 0.01)
    assert assistant._wait_muted() == DUE


def test_muted_a_microphone_that_s_gone_still_ends_tars(monkeypatch):
    """So systemd restarts it on the device that came back, not left deaf and mute until someone looks."""
    assistant, _ = make_assistant([], [])
    assistant.mute(True)
    monkeypatch.setattr(assistant, "_due", lambda: False)

    def gone():
        raise MicrophoneError("gone")

    assistant.body.discard = gone
    with pytest.raises(MicrophoneError):
        assistant._wait_muted()


def test_muting_while_it_listens_for_its_name_stops_the_listening():
    assistant, _ = make_assistant([], [])
    assistant.mute(True)
    assert assistant._woken()  # the wake wait asks this every block: it ends, and the muted wait takes over


def test_stop_while_it_answers_ends_the_conversation_with_no_follow_up_and_no_error():
    assistant, brain = make_assistant([b"a", b"b", None], ["tell me a long story", "and another"])
    assistant.body.on_play = lambda chunks: assistant.stop()
    assistant.converse(follow_up_s=4.0)
    assert brain.asked == ["tell me a long story"]
    assert assistant.body.timeouts == [None]  # no follow-up was listened for
    assert assistant.body.stops == 1


def test_stop_pressed_while_idle_stops_nothing_later(conversations):
    assistant, _ = make_assistant([], [])
    assistant.stop()
    waking(assistant, "name")
    conversations(assistant)
    assert not assistant._stop.is_set()  # cleared when it woke: the conversation that follows isn't cut short
