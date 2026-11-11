"""[home] local_first (commands.LocalFirstSession): nothing goes to the cloud until Vosk has heard the whole request
and found it isn't a house command."""

import dataclasses
from pathlib import Path

from voice_assistant.commands import LocalFirstTranscriber
from voice_assistant.config import _problems, load_config

from .conftest import make_assistant
from .test_commands import COMMANDS, house


class Cloud:
    """The cloud's speech to text for a whole recording: what it was sent."""

    def __init__(self, text="what's the weather like"):
        self.sent, self.text = [], text

    def session(self):
        cloud = self

        class Session:
            def feed(self, pcm):
                cloud.sent.append(pcm)

            def finish(self):
                pass

            def transcript(self):
                return cloud.text

            def close(self):
                pass

        return Session()


def local(heard: str, cloud: Cloud):
    return LocalFirstTranscriber(lambda pcm: heard, COMMANDS.match, cloud).session()


def test_a_house_command_is_heard_on_the_pi_and_nothing_goes_to_the_cloud():
    cloud = Cloud()
    session = local("lock the front door", cloud)
    session.feed(b"\1\0" * 800)
    session.finish()
    assert session.transcript() == "lock the front door" and cloud.sent == [] and session.local is True
    assert session.turn_state is None  # TARS's own rules say when the turn is over


def test_anything_else_goes_to_the_cloud_whole_once_its_over():
    cloud = Cloud()
    session = local("", cloud)
    for _ in range(3):
        session.feed(b"\1\0" * 800)
    session.finish()
    assert session.transcript() == "what's the weather like" and session.local is False
    assert cloud.sent == [b"\1\0" * 2400]


def test_only_a_whole_command_stays_on_the_pi():
    cloud = Cloud("please")
    assert local("turn the boiler on", cloud).transcript() == "turn the boiler on" and cloud.sent == []
    assert local("please", cloud).transcript() == "please" and len(cloud.sent) == 1  # Vosk's [unk] around a word


def test_local_first_starts_no_answer_in_a_pause(speaker):
    assistant, brain = make_assistant(speaker, [["pause", b"q"], None], ["what time is it"])
    assistant.commands = dataclasses.replace(house([]), local_first=True)
    paused = []
    record = assistant.recorder.record
    assistant.recorder.record = lambda *a, **kw: paused.append(kw.get("on_pause")) or record(*a, **kw)
    assistant.converse(follow_up_s=4.0)
    assert paused[0] is None and brain.asked == ["what time is it"]


def test_local_first_needs_house_commands():
    shipped = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    cfg = dataclasses.replace(shipped, home=dataclasses.replace(shipped.home, local_first=True))
    assert any("needs commands_on_pi" in p for p in _problems(cfg))
    on = dataclasses.replace(cfg, home=dataclasses.replace(cfg.home, commands_on_pi=True))
    assert not any("local_first" in p for p in _problems(on))
