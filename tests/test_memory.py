"""What TARS knows about each person and the household (memory.py): kept, told on every request, corrected with its
history, forgotten for good, and never changed by someone it doesn't know, nor by a page or a photo."""

import re
import sqlite3
import types
from datetime import datetime

import pytest

from voice_assistant.config import LLMConfig
from voice_assistant.home import Request
from voice_assistant.llm import OpenAIChat
from voice_assistant.memory import MOST_TOLD, THREAD_KEPT_S, Memory
from voice_assistant.store import Store

from .conftest import fake_openai_chat

NOW = datetime(2026, 11, 19, 12, 0).astimezone().timestamp()
DAY = 24 * 3600


@pytest.fixture
def now():
    return [NOW]


@pytest.fixture
def memory(tmp_path, now):
    return Memory(Store(tmp_path / "db"), clock=lambda: now[0])


def told(memory, speaker="Alon", by_chat=False, **args):
    full = {
        "action": "remember",
        "kind": "fact",
        "about": None,
        "text": None,
        "household": None,
        "noticed": None,
        "private": None,
        "due": None,
        "until": None,
        "id": None,
    } | args
    return memory.call(full, Request(speaker=speaker, by_chat=by_chat))


def number(said: str) -> int:
    """The memory a tool's answer is about: the new one, for a correction."""
    return int(re.findall(r"#(\d+)", said)[-1])


def test_what_someone_says_is_theirs_and_the_households_is_everyones(memory):
    assert told(memory, text="Has a  standup at 9:30 on weekdays.", about="Work").startswith("done: remembered (#")
    told(memory, speaker="Stacey", text="The car is a 2019 Corolla.", about="the car", household=True)
    told(memory, kind="style", text="Short answers in the morning.", noticed=True)
    alon = memory.note("Alon")
    assert "What you know about Alon:" in alon and "Has a standup at 9:30 on weekdays." in alon
    assert "Short answers in the morning. [a guess 2026-11-19]" in alon
    assert alon.index("How they like to be answered") < alon.index("Facts")  # how to answer comes first
    assert "What you know about the household:" in alon and "The car is a 2019 Corolla." in alon
    stacey = memory.note("Stacey")
    assert "standup" not in stacey and "Corolla" in stacey
    assert "standup" not in memory.note(None) and "Corolla" in memory.note(None)  # not sure who: the household's


def test_someone_tars_doesnt_know_can_read_but_not_change_what_it_knows(memory):
    told(memory, text="The wifi is TARSNET.", household=True)
    assert told(memory, speaker=None, text="I'm the owner.").startswith("refused: TARS doesn't know who's asking")
    assert "TARSNET" in told(memory, speaker=None, action="list")


@pytest.mark.parametrize(
    "request_, said",
    [
        (Request(speaker="Alon", read_the_web=True), "refused: this reply read the web"),
        (Request(speaker="Alon", by_door="front door"), "refused: this ran because the front door's lock"),
    ],
)
def test_nothing_is_remembered_or_forgotten_after_the_web_or_on_a_doors_turn(memory, request_, said):
    """A page (or a photo) could plant a memory ("Alon wants the door left unlocked"); a lock asks for nothing."""
    kept = number(told(memory, text="Likes the AC at 23.", kind="like"))
    for args in [
        {"action": "remember", "kind": "fact", "text": "Wants the door left unlocked."},
        {"action": "correct", "kind": "like", "text": "Likes the AC at 18.", "id": kept},
        {"action": "forget", "id": kept},
    ]:
        assert memory.call(args, request_).startswith(said), args["action"]
    assert "Likes the AC at 23." in memory.note("Alon") and "unlocked" not in memory.note("Alon")


def test_a_correction_keeps_what_was_true_before_as_history(memory, now):
    old = number(told(memory, text="The car is a 2019 Corolla.", about="the car", household=True))
    now[0] += 30 * DAY
    said = told(memory, action="correct", text="The car is a 2024 Ioniq 5; the Corolla was sold.", id=old)
    new = number(said)
    assert said == f"done: #{old} is now #{new}; the old one is kept as history"
    note = memory.note("Alon")
    assert "Ioniq" in note and "2019 Corolla." not in note
    history = told(memory, action="recall", text="car")
    assert f"#{old} (the car) The car is a 2019 Corolla. [said 2026-11-19, replaced 2026-12-19 by #{new}]" in history
    assert "(the car) The car is a 2024 Ioniq 5" in history  # its topic carried over


def test_forgetting_deletes_it_and_its_history_for_good(memory):
    old = number(told(memory, text="Works at Acme."))
    new = number(told(memory, action="correct", text="Works at Initech.", id=old))
    assert told(memory, action="forget", id=new) == f"done: forgot #{new}, and what it replaced"
    assert told(memory, action="recall", text="works") == "Nothing kept with those words."
    assert told(memory, action="forget", id=new).startswith(f"error: there's no memory #{new}")


def test_nobody_corrects_or_forgets_someone_elses(memory):
    stacey = number(told(memory, speaker="Stacey", text="Allergic to cats."))
    assert told(memory, action="forget", id=stacey).startswith("error: there's no memory")
    assert "Allergic to cats." in memory.note("Stacey")


