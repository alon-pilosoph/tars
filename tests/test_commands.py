"""House commands handled on the Pi (commands.py): phrases from the house's devices, answered without the model,
and heard by Vosk when the cloud's speech to text is out of reach."""

import pytest

from voice_assistant.commands import ON_PI, Command, Commands, HouseCommands, Replies, normalize, sentences
from voice_assistant.home import LOCK, OFF, ON, SET, STATUS, UNLOCK

from .conftest import make_assistant

DEVICES = [
    ("front door", "Front door", (STATUS, LOCK, UNLOCK), False),
    ("boiler", "Boiler", (STATUS, ON, OFF), False),
    ("ac", "AC", (STATUS, ON, OFF, SET), True),
    ("tv", "TV", (STATUS, ON, OFF), False),
    ("tv lights", "TV lights", (STATUS, ON, OFF), False),
]
COMMANDS = Commands(DEVICES)


@pytest.mark.parametrize(
    "said, command",
    [
        ("Lock the front door.", Command("front door", LOCK)),
        ("lock front door please", Command("front door", LOCK)),
        ("Hey TARS, turn the boiler on.", Command("boiler", ON)),
        ("Can you switch off the boiler?", Command("boiler", OFF)),
        ("Set the AC to twenty-three.", Command("ac", SET, 23)),
        ("AC to 23 degrees", Command("ac", SET, 23)),
        ("put the TV on", Command("tv", ON)),
        ("turn off the TV lights", Command("tv lights", OFF)),
    ],
)
def test_a_house_command_is_matched_however_its_said(said, command):
    assert COMMANDS.match(said) == command


@pytest.mark.parametrize(
    "said",
    [
        "unlock the front door",  # never on the Pi: unlocking stays with the model and its challenge
        "is the front door locked?",
        "turn on the boiler for forty minutes",
        "set the AC to 40",  # out of range: the model says so
        "don't turn on the boiler",
        "turn on the boiler and the AC",
        "what's the weather",
        "",
    ],
)
def test_anything_else_goes_to_the_model(said):
    assert COMMANDS.match(said) is None


def test_two_devices_said_the_same_way_are_left_to_the_model():
    both = Commands([("lamp", "Light", (ON, OFF), False), ("lantern", "Light", (ON, OFF), False)])
    assert both.match("turn on the light") is None and both.match("turn on the lamp") == Command("lamp", ON)


def test_vosk_is_told_every_phrase_in_words_and_unk_for_anything_else():
    grammar = COMMANDS.grammar()
    assert "set a c to twenty three" in grammar and "please lock front door" in grammar
    assert grammar[-1] == "[unk]" and not any(ch.isdigit() for phrase in grammar for ch in phrase)
    assert "[unk] turn on boiler" in grammar and "turn on boiler [unk]" in grammar  # words around it aren't squeezed in
    whole = [phrase for phrase in grammar if "[unk]" not in phrase]
    assert all(COMMANDS.match(phrase) for phrase in whole)  # what Vosk can say, the matcher takes


def test_numbers_and_the_little_words_are_evened_out():
    assert normalize("Set the AC to Twenty-Three!") == "set ac to 23"


def test_a_reply_never_repeats_the_one_before_and_a_failure_says_so():
    replies = Replies({"front door": "Front door", "ac": "AC"}, choose=lambda options: options[0])
    lock = Command("front door", LOCK)
    first, second = replies.say(lock, "done: front door reports locked"), replies.say(lock, "done: ...")
    assert first != second and first == "Front door's locked."
    assert replies.say(Command("ac", SET, 23), "done: ac reports 23") == "AC to 23 degrees."
    assert (
        replies.say(lock, "not confirmed: still locking")
        == "The front door didn't confirm it. Check it, or ask me again."
    )
    plain = Replies({"ac": "AC"}, witty=False)
    assert plain.lines(Command("ac", ON))[0] == "AC's on." and all("Fort Knox" not in l for l in plain.lines(lock))


def house(calls, result="done: boiler reports on", hear=None):
    def call(args, request):
        calls.append((args, request))
        return result

    return HouseCommands(COMMANDS, Replies({d[0]: d[1] for d in DEVICES}, choose=lambda o: o[0]), call, hear)


