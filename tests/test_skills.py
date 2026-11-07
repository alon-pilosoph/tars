"""Skills and preferences: taught, changed and removed by talking to TARS, kept in the event database, told to the
model on each request for whoever is asking; a skill with set times runs by itself, aloud at home or in its owner's
chat."""

import types
from datetime import UTC, datetime

import pytest

from voice_assistant.audio import chime
from voice_assistant.config import ConfigError, LLMConfig, load_config
from voice_assistant.home import Request
from voice_assistant.llm import SKIP, OpenAIChat
from voice_assistant.skills import ALOUD, CHAT, HOUSEHOLD, Clock, Skill, Skills, import_config, on_its_own
from voice_assistant.store import Store

from .conftest import make_assistant, speech
from .test_llm import fake_openai_chat

NEWS = Skill(
    "news",
    "Unless the Fire TV is already on Channel 11 or 12, ask Alon whether he wants the news on, and which channel.",
    ("19:55",),
    ("sun", "mon"),
)
SUNDAY = datetime(2026, 11, 8, 19, 55, 10, tzinfo=UTC)


@pytest.fixture
def skills(tmp_path):
    return Skills(Store(tmp_path / "db"), clock=lambda: 1000.0)


def teach(skills, speaker="Alon", **args):
    full = {
        "action": "save",
        "name": None,
        "instructions": None,
        "when": None,
        "at": None,
        "days": None,
        "say": None,
        "household": None,
        "about": None,
        "guess": None,
    } | args
    return skills.call(full, types.SimpleNamespace(speaker=speaker))


# Taught by talking.


def test_a_skill_taught_is_kept_and_told_to_whoever_may_use_it(skills):
    said = teach(skills, name="Bedtime", instructions="Turn the TV  off, and the AC to 24.", household=True)
    assert said == "done: saved the household's skill bedtime; say so in a few words"
    teach(skills, name="morning", instructions="Tell me the news.", at="7:30", days="sun, Monday", say=CHAT)
    note = skills.note("Alon")
    assert note.startswith("Skills (Alon's and the household's):")
    assert "- morning (Alon's; runs by itself at 07:30 on mon, sun, in their chat): Tell me the news." in note
    assert "- bedtime (household): Turn the TV off, and the AC to 24." in note
    assert "morning" not in skills.note("Stacey") and "bedtime" in skills.note("Stacey")


def test_saving_one_again_changes_it(skills):
    teach(skills, name="panda", instructions="Play it.", household=True)
    said = teach(skills, name="panda", instructions="Play it on the Fire TV.", when="kids want calm", household=True)
    assert said.startswith("done: changed the household's skill panda")
    (panda,) = skills.all()
    assert (panda.instructions, panda.when) == ("Play it on the Fire TV.", "kids want calm")


def test_their_own_skill_of_the_same_name_is_the_one_they_get(skills):
    teach(skills, name="bedtime", instructions="Lights off.", household=True)
    teach(skills, name="bedtime", instructions="Read me a story.")
    assert [s.instructions for s in skills.visible("alon")] == ["Read me a story."]
    assert [s.instructions for s in skills.visible("Stacey")] == ["Lights off."]


def test_pause_resume_and_delete_by_name(skills):
    teach(skills, name="news", instructions="Ask.", at="19:55")
    assert teach(skills, action="pause", name="News") == "done: news paused"
    assert skills.all()[0].paused and "PAUSED" in skills.note("Alon")
    assert teach(skills, action="resume", name="news") == "done: news resumed"
    assert teach(skills, action="delete", name="news") == "done: news deleted"
    assert teach(skills, action="delete", name="news").startswith("error: there's no skill called 'news'")


def test_someone_else_cant_change_your_own_skill(skills):
    teach(skills, name="morning", instructions="Tell me the news.")
    assert teach(skills, speaker="Stacey", action="delete", name="morning").startswith("error: there's no skill")
    assert len(skills.all()) == 1


