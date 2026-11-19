"""TARS looking back on each conversation for what to remember (reflect.py): only what was said to it, by people it
knows, checked change by change, and each conversation once, or again for what's new."""

import json
from datetime import datetime

import pytest

from voice_assistant.memory import Memory
from voice_assistant.reflect import MOST_CHANGES, MOST_TRIES, LookBack

NOW = datetime(2026, 11, 19, 20, 0).astimezone().timestamp()
QUIET = 600.0


class Model:
    """The look back's model: answers with the next of `answers` (a list of changes, or an exception), and keeps
    what it was asked."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    @property
    def conversations(self) -> list[str]:
        """What it was asked of single conversations, not of a whole picture."""
        return [a for a in self.asked if "The conversation, " in a]

    def __call__(self, instructions: str, text: str) -> str:
        self.asked.append(text)
        answer = self.answers.pop(0) if self.answers else []
        if isinstance(answer, Exception):
            raise answer
        return json.dumps({"changes": answer})


def change(**fields) -> dict:
    return {
        "do": "remember",
        "whose": "Alon",
        "kind": "fact",
        "about": None,
        "text": None,
        "said": True,
        "due": None,
        "until": None,
        "id": None,
        "turn": None,
    } | fields


@pytest.fixture
def now():
    return [NOW]


@pytest.fixture
def memory(log, now):
    return Memory(log.store, clock=lambda: now[0])


def look_back(log, memory, model, now) -> LookBack:
    return LookBack(log.store, memory, model, QUIET, clock=lambda: now[0])


def chat(convos, *turns, via="telegram", at=NOW - 3600):
    """A conversation: (who, text) turns, a minute apart; who is "TARS", a name, or None for a voice nobody knew.
    The ids of its turns, in order."""
    conversation = convos.start(None, ts=at, via=via)
    ids = []
    for i, (who, text) in enumerate(turns):
        ts = at + 60 * i
        if who == "TARS":
            ids.append(convos.add_tars_turn(conversation, text, ts=ts))
        else:
            ids.append(convos.add_person_turn(conversation, text, speaker=who, ts=ts))
    return conversation, ids


def test_what_they_said_is_kept_as_said_and_what_was_worked_out_as_noticed(log, convos, memory, now):
    _, (asked, _) = chat(convos, ("Alon", "My interview at Wix is on Thursday at ten."), ("TARS", "Good luck."))
    model = Model(
        [
            change(kind="thread", text="Job interview at Wix.", about="interview", due="2026-11-20 10:00", turn=asked),
            change(kind="style", text="Likes short answers.", said=False, turn=asked),
            change(text="Is looking for a new job.", turn=None),  # said, but from no turn of theirs: a guess
        ]
    )
    done = look_back(log, memory, model, now).look_back(1)
    assert [d.split(" (")[0] for d in done] == ["done: remembered"] * 3
    note = memory.note("Alon")
    assert "Job interview at Wix. [said 2026-11-19, happens 2026-11-20 10:00]" in note
    assert "Likes short answers. [a guess 2026-11-19]" in note
    assert "Is looking for a new job. [a guess 2026-11-19]" in note
    row = log.store.row("SELECT * FROM memories WHERE text LIKE 'Job interview%'")
    assert (row["source"], row["turn_id"], row["private"]) == ("look back", asked, 0)


def test_only_what_was_said_to_tars_is_read_and_a_voice_nobody_knew_is_only_context(log, convos, memory, now):
    conversation, (_, overheard, _) = chat(
        convos,
        ("Alon", "Turn on the kettle."),
        ("Stacey", "We're getting divorced."),  # said to someone else in the room
        (None, "The code is 4471."),
        via=None,
    )
    convos.mark_not_for_tars(overheard)
    model = Model([change(whose="Stacey", text="Getting divorced.")])
    assert look_back(log, memory, model, now).look_back(conversation) == ["skipped: 'Stacey' didn't speak in it"]
    (asked,) = model.asked
    assert "divorced" not in asked and "Turn on the kettle." in asked
    assert "someone TARS couldn't tell: The code is 4471." in asked
    assert memory.note("Stacey") == ""


def test_the_household_s_and_corrections_and_threads_asked_about(log, convos, memory, now):
    memory.remember("Alon", {"kind": "fact", "text": "The car is a 2019 Corolla.", "household": True}, "chat")
    memory.remember("Alon", {"kind": "thread", "text": "Interview at Wix.", "due": "2026-11-19 10:00"}, "chat")
    _, (sold, _, went) = chat(
        convos,
        ("Alon", "We sold the Corolla, the new car is an Ioniq 5."),
        ("TARS", "How did the interview go?"),
        ("Alon", "Great, second round on the 27th."),
    )
    model = Model(
        [
            change(do="correct", whose="household", text="The car is a 2024 Ioniq 5.", id=1, turn=sold),
            change(do="asked", id=2),
            change(do="retire", id=2),
            change(kind="thread", text="Second-round interview at Wix.", due="2026-11-27", turn=went),
        ]
    )
    look_back(log, memory, model, now).look_back(1)
    note = memory.note("Alon")
    assert "Ioniq" in note and "Corolla" not in note and "Interview at Wix." not in note
    assert "Second-round interview at Wix. [said 2026-11-19, happens 2026-11-27]" in note
    assert "#1 The car is a 2019 Corolla." in model.asked[0] and "Interview at Wix." in model.asked[0]


def test_nothing_is_changed_for_someone_else_or_past_what_one_conversation_may(log, convos, memory, now):
    stacey = memory.remember("Stacey", {"kind": "fact", "text": "Allergic to cats."}, "chat")
    _, (said,) = chat(convos, ("Alon", "I'm learning the cello."))
    model = Model(
        [change(do="retire", id=int(stacey.split("#")[1][:-1]))]
        + [change(text=f"Note {i}.", turn=said) for i in range(MOST_CHANGES + 2)]
    )
    done = look_back(log, memory, model, now).look_back(1)
    assert done[0].startswith("error: there's no memory") and len(done) == MOST_CHANGES
    assert "Allergic to cats." in memory.note("Stacey")


def test_a_conversation_is_looked_back_on_once_quiet_and_again_for_what_is_new(log, convos, memory, now):
    conversation, _ = chat(convos, ("Alon", "I'm learning the cello."), at=NOW - 60)
    model = Model([], [])
    looking = look_back(log, memory, model, now)
    assert looking.due() == []  # not quiet yet
    now[0] += QUIET
    looking.tick()
    assert len(model.conversations) == 1 and looking.due() == []
    convos.add_tars_turn(conversation, "Nice.", ts=now[0])
    now[0] += QUIET + 1
    assert looking.due() == []  # only TARS said more: nothing new of theirs
    convos.add_person_turn(conversation, "My teacher is called Dana.", speaker="Alon", ts=now[0])
    now[0] += QUIET + 1
    looking.tick()
    assert len(model.conversations) == 2 and looking.due() == []


def test_a_conversation_with_nobody_tars_knows_is_never_asked_about(log, convos, memory, now):
    chat(convos, (None, "What's the time?"), ("TARS", "Nine."), via=None)
    model = Model()
    look_back(log, memory, model, now).tick()
    assert model.asked == []


def test_a_look_back_that_fails_is_tried_again_and_then_left(log, convos, memory, now, capsys):
    chat(convos, ("Alon", "I'm learning the cello."))
    model = Model(*[RuntimeError("down")] * MOST_TRIES)
    looking = look_back(log, memory, model, now)
    for _ in range(MOST_TRIES + 2):
        looking.tick()
    assert len(model.conversations) == MOST_TRIES and looking.due() == []
    assert "couldn't look back on conversation 1: RuntimeError('down')" in capsys.readouterr().out


def test_what_the_model_gets_wrong_is_skipped_and_the_rest_kept(log, convos, memory, now):
    _, (said,) = chat(convos, ("Alon", "My sister Maya lives in Haifa."))
    model = Model(
        [
            change(kind="gossip", text="x", turn=said),
            change(do="dance", text="x"),
            change(kind="person", text="Maya: his sister, lives in Haifa.", about="maya", turn=said),
        ]
    )
    done = look_back(log, memory, model, now).look_back(1)
    assert done[0] == "skipped: kind is one of style, like, fact, person, thread"
    assert done[1] == "skipped: 'dance' isn't a change"
    assert "Maya: his sister, lives in Haifa." in memory.note("Alon")


def test_a_memory_is_dated_when_it_was_said_not_when_it_was_looked_back_on(log, convos, memory, now):
    _, (said,) = chat(convos, ("Alon", "We got a new car, an Ioniq 5."), at=NOW - 10 * 24 * 3600)
    look_back(log, memory, Model([change(text="Drives a 2024 Ioniq 5.", turn=said)]), now).look_back(1)
    assert "Drives a 2024 Ioniq 5. [said 2026-11-09]" in memory.note("Alon")


# The nightly look at each person's whole picture.


def test_once_a_night_each_person_s_weeks_are_read_for_what_no_one_conversation_shows(log, convos, memory, now):
    days = []
    for n in range(4):  # "the news" is Channel 12, said in four conversations over four days
        _, (ask, _) = chat(convos, ("Alon", "Put the news on. Channel 12"), ("TARS", "On."), at=NOW - (4 - n) * 86400)
        days.append(ask)
    _, (overheard,) = chat(convos, ("Stacey", "Don't tell TARS about the party."), via=None, at=NOW - 86400)
    convos.mark_not_for_tars(overheard)
    chat(convos, ("Stacey", "Boiler on."), at=NOW - 40 * 86400)  # too long ago to count
    model = Model([change(kind="style", text="'The news' means Channel 12.", said=False, turn=days[-1])])
    looking = look_back(log, memory, model, now)
    now[0] = datetime(2026, 11, 20, 3, 0).astimezone().timestamp()
    looking.nightly()
    assert model.asked == []  # not before WHOLE_HOUR
    now[0] = datetime(2026, 11, 20, 4, 30).astimezone().timestamp()
    looking.nightly()
    looking.nightly()  # once a day
    (alon,) = model.asked  # Alon's alone: nothing of Stacey's said to TARS in the last weeks
    alon = [alon]
    assert len(alon) == 1 and alon[0].count("Put the news on. Channel 12") == 4
    assert all("party" not in a for a in model.asked)
    assert "'The news' means Channel 12. [a guess 2026-11-18]" in memory.note("Alon")  # when it was last said


def test_the_whole_picture_can_merge_two_into_one(log, convos, memory, now):
    memory.remember("Alon", {"kind": "like", "text": "Likes jazz."}, "chat")
    memory.remember("Alon", {"kind": "like", "text": "Listens to jazz in the evenings."}, "chat")
    _, (said,) = chat(convos, ("Alon", "Play some jazz, like every evening."))
    model = Model(
        [
            change(do="correct", kind="like", text="Likes jazz, in the evenings.", id=1, turn=said),
            change(do="retire", id=2),
        ]
    )
    done = look_back(log, memory, model, now).whole_picture("Alon")
    assert done[0].startswith("done: #1 is now #3") and done[1] == "done: #2 is history"
    note = memory.note("Alon")
    assert "Likes jazz, in the evenings." in note and "Listens to jazz" not in note