def test_a_command_is_done_with_the_home_tool_and_answered_without_the_model(speaker, tmp_path):
    from voice_assistant.events import EventLog
    from voice_assistant.journal import Journal

    journal = Journal(EventLog(tmp_path / "events"))
    journal.wake(b"\0\0" * 1600, 0.9, "answer", "hey tars", 0.9, "", "")
    assistant, brain = make_assistant(speaker, [b"q", None], ["Turn the boiler on, please."], journal=journal)
    calls = []
    assistant.commands = house(calls)
    assistant.converse(follow_up_s=4.0)
    assert brain.asked == []  # no model
    (args, request), = calls  # fmt: skip
    assert args == {"device": "boiler", "action": ON} and request.by_voice and request.heard.startswith("Turn")
    assert brain.remembered == [("Turn the boiler on, please.", "Boiler's on.")]
    turns = journal.conversations.get(journal.conversations.conversations()[0]["id"])["turns"]
    assert turns[-1]["text"] == "Boiler's on." and turns[-1]["answered_by"] == ON_PI


def test_anything_else_still_goes_to_the_model(speaker):
    assistant, brain = make_assistant(speaker, [b"q", None], ["what's the weather like"])
    calls = []
    assistant.commands = house(calls)
    assistant.converse(follow_up_s=4.0)
    assert calls == [] and brain.asked == ["what's the weather like"]


def test_with_the_cloud_out_of_reach_a_command_is_heard_on_the_pi(speaker):
    from voice_assistant.errors import ServiceError

    assistant, brain = make_assistant(speaker, [b"q", None], [ServiceError("Flux: no route to host")])
    calls, heard = [], []
    assistant.commands = house(
        calls, "done: front door reports locked", hear=lambda pcm: heard.append(pcm) or "lock front door"
    )
    assistant.converse(follow_up_s=4.0)
    assert heard and calls[0][0] == {"device": "front door", "action": LOCK} and brain.asked == []


def test_with_the_cloud_out_of_reach_anything_but_a_command_fails_as_before(speaker, capsys):
    from voice_assistant.errors import ServiceError

    assistant, _ = make_assistant(speaker, [b"q", None], [ServiceError("Flux: no route to host")])
    calls = []
    assistant.commands = house(calls, hear=lambda pcm: "")
    assistant.converse(follow_up_s=4.0)
    assert calls == [] and "ServiceError" in capsys.readouterr().out


def test_every_reply_sentence_is_made_ahead(speaker):
    assistant, _ = make_assistant(speaker, [], [])
    assistant.commands = house([])
    assistant.prepare_phrases()
    assistant.phrases.drain()
    assert "Front door's locked." in assistant.phrases and "Fort Knox has nothing on us." in assistant.phrases
    assert sentences("Locked. Fort Knox has nothing on us.") == ["Locked.", "Fort Knox has nothing on us."]


def test_the_house_says_which_devices_take_a_temperature(tmp_path):
    import dataclasses
    import types
    from pathlib import Path

    from voice_assistant.__main__ import house_commands
    from voice_assistant.config import load_config
    from voice_assistant.home import Device

    class AC(Device):
        actions = (STATUS, ON, OFF, SET)
        takes_temperature = True

        def what(self):
            return "an AC: on, off, set (16 to 30 degrees)"  # no "temperature" in it

    class Purifier(Device):
        actions = (STATUS, ON, OFF, SET)  # set: its fan

    shipped = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    cfg = dataclasses.replace(shipped, home=dataclasses.replace(shipped.home, commands_on_pi=True))
    home = types.SimpleNamespace(devices={"ac": AC("ac"), "purifier": Purifier("purifier")}, call=lambda a, r: "")
    made = house_commands(cfg, tmp_path, home)
    assert made.commands.match("AC to 23") == Command("ac", SET, 23)
    assert made.commands.match("purifier to 23") is None


@pytest.mark.parametrize(
    "result, said",
    [
        ("started: front door reports locking", "Front door's locking."),
        ("sent: asked the front door to lock; it can't report back", "Sent it to the front door."),
        (
            "not confirmed: asked the front door to lock, and it still says unlocked",
            "The front door didn't confirm it.",
        ),
        (
            "error: Home Assistant didn't answer; nothing is known to have changed",
            "Couldn't do that with the front door.",
        ),
        ("refused: this reply searched the web", "Couldn't do that with the front door."),
        ("something new", "The front door didn't confirm it."),  # claims neither way
    ],
)
def test_each_outcome_has_its_own_lines(result, said):
    replies = Replies({"front door": "Front door"}, choose=lambda options: options[0])
    assert replies.say(Command("front door", LOCK), result).startswith(said)


