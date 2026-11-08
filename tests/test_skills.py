"""Skills and preferences: taught, changed and removed by talking to TARS, kept in the event database, told to the
model on each request for whoever is asking; a skill with set times runs by itself, aloud at home or in its owner's
chat."""

import types
from datetime import UTC, datetime

import pytest

import voice_assistant.assistant as assistant_module
from voice_assistant.audio import chime
from voice_assistant.config import ConfigError, LLMConfig, load_config
from voice_assistant.home import Request
from voice_assistant.llm import SKIP, OpenAIChat
from voice_assistant.skills import ALOUD, CHAT, HOUSEHOLD, Clock, Due, Skill, Skills, import_config, on_its_own
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
        "trigger": None,
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
    assert clock.take() == [Due(NEWS, SUNDAY)] and not clock.is_due()
    clock.look(datetime(2026, 11, 10, 19, 55, tzinfo=UTC))  # a Tuesday
    assert not clock.is_due()
    chats = Clock(lambda: [NEWS, chat], CHAT)
    chats.look(SUNDAY)
    assert chats.take() == [Due(chat, SUNDAY)]


def test_a_skill_taught_a_minute_ago_is_kept_to(skills):
    clock = Clock(skills.all, ALOUD)
    clock.look(SUNDAY)
    teach(skills, name="news", instructions="Ask.", at="19:56", household=True)
    clock.look(SUNDAY.replace(minute=56))
    assert [d.skill.name for d in clock.take()] == ["news"]


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
    # Only the answer, a few seconds of it, and no listening for more once it's replied: the room was muted.
    assert assistant.recorder.longest == [assistant_module.ANSWER_ONLY_S]


def test_unmuted_a_skill_listens_for_follow_ups_as_any_conversation(speaker):
    assistant, _ = make_assistant(speaker, [speech(), None], ["no thanks"], replies=["News on?", "Okay."])
    assistant._skill_clock = due(NEWS)
    assistant.run_skills(follow_up_s=4.0)
    assert assistant.recorder.longest == [None, None]  # the answer, then the follow-up window


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


# Run on something happening.

STACEY_HOME = {"event": "arrives", "who": "Stacey", "device": None, "says": None, "above": None, "below": None,
               "reading": None}  # fmt: skip


def test_a_trigger_is_taught_checked_against_whats_there_and_described(skills):
    skills.devices, skills.people = (lambda: {"front door", "air quality"}), (lambda: {"alon", "stacey"})
    teach(skills, name="welcome", instructions="Tell her what's waiting.", trigger=STACEY_HOME, household=True)
    co2 = {**STACEY_HOME, "event": "device", "who": None, "device": "Air Quality", "above": 1000, "reading": "CO2"}
    teach(skills, name="air", instructions="Turn on the purifier and tell me.", trigger=co2)
    note = skills.note("Alon")
    assert "- welcome (household; runs by itself when Stacey comes home, said aloud): Tell her" in note
    assert "- air (Alon's; runs by itself when the air quality's co2 goes over 1000, said aloud)" in note
    assert teach(skills, name="x", instructions="y", trigger={**STACEY_HOME, "who": "bob"}).startswith(
        "error: who's home doesn't follow 'bob'"
    )
    door = {**STACEY_HOME, "event": "device", "device": "back door", "says": "open"}
    assert teach(skills, name="x", instructions="y", trigger=door) == "error: there's no device called 'back door'"
    assert teach(skills, name="x", instructions="y", at="7:00", trigger=STACEY_HOME).startswith(
        "error: a skill runs at set times or on something happening, not both"
    )


class House:
    """Who's home and the devices' statuses, as a test sets them."""

    def __init__(self):
        self.home = {"Alon": True, "Stacey": False}
        self.statuses = {"front door": "front door: locked", "air quality": "air quality: CO2 800 ppm, PM2.5 4"}

    def clock(self, *skills, now=None):
        ticks = [0.0]
        clock = Clock(
            lambda: list(skills),
            ALOUD,
            people=lambda: dict(self.home),
            status=lambda d: self.statuses[d],
            clock=lambda: ticks[0],
        )
        clock.ticks = ticks
        return clock


def on(trigger, name="it", **kw):
    return Skill(name, "Do it.", trigger={"who": None, "says": None, "above": None, "below": None,
                                          "reading": None, **trigger}, id=hash(name), **kw)  # fmt: skip


def test_someone_coming_home_sets_off_the_skills_waiting_for_them():
    house = House()
    for_stacey, for_anyone = on({"event": "arrives", "who": "stacey"}, "a"), on({"event": "arrives"}, "b")
    for_alon = on({"event": "arrives", "who": "alon"}, "c")
    clock = house.clock(for_stacey, for_anyone, for_alon)
    clock.look(SUNDAY)  # the first look only sees who's home
    assert not clock.is_due()
    house.home["Stacey"] = True
    clock.look(SUNDAY)
    assert [(d.skill.name, d.why) for d in clock.take()] == [
        ("a", "Stacey just came home"),
        ("b", "Stacey just came home"),
    ]


