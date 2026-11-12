"""[home] local_first (commands.LocalFirstTranscriber): nothing goes to the cloud until Vosk has heard the whole request
and found it isn't a house command."""

import dataclasses
import types
from pathlib import Path

import pytest

from voice_assistant.commands import LONGEST_COMMAND_S, LocalFirstTranscriber
from voice_assistant.config import ConfigError, _problems, load_config
from voice_assistant.errors import ServiceError

from .conftest import make_assistant
from .test_commands import COMMANDS, LOCK, house


class Cloud:
    """The cloud's speech to text for a whole recording: what it was sent, and the words it was told to expect."""

    def __init__(self, text="what's the weather like"):
        self.sent, self.expected, self.text = [], [], text

    def session(self, expect=()):
        cloud = self
        cloud.expected.append(expect)

        class Session:
            def feed(self, pcm):
                cloud.sent.append(pcm)

            def finish(self):
                pass

            def transcript(self):
                if isinstance(cloud.text, Exception):
                    raise cloud.text
                return cloud.text

            def close(self):
                pass

        return Session()


def local(heard, cloud: Cloud, expect=()):
    hear = heard if callable(heard) else lambda pcm: heard
    return LocalFirstTranscriber(hear, COMMANDS.match, cloud).session(expect)


def said(session, pcm=b"\1\0" * 800) -> str:
    session.feed(pcm)
    session.finish()
    return session.transcript()


def test_a_house_command_is_heard_on_the_pi_and_nothing_goes_to_the_cloud():
    cloud = Cloud()
    session = local("lock the front door", cloud)
    assert said(session) == "lock the front door" and cloud.sent == []
    assert session.turn_state is None  # TARS's own rules say when the turn is over


def test_anything_else_goes_to_the_cloud_whole_once_its_over():
    cloud = Cloud()
    session = local("", cloud)
    for _ in range(3):
        session.feed(b"\1\0" * 800)
    session.finish()
    assert session.transcript() == "what's the weather like" and cloud.sent == [b"\1\0" * 2400]


def test_only_a_whole_command_stays_on_the_pi():
    command, other = Cloud("please"), Cloud("please")
    assert said(local("turn the boiler on", command)) == "turn the boiler on" and command.sent == []
    assert said(local("please", other)) == "please" and len(other.sent) == 1  # Vosk's [unk] around a word


def test_when_vosk_breaks_the_cloud_hears_the_request(capsys):
    def broken(pcm):
        raise RuntimeError("model gone")

    cloud = Cloud()
    assert said(local(broken, cloud)) == "what's the weather like" and len(cloud.sent) == 1
    assert "couldn't listen for a house command on the Pi" in capsys.readouterr().out


def test_a_recording_too_long_for_a_command_goes_straight_to_the_cloud():
    heard, cloud = [], Cloud()
    pcm = b"\0\0" * int(16_000 * (LONGEST_COMMAND_S + 1))
    assert said(local(lambda p: heard.append(p) or "lock the front door", cloud), pcm) == "what's the weather like"
    assert heard == [] and cloud.sent == [pcm]


def test_the_words_tars_expects_reach_the_cloud():
    cloud = Cloud("eleven")
    assert said(local("", cloud, ("eleven", "twelve"))) == "eleven" and cloud.expected == [("eleven", "twelve")]


def local_assistant(speaker, hear, cloud, calls):
    assistant, brain = make_assistant(speaker, [b"\1\0" * 800, None], [])
    assistant.commands = dataclasses.replace(house(calls, "done: front door reports locked", hear), local_first=True)
    assistant.transcriber = LocalFirstTranscriber(hear, COMMANDS.match, cloud)
    return assistant, brain


def test_a_command_heard_on_the_pi_is_done_with_no_cloud_and_no_model(speaker):
    calls, cloud = [], Cloud()
    assistant, brain = local_assistant(speaker, lambda pcm: "lock front door", cloud, calls)
    assistant.converse(follow_up_s=4.0)
    assert calls[0][0] == {"device": "front door", "action": LOCK} and cloud.sent == [] and brain.asked == []


def test_when_the_cloud_fails_local_first_vosk_isnt_asked_twice(speaker, capsys):
    calls, heard = [], []
    cloud = Cloud(ServiceError("Deepgram speech to text: timed out"))
    assistant, _ = local_assistant(speaker, lambda pcm: heard.append(pcm) or "", cloud, calls)
    assistant.converse(follow_up_s=4.0)
    assert len(heard) == 1 and calls == [] and "ServiceError" in capsys.readouterr().out


def test_local_first_needs_vosk(tmp_path):
    from voice_assistant.__main__ import local_first

    shipped = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    with pytest.raises(ConfigError, match="needs Vosk"):
        local_first(shipped, tmp_path, house([]))


def test_a_spoken_request_waits_for_the_cloud_as_a_spoken_turn_does(tmp_path):
    from voice_assistant.__main__ import OPENAI_TIMEOUT_S, make_recording_transcriber
    from voice_assistant.config import with_keys
    from voice_assistant.stt import FILE_TIMEOUT_S, SPOKEN_FILE_TIMEOUT_S

    options = []
    client = types.SimpleNamespace(with_options=lambda **kw: options.append(kw) or client)
    shipped = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    cfg, _ = with_keys(shipped, {"DEEPGRAM_API_KEY", "OPENAI_API_KEY", "CARTESIA_API_KEY", "CEREBRAS_API_KEY"})
    env = tmp_path / ".env"
    env.write_text("DEEPGRAM_API_KEY=x\nOPENAI_API_KEY=x\n")
    spoken = make_recording_transcriber(cfg, env, client, spoken=True)
    assert spoken._transcriber._http.timeout.read == SPOKEN_FILE_TIMEOUT_S
    assert options == [{"timeout": OPENAI_TIMEOUT_S, "max_retries": 0}]
    message = make_recording_transcriber(cfg, env, client)  # a voice message, as Telegram's
    assert message._transcriber._http.timeout.read == FILE_TIMEOUT_S and options[1] == {"timeout": FILE_TIMEOUT_S}


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
