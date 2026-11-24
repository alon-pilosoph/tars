"""The Memory page: what's shown, put right, forgotten and never learned again."""

import json

import pytest

from voice_assistant.memory import FACT, LIKE, THREAD, Memory, same_note
from voice_assistant.reflect import LookBack


@pytest.fixture
def memory(log):
    return Memory(log.store)


def keep(memory, text, person="Alon", kind=LIKE, **more):
    memory.remember(person, {"kind": kind, "text": text, **more}, "chat")
    return memory.store.row("SELECT MAX(id) AS id FROM memories")["id"]


def texts(page):
    return [m["text"] for m in page["memories"]]


def test_the_page_shows_everyones_but_never_a_private_one_nor_lets_it_be_changed(memory, client):
    keep(memory, "Skip esports.", noticed=True)
    keep(memory, "Bins go out on Tuesday.", household=True)
    secret = keep(memory, "Planning a surprise party.", private=True)
    page = client.get("/api/memories").json()
    assert set(texts(page)) == {"Skip esports.", "Bins go out on Tuesday."}
    assert {m["text"]: m["guessed"] for m in page["memories"]}["Skip esports."] is True
    assert client.post(f"/api/memories/{secret}", json={"text": "x"}).status_code == 404
    assert client.delete(f"/api/memories/{secret}").status_code == 404
    assert client.post(f"/api/memories/{secret}/never").status_code == 404
    assert "surprise" not in json.dumps(client.get("/api/skills").json())


def test_one_put_right_keeps_its_date_and_what_it_was(memory, client):
    one = keep(memory, "Interview.", kind=THREAD, due="2099-01-02")
    assert client.post(f"/api/memories/{one}", json={"text": " Job interview. "}).json() == {"ok": True}
    (m,) = client.get("/api/memories").json()["memories"]
    old = memory.store.row("SELECT * FROM memories WHERE id=?", (one,))
    assert (m["text"], m["due"], m["guessed"], m["source"]) == ("Job interview.", old["due"], False, "web")
    assert [h["text"] for h in m["history"]] == ["Interview."]
    assert client.post(f"/api/memories/{m['id']}", json={"text": " "}).status_code == 400
    assert client.post(f"/api/memories/{one}", json={"text": "Old one."}).status_code == 404  # it's history


def test_forgetting_takes_its_history_and_never_keeps_its_words_until_allowed(memory, client):
    one = keep(memory, "Drives a van.", kind=FACT)
    memory.correct("Alon", {"id": one, "kind": FACT, "text": "Drives a hatchback."}, "chat")
    (m,) = client.get("/api/memories").json()["memories"]
    assert client.post(f"/api/memories/{m['id']}/never").json() == {"ok": True}
    page = client.get("/api/memories").json()
    assert page["memories"] == [] and memory.store.rows("SELECT * FROM memories") == []
    (n,) = page["never"]
    assert n["text"] == "Drives a hatchback."
    assert client.delete(f"/api/never/{n['id']}").json() == {"ok": True}
    assert client.delete(f"/api/never/{n['id']}").status_code == 404
    assert client.get("/api/memories").json()["never"] == []


def test_the_look_back_is_told_and_refused_what_its_never_to_learn(memory):
    memory.drop(keep(memory, "Has stopped drinking coffee."), never=True)
    look = LookBack(memory.store, memory, lambda *a: "", 60)
    assert "- Has stopped drinking coffee." in look._never(["alon"])
    assert look._never(["stacey"]) == ""
    change = {
        "do": "remember",
        "whose": "Alon",
        "kind": FACT,
        "said": True,
        "turn": 1,
        "text": "Stopped drinking coffee.",
    }
    apply = lambda c: look._apply(c, {"alon": "Alon"}, {1: "alon"}, {1: 5.0}, 5.0)
    assert apply(change) == "skipped: never to be learned"
    assert apply({**change, "whose": "household"}) == "skipped: never to be learned"
    assert apply({**change, "text": "Drinks green tea."}).startswith("done: remembered")


def test_they_can_still_ask_tars_to_remember_it(memory):
    memory.drop(keep(memory, "Skip esports."), never=True)
    assert keep(memory, "Skip esports.") and texts(memory.page()) == ["Skip esports."]


def test_same_note():
    assert same_note("Has stopped drinking coffee.", "stopped drinking coffee")
    assert not same_note("Likes coffee.", "Likes jazz.") and not same_note("", "")