def test_everyone_is_the_last_one_out_and_the_first_one_home():
    house = House()
    last, first = (
        on({"event": "leaves", "who": "everyone"}, "last"),
        on({"event": "arrives", "who": "everyone"}, "first"),
    )
    clock = house.clock(last, first)
    clock.look(SUNDAY)
    house.home["Alon"] = False
    clock.look(SUNDAY)
    assert [(d.skill.name, d.why) for d in clock.take()] == [("last", "Alon just went out (nobody's home now)")]
    house.home["Stacey"] = True
    clock.look(SUNDAY)
    assert [d.skill.name for d in clock.take()] == ["first"]
    house.home["Alon"] = True  # someone was home already
    clock.ticks[0] += 3600
    clock.look(SUNDAY)
    assert clock.take() == []


def test_a_device_saying_a_word_it_didnt_sets_off_a_skill_once():
    house = House()
    clock = house.clock(on({"event": "device", "device": "front door", "says": "unlocked"}))
    clock.look(SUNDAY)
    house.statuses["front door"] = "front door: unlocked, door open"
    clock.look(SUNDAY)
    (due,) = clock.take()
    assert due.why == "the front door now says unlocked (front door: unlocked, door open)"
    clock.look(SUNDAY)  # still unlocked: nothing new
    assert clock.take() == []


def test_locked_isnt_said_by_unlocked():
    house = House()
    house.statuses["front door"] = "front door: unlocked"
    clock = house.clock(on({"event": "device", "device": "front door", "says": "locked"}))
    clock.look(SUNDAY)
    house.statuses["front door"] = "front door: unlocked, door open"
    clock.look(SUNDAY)
    assert clock.take() == []
    house.statuses["front door"] = "front door: locked"
    clock.look(SUNDAY)
    assert len(clock.take()) == 1


def test_a_reading_crossing_its_limit_sets_off_a_skill_but_not_again_within_the_cooldown():
    house = House()
    clock = house.clock(on({"event": "device", "device": "air quality", "above": 1000, "reading": "co2"}))
    clock.look(SUNDAY)
    house.statuses["air quality"] = "air quality: CO2 1100 ppm, PM2.5 4"
    clock.look(SUNDAY)
    assert [d.why for d in clock.take()] == ["the air quality's co2 went over 1000, to 1100"]
    for co2 in (900, 1200):  # flapping around its limit
        house.statuses["air quality"] = f"air quality: CO2 {co2} ppm, PM2.5 4"
        clock.look(SUNDAY)
    assert clock.take() == []
    clock.ticks[0] += 601
    house.statuses["air quality"] = "air quality: CO2 900 ppm"
    clock.look(SUNDAY)
    house.statuses["air quality"] = "air quality: CO2 1300 ppm"
    clock.look(SUNDAY)
    assert len(clock.take()) == 1


def test_a_device_that_doesnt_answer_sets_nothing_off():
    house = House()
    clock = house.clock(on({"event": "device", "device": "front door", "says": "unlocked"}))
    clock.look(SUNDAY)
    house.statuses = {}  # KeyError: it doesn't answer
    clock.look(SUNDAY)
    house.statuses = {"front door": "front door: unlocked"}
    clock.look(SUNDAY)  # compared with the last answer it gave
    assert len(clock.take()) == 1


def test_its_turn_says_what_happened():
    asked = on_its_own(Skill("welcome", "Greet her."), SUNDAY, SKIP, "Stacey just came home")
    assert asked.startswith("(Nobody said this: it's 19:55 and Stacey just came home, when the skill welcome runs")


def test_runs_are_kept_and_told(skills):
    teach(skills, name="news", instructions="Ask.", at="19:55", household=True)
    teach(skills, name="morning", instructions="Tell me.", at="7:30", say=CHAT)
    (news,) = [s for s in skills.all() if s.name == "news"]
    (morning,) = [s for s in skills.all() if s.name == "morning"]
    skills.ran(news, "at 19:55", "Alon, should I put the news on?")
    skills.ran(morning, "at 07:30", "")
    told = teach(skills, action="runs")
    assert told.startswith("What skills did by themselves lately, newest first:")
    assert "morning (at 07:30): nothing to do" in told and "news (at 19:55): Alon, should I put" in told
    assert "morning" not in teach(skills, speaker="Stacey", action="runs")


def test_a_run_is_recorded_with_what_it_said(speaker):
    runs = []
    assistant, _ = make_assistant(speaker, [None], [], replies=["News?"])
    clock = Clock(lambda: [NEWS], ALOUD, record=lambda skill, why, said: runs.append((skill.name, why, said)))
    clock.look(SUNDAY)
    assistant._skill_clock = clock
    assistant.run_skills(follow_up_s=4.0)
    assert runs == [("news", "at 19:55", "News?")]