@pytest.mark.parametrize(
    "args, says",
    [
        ({"name": "x"}, "error: save needs the skill's name and its instructions"),
        ({"name": "x", "instructions": "y", "at": "25:00"}, "error: '25:00' isn't a time of day"),
        ({"name": "x", "instructions": "y", "days": "sun"}, "error: days are for a skill that runs by itself"),
        ({"name": "x", "instructions": "y", "at": "7:00", "days": "someday"}, "error: 'someday' isn't a day"),
        ({"name": "x", "instructions": "y", "household": True, "say": CHAT}, "error: a household skill is said aloud"),
    ],
)
def test_what_cant_be_saved_says_why(skills, args, says):
    assert teach(skills, **args).startswith(says)
    assert skills.all() == []


def test_only_someone_tars_knows_can_teach_it(skills):
    assert teach(skills, speaker=None, name="x", instructions="y").startswith("refused: TARS doesn't know who")
    assert teach(skills, speaker=None, action="list") == "Nothing taught yet: no skills, and no preferences."


def test_a_turn_thrown_away_teaches_nothing(skills):
    withdrawn = types.SimpleNamespace(speaker="Alon", go_ahead=lambda: False)
    args = {"action": "save", "name": "x", "instructions": "y"}
    assert skills.call(args, withdrawn).startswith("error: the request was withdrawn")
    assert skills.all() == []


# Preferences.


def test_preferences_are_remembered_for_that_person_guesses_marked(skills):
    teach(skills, action="remember", instructions="Skip esports.", about="Gaming news")
    said = teach(skills, action="remember", instructions="Likes the AC at 23.", about="the AC", guess=True)
    assert said == "done: remembered (as a guess)"
    note = skills.note("Alon")
    assert (
        "What Alon likes and doesn't:\n- (the ac) Likes the AC at 23. [a guess]\n- (gaming news) Skip esports." in note
    )
    assert skills.note("Stacey") == ""


def test_saying_a_guess_makes_it_no_longer_one(skills):
    teach(skills, action="remember", instructions="Likes the AC at 23.", guess=True)
    teach(skills, action="remember", instructions="likes the AC at 23.")
    assert "[a guess]" not in skills.note("Alon") and len(skills.preferences("alon")) == 1


def test_forgetting_by_its_words(skills):
    teach(skills, action="remember", instructions="Skip esports.")
    assert teach(skills, action="forget", instructions="esports") == "done: forgot Skip esports."
    assert teach(skills, action="forget", instructions="esports").startswith("error: nothing they like")


# Run by themselves.


def test_the_clock_keeps_each_skill_at_its_times_on_its_days_once_and_for_its_own_tars():
    chat = Skill("digest", "Tell me.", ("19:55",), say=CHAT, owner="Alon")
    paused = Skill("old", "Ask.", ("19:55",), paused=True)
    clock = Clock(lambda: [NEWS, Skill("panda", "Play it."), chat, paused], ALOUD)
    clock.look(SUNDAY)
    clock.look(SUNDAY.replace(second=40))  # the same minute: kept once
    assert clock.take() == [(NEWS, SUNDAY)] and not clock.is_due()
    clock.look(datetime(2026, 11, 10, 19, 55, tzinfo=UTC))  # a Tuesday
    assert not clock.is_due()
    chats = Clock(lambda: [NEWS, chat], CHAT)
    chats.look(SUNDAY)
    assert chats.take() == [(chat, SUNDAY)]


def test_a_skill_taught_a_minute_ago_is_kept_to(skills):
    clock = Clock(skills.all, ALOUD)
    clock.look(SUNDAY)
    teach(skills, name="news", instructions="Ask.", at="19:56", household=True)
    clock.look(SUNDAY.replace(minute=56))
    assert [s.name for s, _ in clock.take()] == ["news"]


def test_a_table_that_cant_be_read_waits_for_the_next_tick():
    def broken():
        raise OSError("locked")

    clock = Clock(broken, ALOUD)
    clock.look(SUNDAY)
    assert not clock.is_due()


