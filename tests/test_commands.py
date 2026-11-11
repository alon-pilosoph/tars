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
    assert all(COMMANDS.match(phrase) for phrase in grammar[:-1])  # what Vosk can say, the matcher takes


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
    plain = Replies({"ac": "AC"}, dry=False)
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
