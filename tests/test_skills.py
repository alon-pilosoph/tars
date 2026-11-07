"""Skills: plain-words instructions under a name, for the model to carry out with its tools; a skill with set times
runs by itself, from the voice's TARS, as a turn of its own that's said aloud and listened to."""

import re
from datetime import UTC, datetime

import pytest

from voice_assistant.audio import chime
from voice_assistant.config import ConfigError, HomeConfig, load_config
from voice_assistant.home import STATUS, Device, HomeTools
from voice_assistant.llm import SKIP
from voice_assistant.skills import Clock, Skill, from_config, on_its_own

from .conftest import make_assistant, speech

NEWS = Skill(
    "news",
    "Unless the Fire TV is already on Channel 11 or 12, ask Alon whether he wants the news on, and which channel.",
    ("19:55",),
    ("sun", "mon"),
)
SUNDAY = datetime(2026, 11, 8, 19, 55, 10, tzinfo=UTC)


class Thing(Device):
    actions = (STATUS,)

    def what(self):
        return "a thing"

    def status(self):
        return f"{self.name}: fine"


def test_the_instructions_carry_each_skill_in_its_own_words():
    cfg = HomeConfig(skills=[{"name": "panda", "instructions": "Play  https://youtu.be/x\n on the TV."}])
    rules = HomeTools(cfg, {"tv": Thing("tv")}).rules()
    assert "Skills: when someone asks for one of these by name" in rules
    assert rules.endswith("- panda: Play https://youtu.be/x on the TV.")


def test_no_skills_no_word_of_them():
    assert "Skills:" not in HomeTools(HomeConfig(), {"tv": Thing("tv")}).rules()


def test_from_config_reads_times_and_days():
    (news,) = from_config([{"name": " news ", "instructions": "Ask.", "at": "19:55", "days": ["Sunday"]}])
    assert news == Skill("news", "Ask.", ("19:55",), ("sun",))


def test_its_own_turn_says_nobody_asked_and_how_to_say_nothing():
    asked = on_its_own(NEWS, SUNDAY, SKIP)
    assert asked.startswith("(Nobody said this: it's 19:55, when the skill news runs by itself.")
    assert f"reply with exactly {SKIP}" in asked and asked.endswith(f"Instructions: {NEWS.instructions}")


def test_the_clock_keeps_a_skill_at_its_times_on_its_days_once():
    clock = Clock([NEWS, Skill("panda", "Play it.")])
    assert clock.skills == [NEWS]  # one with no time never runs by itself
    clock.look(SUNDAY)
    clock.look(SUNDAY.replace(second=40))  # the same minute: kept once
    assert clock.is_due() and clock.take() == [(NEWS, SUNDAY)]
    assert not clock.is_due() and clock.take() == []
    clock.look(datetime(2026, 11, 10, 19, 55, tzinfo=UTC))  # a Tuesday
    clock.look(datetime(2026, 11, 8, 19, 56, tzinfo=UTC))
    assert not clock.is_due()
    clock.look(datetime(2026, 11, 9, 19, 55, tzinfo=UTC))  # Monday
    assert clock.is_due()


# Its own turn, said aloud.


def due(*skill):
    clock = Clock(list(skill))
    clock.look(SUNDAY)
    return clock


def test_a_skill_due_is_a_turn_said_after_a_chime_and_its_answer_heard_without_the_wake_word(speaker):
    assistant, brain = make_assistant(
        speaker, [speech(), None], ["twelve"], replies=["Alon, should I put the news on?", "Channel 12 is on."]
    )
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    assistant.voice.stream = lambda text: iter([text.encode()])
    assistant._skill_clock = due(NEWS)
    assert assistant._due()
    assistant.run_skills(follow_up_s=4.0)
    assert brain.asked[0] == on_its_own(NEWS, SUNDAY, SKIP)
    assert played[:2] == [chime(24_000), b"Alon, should I put the news on?"]
    assert brain.asked[1] == "[Follow-up, no wake word] twelve"
    assert not assistant._due()


def test_a_skill_with_nothing_to_do_says_nothing_and_is_forgotten(speaker):
    assistant, brain = make_assistant(speaker, [], [], replies=[SKIP])
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    assistant._skill_clock = due(NEWS)
    assistant.run_skills(follow_up_s=4.0)
    assert played == [] and brain.forgotten == 1


def test_a_skill_whose_turn_fails_waits_for_its_next_time(speaker):
    assistant, brain = make_assistant(speaker, [], [])

    def down(text, request=None):
        raise ConnectionError("no model")

    brain.stream_reply = down
    assistant._skill_clock = due(NEWS)
    assistant.run_skills(follow_up_s=4.0)
    assert brain.forgotten == 1 and not assistant._due()


# Checked at startup.


def config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return load_config(path)


def test_a_skill_with_times_loads(tmp_path):
    cfg = config(tmp_path, '[[home.skills]]\nname = "news"\ninstructions = "Ask."\nat = "19:55"\ndays = ["sun"]')
    assert cfg.home.skills[0]["at"] == "19:55"


@pytest.mark.parametrize(
    "setup, says",
    [
        ('instructions = ""', "needs instructions: what to do, in plain words"),
        ('steps = [{ device = "tv", action = "on" }]', "steps are gone: say what to do in plain words"),
        ('instructions = "Ask."\nat = "7:55"', 'at is a time of day like "19:55"'),
        ('instructions = "Ask."\nat = ["19:55", "25:00"]', 'at is a time of day like "19:55"'),
        ('instructions = "Ask."\nat = "19:55"\ndays = ["someday"]', "days are like"),
        ('instructions = "Ask."\ndays = ["sun"]', "days are when it runs by itself, so it needs at"),
    ],
)
def test_a_skill_that_cant_work_stops_at_startup(tmp_path, setup, says):
    with pytest.raises(ConfigError, match=re.escape(says)):
        config(tmp_path, f'[[home.skills]]\nname = "news"\n{setup}')