def test_its_own_turn_says_nobody_asked_whose_it_is_and_where_its_said():
    asked = on_its_own(NEWS, SUNDAY, SKIP)
    assert asked.startswith("(Nobody said this: it's 19:55, when the skill news runs by itself. Do what")
    assert "said aloud, unprompted" in asked and f"reply with exactly {SKIP}" in asked
    mine = on_its_own(Skill("digest", "Tell me.", ("07:30",), owner="Alon", say=CHAT), SUNDAY, SKIP)
    assert "runs by itself, for Alon." in mine and "posted in their chat" in mine


def due(*skills):
    clock = Clock(lambda: list(skills), ALOUD)
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
    assert brain.asked[0] == on_its_own(NEWS, SUNDAY, SKIP) and brain.requests[0].speaker is None
    assert played[:2] == [chime(24_000), b"Alon, should I put the news on?"]
    assert brain.asked[1] == "[Follow-up, no wake word] twelve"
    assert not assistant._due()


def test_a_persons_own_skill_runs_as_theirs(speaker):
    assistant, brain = make_assistant(speaker, [None], [], replies=["Morning."])
    assistant._skill_clock = due(Skill("morning", "Tell me the news.", ("19:55",), owner="Alon"))
    assistant.run_skills(follow_up_s=4.0)
    assert brain.requests[0].speaker == "Alon"  # their feeds, and what they like


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


def test_muted_a_skill_that_asks_unmutes_it_for_the_answer_then_mutes_it_again(speaker):
    assistant, brain = make_assistant(speaker, [speech(), None], ["no thanks"], replies=["News on?", "Okay."])
    switched = []

    def button(muted):
        switched.append(muted)
        assistant.mute(muted)

    assistant.set_muted = button
    assistant.mute(True)
    assistant._skill_clock = due(NEWS)
    assistant.run_skills(follow_up_s=4.0)
    assert brain.asked[1] == "[Follow-up, no wake word] no thanks"
    assert switched == [False, True] and assistant.muted


def test_muted_a_skill_that_only_tells_stays_muted(speaker):
    assistant, _ = make_assistant(speaker, [None], [], replies=["The news is on."])
    assistant.set_muted = lambda muted: pytest.fail("nothing to listen for")
    assistant.mute(True)
    assistant._skill_clock = due(NEWS)
    assistant.run_skills(follow_up_s=4.0)
    assert assistant.muted


# The brain, and the old config.


def test_the_brain_has_the_tool_and_each_request_knows_the_askers_skills(skills):
    teach(skills, name="panda", instructions="Play the panda video.", household=True)
    teach(skills, action="remember", instructions="Skip esports.")
    remember = {"action": "remember", "instructions": "Likes jazz."}
    client = fake_openai_chat(
        lambda m: "Saved." if m[-1].get("type") == "function_call_output" else "",
        lambda m: [("skills", remember)] if m[-1].get("role") == "user" else [],
    )
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False), skills=skills)
    assert "".join(brain.stream_reply("I like jazz", Request(speaker="Alon"))) == "Saved."
    first = client.requests[0]
    assert any(t.get("name") == "skills" for t in first["tools"])
    assert "Skills and preferences: you're taught them by talking" in first["instructions"]
    assert "- panda (household): Play the panda video." in first["instructions"]
    assert "- Skip esports." in first["instructions"]
    assert [p["text"] for p in skills.preferences("alon")] == ["Likes jazz.", "Skip esports."]


def test_config_skills_are_imported_once_as_the_households(skills, tmp_path):
    local = tmp_path / "config.local.toml"
    local.write_text(
        '[[home.skills]]\nname = "Panda"\ninstructions = "Play it."\nwhen = "kids"\n\n'
        '[[home.skills]]\nname = "news"\ninstructions = "Ask."\nat = "19:55"\n'
    )
    assert import_config(local, skills) == ["panda", "news"]
    assert import_config(local, skills) == []  # once
    by_name = {s.name: s for s in skills.all()}
    assert (by_name["panda"].owner, by_name["panda"].when, by_name["news"].at) == (HOUSEHOLD, "kids", ("19:55",))


def test_skills_left_in_the_config_stop_it_saying_how_to_import_them(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[home.skills]]\nname = "panda"\ninstructions = "Play it."')
    with pytest.raises(ConfigError, match="import them once with"):
        load_config(path)
