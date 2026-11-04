"""What a chat asks of the assistant over control.py: TARS's microphone muted and unmuted, and /say."""

import shutil
import tempfile
from pathlib import Path

import pytest

from voice_assistant import control
from voice_assistant.assistant import Announcements
from voice_assistant.config import HomeConfig
from voice_assistant.home import HomeTools, Request
from voice_assistant.mic_mute import DEVICE, MicMute

from .conftest import make_assistant


@pytest.fixture
def short_folder():
    folder = Path(tempfile.mkdtemp(dir="/tmp"))  # a Unix socket's path must be short (about 100 characters)
    yield folder
    shutil.rmtree(folder)


def chat(mic: MicMute) -> HomeTools:
    return HomeTools(HomeConfig(), {DEVICE: mic})


def ask(action: str) -> dict:
    return {"device": DEVICE, "action": action}


def test_a_chat_mutes_and_unmutes_the_assistant_as_its_button_does(speaker, short_folder):
    assistant, _ = make_assistant(speaker, [], [])
    pressed = []

    def button(muted: bool) -> None:  # start_buttons' set_muted: the speakerphone's mute, then the assistant's
        pressed.append(muted)
        assistant.mute(muted)

    server = control.Server(short_folder, assistant.control_ops(button))
    try:
        tools = chat(MicMute(short_folder))
        assert tools.call(ask("status"), Request()) == f"{DEVICE}: on: listening at home"
        assert tools.call(ask("off"), Request()) == f"done: {DEVICE} is muted: not listening at home"
        assert assistant.muted and assistant.talk().startswith("ignored: muted")
        assert tools.call(ask("on"), Request()) == f"done: {DEVICE} is on: listening at home"
        assert not assistant.muted and pressed == [True, False]
        assert tools.call(ask("status"), Request()) == f"{DEVICE}: on: listening at home"
        assert pressed == [True, False]  # asking how it is changes nothing
    finally:
        server.close()


def test_an_assistant_with_no_mute_to_work_says_so(speaker, short_folder):
    assistant, _ = make_assistant(speaker, [], [])
    server = control.Server(short_folder, assistant.control_ops())  # not Linux: no buttons, no mute
    try:
        said = chat(MicMute(short_folder)).call(ask("on"), Request())
    finally:
        server.close()
    assert said.startswith("error: the voice assistant can't be muted from here")


def test_with_the_assistant_not_running_nothing_is_said_to_have_changed(short_folder):
    mic = MicMute(short_folder)
    said = chat(mic).call(ask("on"), Request())
    assert said.startswith("error: the voice assistant at home isn't running") and "nothing is known" in said
    assert mic.check()[0].status == "warn"


def test_a_mute_the_assistant_didnt_take_is_not_confirmed():
    mic = MicMute(Path("/nowhere"), ask=lambda folder, op, **args: {"ok": True, "muted": False})
    assert chat(mic).call(ask("off"), Request()).startswith("not confirmed:")


def test_a_chats_say_is_announced_word_for_word(speaker, short_folder):
    assistant, _ = make_assistant(speaker, [], [])
    assistant.announcements = Announcements()
    server = control.Server(short_folder, assistant.control_ops())
    try:
        control.ask(short_folder, "say", text="Ari, time for bed")
        with pytest.raises(control.Refused):
            control.ask(short_folder, "say", text="  ")
    finally:
        server.close()
    assert assistant.announcements.take() == ["Ari, time for bed"]


def test_at_a_glance_muted_or_listening():
    for muted, said in ((True, "muted"), (False, "listening")):
        assert MicMute(Path("/nowhere"), ask=lambda folder, op, m=muted, **args: {"muted": m}).glance() == said
