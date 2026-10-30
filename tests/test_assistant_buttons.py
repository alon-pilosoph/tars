"""The remote's talk and stop buttons and the speakerphone's mute, as the assistant takes them: talk wakes it as its
name does; stop cuts what it's saying and ends the conversation, quietly, with no follow-up; muted, it doesn't wake."""

import types

import pytest

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


def test_muted_it_wakes_to_neither_its_name_nor_the_talk_button(speaker, conversations):
    assistant, _ = make_assistant(speaker, [], [])
    assistant.mute(True)
    assert assistant.talk().startswith("ignored: muted")
    assistant.trigger = waking(assistant, "name", "talk")
    assert conversations(assistant) == []
    assistant.mute(False)
    assistant.trigger = waking(assistant, "talk")
    assert conversations(assistant) == [ALWAYS]


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
