"""The remote's talk and stop buttons and the speakerphone's mute, as the assistant takes them: talk wakes it as its
name does; stop cuts what it's saying and ends the conversation, quietly, with no follow-up; muted, it doesn't wake."""

import threading
import time
import types

import pytest

import voice_assistant.assistant as assistant_module
from voice_assistant.assistant import ALWAYS, PAUSE
from voice_assistant.wake import DUE

from .conftest import make_assistant


class Done(Exception):
    """Ends run_forever, which never returns on its own."""


def waking(assistant, *steps):
    """A trigger whose waits do `steps` in turn: "talk" presses the talk button and waits on `due` as the wake word's
    wait does; "name" is TARS's name heard. Then Done."""
    left = list(steps)

    def wait(mic, due=None):
        if not left:
            raise Done
        step = left.pop(0)
        if step == "name":
            return None
        assistant.talk()
        return DUE if due and due() else None

    return types.SimpleNamespace(wait=wait, last_audio=None)


@pytest.fixture
def conversations(monkeypatch):
    def run(assistant):
        started = []
        monkeypatch.setattr(assistant, "converse", lambda follow_up_s, **kw: started.append(kw.get("greet")))
        monkeypatch.setattr(assistant, "prepare_phrases", lambda: None)
        with pytest.raises(Done):
            assistant.run_forever("Say 'hey tars'...")
        return started

    return run


def test_the_talk_button_wakes_it_and_it_greets_at_once(speaker, conversations):
    assistant, _ = make_assistant(speaker, [], [])
    assistant.trigger = waking(assistant, "talk", "name")
    assert conversations(assistant) == [ALWAYS, PAUSE]  # the button, then its name as usual


def test_muted_it_doesn_t_even_listen_for_its_name_and_unmuting_listens_again(speaker, conversations):
    assistant, _ = make_assistant(speaker, [], [])
    assistant.mic.clear = lambda: None
    assistant.mute(True)
    assert assistant.talk().startswith("ignored: muted")
    waits = []
    trigger = waking(assistant, "name")
    assistant.trigger = types.SimpleNamespace(wait=lambda mic, due=None: waits.append(1) or trigger.wait(mic, due))
    threading.Timer(0.3, assistant.mute, [False]).start()  # the speakerphone unmuted, a moment later
    started = time.monotonic()
    assert conversations(assistant) == [PAUSE]  # then its name woke it, as usual
    assert waits == [1, 1]  # the wake model was asked only once unmuted: its name, then Done
    assert 0.3 <= time.monotonic() - started < 1.0  # back at once, not at the next look


def test_muted_what_s_due_is_still_said(speaker, monkeypatch):
    assistant, _ = make_assistant(speaker, [], [])
    assistant.mic.clear = lambda: None
    assistant.mute(True)
    due = iter([False, False, True])
    monkeypatch.setattr(assistant, "_due", lambda: next(due, False))
    monkeypatch.setattr(assistant_module, "MUTED_POLL_S", 0.01)
    assert assistant._wait_muted() == DUE


def test_muting_while_it_listens_for_its_name_stops_the_listening(speaker):
    assistant, _ = make_assistant(speaker, [], [])
    assistant.mute(True)
    assert assistant._woken()  # the wake wait asks this every block: it ends, and the muted wait takes over


def test_stop_while_it_answers_ends_the_conversation_with_no_follow_up_and_no_error(speaker):
    assistant, brain = make_assistant(speaker, [b"a", b"b", None], ["tell me a long story", "and another"])
    speaker.play_pcm_stream = lambda chunks, *a, **kw: (list(chunks), assistant.stop())
    assistant.converse(follow_up_s=4.0)
    assert brain.asked == ["tell me a long story"]
    assert assistant.recorder.timeouts == [None]  # no follow-up was listened for
    assert speaker.stops == 1


def test_stop_pressed_while_idle_stops_nothing_later(speaker, conversations):
    assistant, _ = make_assistant(speaker, [], [])
    assistant.stop()
    assistant.trigger = waking(assistant, "name")
    conversations(assistant)
    assert not assistant._stop.is_set()  # cleared when it woke: the conversation that follows isn't cut short