def test_every_outcomes_lines_are_made_ahead():
    replies = Replies({"ac": "AC"}, witty=False)
    assert {"AC's on.", "AC's turning on.", "Sent it to the AC.", "Couldn't do that with the AC."} <= set(
        replies.lines(Command("ac", ON))
    )


def test_every_all_caps_word_of_a_label_is_spelled_out_for_vosk():
    grammar = Commands([("fire tv", "Fire TV", (ON, OFF), False)]).grammar()
    assert "turn on the fire t v" in grammar and not any("fire tv" in phrase for phrase in grammar)


def test_a_follow_up_goes_to_the_model_which_may_skip_it(speaker):
    assistant, brain = make_assistant(speaker, [b"q", b"f", None], ["what time is it", "turn on the boiler"])
    calls = []
    assistant.commands = house(calls)
    assistant.converse(follow_up_s=4.0)
    assert calls == [] and len(brain.asked) == 2 and brain.asked[1].endswith("turn on the boiler")


def _prepared(assistant, text, may_act):
    import types

    draft = types.SimpleNamespace(cancelled=False, may_act=may_act)
    session = types.SimpleNamespace(transcript=lambda: text)
    answer = assistant._prepare(draft, b"\0\0" * 1600, session, False, None)
    list(answer.reply)
    return answer


def test_a_thrown_away_draft_says_and_remembers_nothing(speaker):
    from voice_assistant.home import Request

    assistant, brain = make_assistant(speaker, [], [])
    replies = Replies({"boiler": "Boiler"}, choose=lambda o: o[0])

    def call(args, request: Request):
        if not request.go_ahead():
            return "error: the request was withdrawn before anything was done; nothing changed"
        raise AssertionError("done in a draft that was thrown away")

    assistant.commands = HouseCommands(COMMANDS, replies, call)
    answer = _prepared(assistant, "turn on the boiler", may_act=lambda timeout_s: False)
    assert answer.spoken == [] and brain.remembered == [] and replies._last is None


def test_a_call_that_raises_is_said_as_couldnt(speaker):
    assistant, brain = make_assistant(speaker, [], [])

    def call(args, request):
        raise RuntimeError("the plugin broke")

    assistant.commands = HouseCommands(COMMANDS, Replies({"boiler": "Boiler"}, choose=lambda o: o[0]), call)
    answer = _prepared(assistant, "turn on the boiler", may_act=lambda timeout_s: True)
    assert "".join(answer.spoken).strip() == "Couldn't do that with the boiler."
    assert brain.remembered == [("turn on the boiler", "Couldn't do that with the boiler.")]


def test_a_made_ahead_line_that_isnt_there_is_said_live_and_a_voice_failure_ends_nothing(capsys):
    import types
    from concurrent.futures import Future

    from voice_assistant.assistant import _MadeAhead

    failed = Future()
    failed.set_exception(RuntimeError("TTS was down when it was made"))

    class Phrases:  # every line queued ahead, none made
        def __contains__(self, text):
            return True

        def get(self, text):
            return failed

    phrases = Phrases()
    live = types.SimpleNamespace(sample_rate=24_000, stream=lambda text: iter([b"live"]), warm=lambda: None)
    assert list(_MadeAhead(live, phrases).stream("Boiler's on.")) == [b"live"]

    def broken(text):
        raise RuntimeError("no network")
        yield b""

    down = types.SimpleNamespace(sample_rate=24_000, stream=broken, warm=lambda: None)
    assert list(_MadeAhead(down, phrases).stream("Boiler's on.")) == []
    assert "no network" in capsys.readouterr().out


def test_vosk_refuses_anything_with_an_unknown_word(monkeypatch, tmp_path):
    import json
    import sys
    import types

    from voice_assistant import commands, verify

    heard = iter(["[unk] turn on the boiler", "turn on the boiler"])

    class Recognizer:
        def __init__(self, model, rate, grammar):
            pass

        def AcceptWaveform(self, pcm):
            pass

        def FinalResult(self):
            return json.dumps({"text": next(heard)})

    fake = types.SimpleNamespace(
        KaldiRecognizer=Recognizer, Model=lambda path: object(), SetLogLevel=lambda level: None
    )
    monkeypatch.setitem(sys.modules, "vosk", fake)
    monkeypatch.setattr(verify, "ensure_model", lambda models_dir: tmp_path)
    hear = commands.vosk_hearing(COMMANDS.grammar(), tmp_path)
    assert hear(b"") == "" and hear(b"") == "turn on the boiler"