def test_a_reading_is_checked_against_what_the_device_says_now(skills):
    status = "air quality: 28, home PM2.5: 4 μg/m³, home Carbon dioxide: 801 ppm"
    skills.devices, skills.status = (lambda: {"air quality"}), (lambda device: status)
    co2 = {**STACEY_HOME, "event": "device", "who": None, "device": "air quality", "above": 1000, "reading": "co2"}
    said = teach(skills, name="air", instructions="Purifier on.", trigger=co2)
    assert said == f"error: the air quality's status has no 'co2' reading; it says: {status}"
    said = teach(skills, name="air", instructions="Purifier on.", trigger={**co2, "reading": "carbon dioxide"})
    assert said.startswith("done: saved")
    house = House()
    house.statuses["air quality"] = status
    clock = house.clock(*skills.all())
    clock.look(SUNDAY)
    house.statuses["air quality"] = status.replace("801", "1150")
    clock.look(SUNDAY)
    assert [d.why for d in clock.take()] == ["the air quality's carbon dioxide went over 1000, to 1150"]


# The web UI's Skills page.


def test_the_skills_page_shows_everything_taught_and_done_and_changes_it(log, client):
    skills = Skills(log.store)
    teach(skills, name="panda", instructions="Play it.", when="kids want calm", household=True)
    teach(skills, name="welcome", instructions="Greet her.", trigger={**STACEY_HOME, "who": "stacey"})
    teach(skills, action="remember", instructions="Skip esports.", about="gaming", guess=True)
    (welcome,) = [s for s in skills.all() if s.name == "welcome"]
    skills.ran(welcome, "Stacey just came home", "Welcome home, Stacey.")
    page = client.get("/api/skills").json()
    by_name = {s["name"]: s for s in page["skills"]}
    assert by_name["panda"] | {"id": 0} == {
        "id": 0,
        "name": "panda",
        "instructions": "Play it.",
        "when": "kids want calm",
        "owner": "household",
        "household": True,
        "runs_by_itself": "",
        "say": "aloud",
        "paused": False,
    }
    assert by_name["welcome"]["runs_by_itself"] == "when Stacey comes home"
    assert [(p["person"], p["text"], p["guessed"]) for p in page["preferences"]] == [("alon", "Skip esports.", True)]
    assert [(r["name"], r["why"]) for r in page["runs"]] == [("welcome", "Stacey just came home")]
    panda = by_name["panda"]["id"]
    assert client.post(f"/api/skills/{panda}/pause", json={"paused": True}).json() == {"ok": True}
    assert [s.paused for s in skills.all() if s.name == "panda"] == [True]
    assert client.delete(f"/api/skills/{panda}").json() == {"ok": True}
    assert client.delete(f"/api/skills/{panda}").status_code == 404
    pref = page["preferences"][0]["id"]
    assert client.delete(f"/api/preferences/{pref}").json() == {"ok": True}
    assert skills.preferences("alon") == []


UPDATED = {"event": "updated", "who": None, "device": None, "says": None, "above": None, "below": None, "reading": None}


def test_tell_me_when_you_learn_something_new_runs_once_for_each_update_with_what_changed(skills):
    # Stacey's: "keep me updated when Alon teaches you how to do more things", in her chat.
    said = teach(skills, speaker="Stacey", name="news of me", instructions="Tell me what's new.", trigger=UPDATED,
                 say=CHAT)  # fmt: skip
    assert said.startswith("done: saved")
    assert "runs by itself when TARS is updated" in teach(skills, speaker="Stacey", action="list")
    running = ["aaa1111"]
    asked = []

    def between(old, new):
        asked.append((old, new))
        return "2026-11-09 TARS knows itself (#95)"

    clock = Clock(
        skills.all, CHAT, version=lambda: running[0], between=between, seen=skills.seen_version, see=skills.see_version
    )
    clock.look(SUNDAY)
    assert clock.take() == []  # just saved: it notes the version it starts from
    clock.look(SUNDAY)
    assert clock.take() == []  # nothing new
    running[0] = "bbb2222"  # deployed, TARS restarted: a new clock, the same database
    later = Clock(
        skills.all, CHAT, version=lambda: running[0], between=between, seen=skills.seen_version, see=skills.see_version
    )
    later.look(SUNDAY)
    (due,) = later.take()
    assert due.skill.name == "news of me" and "TARS was just updated (aaa1111 to bbb2222)" in due.why
    assert "TARS knows itself (#95)" in due.why and asked == [("aaa1111", "bbb2222")]
    later.look(SUNDAY)
    assert later.take() == []  # told once
    aloud = Clock(skills.all, ALOUD, version=lambda: "ccc3333", seen=skills.seen_version, see=skills.see_version)
    aloud.look(SUNDAY)
    assert aloud.take() == []  # hers is said in her chat, not aloud