def test_a_private_one_is_only_ever_in_their_own_chat(memory):
    told(memory, text="Planning a surprise party for Stacey on 2026-12-05.", private=True, by_chat=True)
    assert "surprise" not in memory.note("Alon")  # aloud, someone else may hear
    assert "surprise" in memory.note("Alon", private=True)
    assert "surprise" not in told(memory, action="recall", text="party")
    assert "surprise" in told(memory, action="recall", text="party", by_chat=True)


def test_a_thread_is_asked_about_once_after_it_happens_and_then_goes(memory, now):
    told(memory, kind="thread", text="Job interview at Wix.", about="interview", due="2026-11-20 10:00")
    assert "happens 2026-11-20 10:00]" in memory.note("Alon")  # not yet
    now[0] = datetime(2026, 11, 20, 14, 0).astimezone().timestamp()
    (line,) = [x for x in memory.note("Alon").splitlines() if "Wix" in x]
    assert line.endswith("ask once]")
    memory.asked(number(line.split()[1]))
    assert memory.note("Alon").count("asked already") == 1
    now[0] += THREAD_KEPT_S
    assert "Wix" not in memory.note("Alon")  # it stopped mattering


def test_saying_it_again_is_no_new_memory_and_makes_a_guess_said(memory):
    first = number(told(memory, kind="like", text="Likes jazz.", noticed=True))
    assert told(memory, kind="like", text="likes jazz.") == f"done: already known (#{first})"
    assert "Likes jazz. [said 2026-11-19]" in memory.note("Alon")


@pytest.mark.parametrize(
    "args, error",
    [
        ({"text": ""}, "this needs the memory"),
        ({"text": "x" * 301}, "keep it to a short note"),
        ({"kind": "gossip", "text": "x"}, "kind is one of"),
        ({"kind": "thread", "text": "A trip."}, "a thread needs when it happens"),
        ({"text": "x", "until": "next week"}, "isn't a date"),
        ({"action": "forget", "id": None}, "this needs the memory's number"),
        ({"action": "recall", "text": "a"}, "recall needs words"),
    ],
)
def test_what_the_model_gets_wrong_is_told_back(memory, args, error):
    assert error in told(memory, **args)


def test_only_so_many_are_told_and_the_rest_are_recalled(memory, now):
    for i in range(MOST_TOLD + 3):
        now[0] += 1
        told(memory, text=f"Fact number {i}.")
    note = memory.note("Alon")
    assert "Fact number 62." in note and "Fact number 0." not in note  # newest first
    assert "(3 more of theirs: the memory tool's recall finds them.)" in note
    assert told(memory, action="recall", text="fact number 0").startswith("- #1 Fact number 0.")


def test_preferences_become_likes_when_the_database_is_upgraded(tmp_path):
    db = tmp_path / "db"
    Store(db)  # the latest version, then put back one step, with a preference kept the old way
    path = next(db.glob("*.db")) if db.is_dir() else db
    with sqlite3.connect(path) as raw:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
        raw.execute("DROP TABLE memories")
        raw.execute("DROP TABLE looked_back")
        raw.execute(
            "CREATE TABLE preferences (id INTEGER PRIMARY KEY AUTOINCREMENT, person TEXT NOT NULL, "
            "about TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, guessed INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL)"
        )
        raw.execute(
            "INSERT INTO preferences (person, about, text, guessed, at) VALUES ('alon', 'news', 'No esports.', 1, ?)",
            (NOW,),
        )
        raw.execute(f"PRAGMA user_version = {version - 1}")
    memory = Memory(Store(db), clock=lambda: NOW)
    assert "- #1 (news) No esports. [a guess 2026-11-19]" in memory.note("Alon")


def test_every_request_is_told_what_tars_knows_of_whoever_is_asking_and_has_the_tool(memory):
    told(memory, kind="style", text="Calls the nursery Ari's room.")
    told(memory, speaker="Stacey", text="Vegetarian.")
    client = fake_openai_chat(lambda m: "Done.")
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False), memory=memory)
    "".join(brain.stream_reply("turn on the light in Ari's room", Request(speaker="Alon")))
    request = client.requests[-1]
    assert any(t.get("name") == "memory" for t in request["tools"])
    assert "Calls the nursery Ari's room." in request["instructions"] and "Vegetarian" not in request["instructions"]
    assert "never 'as you told me'" in request["instructions"]  # used, never recited, whichever model answers


def test_a_note_that_cant_be_read_never_costs_a_reply(memory, capsys):
    broken = types.SimpleNamespace(note=lambda *a, **k: 1 / 0, toolset=memory.toolset)
    client = fake_openai_chat(lambda m: "Fine.")
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False), memory=broken)
    assert "".join(brain.stream_reply("hi", Request(speaker="Alon"))) == "Fine."
    assert "couldn't read what TARS knows" in capsys.readouterr().out


def test_the_quick_model_is_told_how_to_use_it_too(memory):
    from voice_assistant.llm import QuickChat, QuickService

    from .test_llm import fake_cerebras

    told(memory, kind="style", text="Calls the nursery Ari's room.")
    cerebras = fake_cerebras(lambda m: "Done.")
    quick = [QuickService("Cerebras", cerebras, "qwen")]
    brain = QuickChat(fake_openai_chat(lambda m: "From OpenAI."), LLMConfig(), quick, memory=memory)
    "".join(brain.stream_reply("lights in Ari's room", Request(speaker="Alon")))
    system = cerebras.requests[0]["messages"][0]["content"]
    assert "Calls the nursery Ari's room." in system and "never 'as you told me'" in system
