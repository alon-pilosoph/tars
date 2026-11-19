"""The Voices page's people (people.py): people, never anonymous voices; "Who said this?" one request at a time,
with TARS's guess; each answer final; and what a person may have been given by mistake, asked about."""

import time

import numpy as np
import pytest

from voice_assistant.events import NOT_PERSON
from voice_assistant.people import ASK_DAYS, MOST_ASKED, TV, UNSURE, People

from .test_webui_and_clustering import add_requests, voice


@pytest.fixture
def people(log):
    return People(log)


def named(log, people, name, ids):
    person = people.add(name)
    for i in ids:
        log.assign(i, person, pinned=True)
    return person


def test_only_named_people_are_shown_and_what_they_said_on_each_mic(log, people):
    rng = np.random.default_rng(1)
    atom = add_requests(log, voice(np.eye(16)[0], rng, 3), room="Bedroom")
    speakerphone = add_requests(log, voice(np.eye(16)[0], rng, 2))
    alon = named(log, people, "Alon", [*atom, *speakerphone])
    people.add("Stacey")
    add_requests(log, voice(np.eye(16)[5], rng, 2))  # nobody's yet
    page = people.page()
    assert [(p["name"], p["requests"]) for p in page["people"]] == [("Alon", 5), ("Stacey", 0)]
    assert page["people"][0]["mics"] == [{"room": "Bedroom", "requests": 3}, {"room": "home", "requests": 2}]
    assert page["people"][0]["id"] == alon and len(page["people"][0]["shown"]) == 5


def test_who_said_this_asks_about_recent_requests_nobody_answered_with_tars_guess(log, people):
    rng = np.random.default_rng(2)
    alon = named(log, people, "Alon", add_requests(log, voice(np.eye(16)[0], rng, 3)))
    stacey = named(log, people, "Stacey", add_requests(log, voice(np.eye(16)[1], rng, 3)))
    (sounds_like_her,) = add_requests(log, voice(np.eye(16)[1], rng, 1))
    (nobody_knows,) = add_requests(log, voice(np.eye(16)[7], rng, 1))
    cards = {c["event_id"]: c for c in people.page()["ask"]}
    assert set(cards) == {sounds_like_her, nobody_knows}
    assert cards[sounds_like_her]["guess"] == {"id": stacey, "name": "Stacey"}
    assert cards[nobody_knows]["guess"] is None
    assert alon != stacey


def test_each_answer_is_final_and_never_asked_again(log, people):
    rng = np.random.default_rng(3)
    stacey = people.add("Stacey")
    hers, tv, unsure = add_requests(log, voice(np.eye(16)[1], rng, 3))
    people.answer(hers, stacey)
    people.answer(tv, TV)
    people.answer(unsure, UNSURE)
    assert people.page()["ask"] == []
    assert log.get(hers)["cluster_id"] == stacey and log.get(hers)["cluster_pinned"]
    tv_voice = log.get(tv)["cluster_id"]
    assert [c["kind"] for c in log.clusters() if c["id"] == tv_voice] == [NOT_PERSON]
    assert log.get(unsure)["cluster_id"] is None and log.get(unsure)["cluster_pinned"]
    with pytest.raises(ValueError, match="no such person"):
        people.answer(hers, tv_voice)  # the TV's voice isn't a person


def test_only_so_many_and_only_recent_requests_are_asked_about(log, people):
    rng = np.random.default_rng(4)
    add_requests(log, voice(np.eye(16)[3], rng, MOST_ASKED + 4))
    old = log.add_wake(
        np.zeros(16000, np.int16), 0.9, "answer", "hey tars", 0.9, ts=time.time() - (ASK_DAYS + 1) * 86400
    )
    log.add_request(old, np.zeros(16000, np.int16), "old", None, None, np.eye(16)[3].astype(np.float32))
    asked = people.page()["ask"]
    assert len(asked) == MOST_ASKED and old not in {c["event_id"] for c in asked}


def test_a_request_given_to_someone_that_sounds_unlike_the_rest_is_asked_about(log, people):
    rng = np.random.default_rng(5)
    alon = people.add("Alon")
    his = add_requests(log, voice(np.eye(16)[0], rng, 4))
    (hers,) = add_requests(log, voice(np.eye(16)[1], rng, 1))
    for i in [*his, hers]:
        log.assign(i, alon, pinned=False)  # as an old grouping left them, not as anyone said
    (person,) = people.page()["people"]
    assert [c["event_id"] for c in person["check"]] == [hers]
    log.assign(hers, alon, pinned=True)  # "yes, mine": asked no more
    assert people.page()["people"][0]["check"] == []


def test_a_clean_start_sets_aside_everything_nobody_named_for_good(log, people):
    rng = np.random.default_rng(6)
    alon = named(log, people, "Alon", add_requests(log, voice(np.eye(16)[0], rng, 2)))
    old = add_requests(log, voice(np.eye(16)[2], rng, 3))
    unnamed = log.new_cluster()
    for i in old:
        log.assign(i, unnamed, pinned=False)
    assert people.clean_start() == 3
    assert people.page()["ask"] == [] and all(c["id"] != unnamed for c in log.clusters())
    assert people.page()["people"][0]["requests"] == 2 and alon


def test_adding_someone_twice_is_the_same_person(people):
    assert people.add("Stacey") == people.add(" stacey ")
    with pytest.raises(ValueError):
        people.add("  ")


def test_an_answer_relearns_the_voices_on_its_own_thread(log):
    import threading

    done = threading.Event()
    people = People(log, rebuild=done.set)
    (one,) = add_requests(log, voice(np.eye(16)[0], np.random.default_rng(7), 1))
    people.answer(one, people.add("Alon"))
    assert done.wait(5)


def test_the_page_and_its_answers_over_the_api(log, client):
    rng = np.random.default_rng(8)
    (one,) = add_requests(log, voice(np.eye(16)[0], rng, 1))
    stacey = client.post("/api/people", json={"name": "Stacey"}).json()["id"]
    assert [c["event_id"] for c in client.get("/api/people").json()["ask"]] == [one]
    assert client.post("/api/people/answer", json={"event_id": one, "to": stacey}).json() == {"ok": True}
    page = client.get("/api/people").json()
    assert page["ask"] == [] and page["people"][0]["requests"] == 1
    assert client.post("/api/people/answer", json={"event_id": 999, "to": "tv"}).status_code == 404
    assert client.post("/api/people/clean-start").json() == {"set_aside": 0}
