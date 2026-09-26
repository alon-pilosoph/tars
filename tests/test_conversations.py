"""The conversation log and its web API."""

import pytest

from voice_assistant.conversations import FILE, HOUSEHOLD, LINK, LIST, NOTE, SentItem

from .conftest import AUDIO


def a_conversation(log, convos):
    """hey TARS → a request with a follow-up, one aside, and a list sent to the household."""
    wake = log.add_wake(AUDIO, 0.9, "answer", "hey tars", 0.95)
    c = convos.start(wake)
    convos.add_person_turn(c, "what's for dinner", AUDIO, "alon", 0.8)
    t = convos.add_tars_turn(c, "Pasta. I sent you the shopping list.")
    convos.add_item(c, t, SentItem(LIST, "Shopping", scope=HOUSEHOLD, entries=["pasta", "tomatoes"]))
    aside = convos.add_person_turn(c, "did you feed the cat", AUDIO, "stacey", 0.7)
    convos.mark_not_for_tars(aside)
    convos.end(c)
    return c, wake


def test_a_conversation_reads_back_as_a_thread(log, convos):
    c, wake = a_conversation(log, convos)
    conv = convos.get(c)
    assert [t["role"] for t in conv["turns"]] == ["person", "tars", "person"]
    assert conv["turns"][2]["not_for_tars"] and conv["turns"][0]["has_audio"]
    assert conv["wake"] == {"event_id": wake, "heard": "hey tars", "confidence": 0.95, "outcome": "answer"}
    assert conv["speaker"] == {"cluster_id": None, "name": "Alon"}
    (item,) = conv["turns"][1]["items"]
    assert item["entries"] == [{"text": "pasta", "done": False}, {"text": "tomatoes", "done": False}]
    assert item["for"] is None and item["scope"] == HOUSEHOLD
    (summary,) = convos.conversations()
    assert summary["preview"] == "what's for dinner" and summary["turn_count"] == 3
    assert summary["item_count"] == summary["unseen_count"] == 1
    assert {k: v for k, v in summary.items() if k != "turns"} == {
        k: v for k, v in conv.items() if k not in ("turns", "items")
    }


def test_speaker_id_guesses_resolve_to_named_voices(log, convos):
    c, _ = a_conversation(log, convos)
    alon = log.new_cluster("Alon", "person")
    assert convos.get(c)["turns"][0]["speaker"] == {"cluster_id": alon, "name": "Alon"}


def test_a_persons_correction_of_the_voice_wins_for_the_whole_conversation(log, convos):
    c, wake = a_conversation(log, convos)
    stacey = log.new_cluster("Stacey", "person")
    log.assign(wake, stacey, pinned=True)
    for conv in (convos.get(c), convos.conversations()[0]):
        assert conv["speaker"] == {"cluster_id": stacey, "name": "Stacey"}
        assert conv["turns"][0]["speaker"]["name"] == "Stacey"  # the request that came with the wake follows it
    assert convos.conversations(turns=False)[0]["speaker"]["name"] == "Stacey"


def test_turns_heard_as_someone_else_keep_their_own_voice(log, convos):
    c, wake = a_conversation(log, convos)
    log.new_cluster("Stacey", "person")
    log.assign(wake, log.new_cluster("Guest", "person"), pinned=True)
    first, _, aside = convos.get(c)["turns"]
    assert first["speaker"]["name"] == "Guest" and aside["speaker"]["name"] == "Stacey"
    later = convos.add_person_turn(c, "and a salad", AUDIO, "alon", 0.8)  # heard the same way as the first turn
    assert next(t for t in convos.get(c)["turns"] if t["id"] == later)["speaker"]["name"] == "Guest"


def test_things_sent_for_whoever_asked_follow_the_corrected_voice(log, convos):
    c, wake = a_conversation(log, convos)
    log.assign(wake, log.new_cluster("Guest", "person"), pinned=True)
    t = convos.get(c)["turns"][1]["id"]
    recipe = convos.add_item(c, t, SentItem(NOTE, "Recipe", body="Boil water."), for_name="alon")
    assert convos.item(recipe)["for"]["name"] == "Guest"
    assert next(i for i in convos.items() if i["id"] == recipe)["for"]["name"] == "Guest"
    assert [i["id"] for i in convos.items(person="guest") if i["id"] == recipe] == [recipe]
    assert [i["id"] for i in convos.items(person="Alon") if i["id"] == recipe] == []


def test_automatic_clustering_never_overrides_what_speaker_id_heard(log, convos):
    c, wake = a_conversation(log, convos)
    log.assign(wake, log.new_cluster(), pinned=False)  # re-clustering put it in an unnamed voice
    conv = convos.get(c)
    assert conv["speaker"] == {"cluster_id": None, "name": "Alon"} and conv["turns"][0]["speaker"]["name"] == "Alon"


def test_an_unrecognized_voice_shows_its_cluster(log, convos):
    wake = log.add_wake(AUDIO, 0.9, "answer", "hey tars", 0.95)
    c = convos.start(wake)
    convos.add_person_turn(c, "is it going to rain", AUDIO, None, None)
    voice = log.new_cluster()
    log.assign(wake, voice, pinned=False)
    assert convos.get(c)["speaker"] == {"cluster_id": voice, "name": None}


def test_items_filter_by_person_and_unseen(convos):
    c = convos.start(None)
    t = convos.add_tars_turn(c, "Sent.")
    mine = convos.add_item(
        c, t, SentItem(LINK, "Recipe", url="https://example.com/r", site="example.com"), for_name="alon"
    )
    house = convos.add_item(c, t, SentItem(NOTE, "Wifi", scope=HOUSEHOLD, body="**password** on the router"))
    theirs = convos.add_item(c, t, SentItem(NOTE, "Gift ideas", body="- a scarf"), for_name="Stacey")
    assert {i["id"] for i in convos.items(person="Alon")} == {mine, house}
    assert {i["id"] for i in convos.items(person=" stacey ")} == {theirs, house}
    convos.mark_seen(house)
    assert {i["id"] for i in convos.items(unseen=True)} == {mine, theirs} and convos.unseen_count() == 2


def test_lists_tick_and_mark_the_list_seen(convos):
    c = convos.start(None)
    lst = convos.add_item(c, convos.add_tars_turn(c, "Sent."), SentItem(LIST, "Packing", entries=["socks", "charger"]))
    convos.tick(lst, 1, True)
    assert convos.item(lst)["entries"][1]["done"] is True and convos.item(lst)["seen"]
    with pytest.raises(IndexError):
        convos.tick(lst, 5, True)


def test_files_are_stored_under_unique_names_and_keep_their_own(convos, log):
    c = convos.start(None)
    t = convos.add_tars_turn(c, "Sent.")
    one, two = (
        convos.add_item(c, t, SentItem(FILE, "Plan", file_bytes=text, file_name="trip plan.txt", mime="text/plain"))
        for text in (b"day 1: fly", b"day 2: swim")
    )
    item = convos.item(one)
    assert item["name"] == "trip plan.txt" and item["size"] == 10 and "file" not in item
    assert (log.folder / convos.item_file(one)).read_bytes() == b"day 1: fly"
    assert (log.folder / convos.item_file(two)).read_bytes() == b"day 2: swim"


def test_deleting_a_conversation_removes_everything(log, convos):
    c, wake = a_conversation(log, convos)
    t = convos.get(c)["turns"][1]["id"]
    f = convos.add_item(c, t, SentItem(FILE, "Notes", file_bytes=b"x", file_name="n.txt", mime="text/plain"))
    files = [log.folder / convos.item_file(f)] + [
        log.folder / convos.turn(x["id"])["audio"] for x in convos.get(c)["turns"] if x["role"] == "person"
    ]
    convos.delete(c)
    assert convos.get(c) is None and convos.items() == [] and log.get(wake) is None
    assert not any(p.exists() for p in files)


def test_deleting_a_wake_keeps_its_conversation(log, convos):
    c, wake = a_conversation(log, convos)
    log.delete(wake)
    assert convos.get(c)["wake"] is None
    convos.delete(c)  # and deleting it later deletes nothing else
    assert log.get(log.add_wake(AUDIO, 0.9, "answer", "", None))


def test_nothing_is_added_to_a_deleted_conversation(convos):
    c = convos.start(None)
    convos.delete(c)
    assert convos.add_person_turn(c, "hello?", AUDIO) is None and convos.add_tars_turn(c, "Hi.") is None
    assert convos.store.rows("SELECT * FROM turns") == []


# ---------- the web API ----------


def test_the_api_lists_conversations_with_or_without_turns(log, convos, client):
    c, _ = a_conversation(log, convos)
    (listed,) = client.get("/api/conversations").json()
    assert [t["text"] for t in listed["turns"]] == [
        "what's for dinner",
        "Pasta. I sent you the shopping list.",
        "did you feed the cat",
    ]
    assert listed["turns"][1]["items"][0]["kind"] == "list"
    assert "turns" not in client.get("/api/conversations?turns=false").json()[0]
    assert client.get(f"/api/conversations/{c}").json()["id"] == c
    assert client.get("/api/conversations?limit=0").status_code == 422


def test_only_what_a_person_said_has_audio(log, convos, client):
    c, _ = a_conversation(log, convos)
    person, tars, _ = client.get(f"/api/conversations/{c}").json()["turns"]
    assert client.get(f"/api/audio/turn/{person['id']}").status_code == 200
    assert client.get(f"/api/audio/turn/{tars['id']}").status_code == 404  # TARS's own voice isn't stored


def test_only_tars_replies_can_be_rated(log, convos, client):
    c, _ = a_conversation(log, convos)
    person, tars, _ = client.get(f"/api/conversations/{c}").json()["turns"]
    assert client.post(f"/api/turns/{tars['id']}/rating", json={"rating": "good"}).status_code == 200
    assert client.post(f"/api/turns/{tars['id']}/rating", json={"rating": "meh"}).status_code == 422
    assert client.post(f"/api/turns/{person['id']}/rating", json={"rating": "good"}).status_code == 400
    assert client.get(f"/api/conversations/{c}").json()["turns"][1]["rating"] == "good"


def test_a_correction_back_to_what_tars_heard_clears_it(log, convos, client):
    c, _ = a_conversation(log, convos)
    person = client.get(f"/api/conversations/{c}").json()["turns"][0]
    url = f"/api/turns/{person['id']}/correction"
    assert client.post(url, json={"text": " what's for dinner, TARS "}).status_code == 200
    assert client.get(f"/api/conversations/{c}").json()["turns"][0]["corrected_text"] == "what's for dinner, TARS"
    client.post(url, json={"text": "what's for dinner"})
    assert client.get(f"/api/conversations/{c}").json()["turns"][0]["corrected_text"] is None


def test_list_entries_tick_and_seen_can_be_undone(log, convos, client):
    a_conversation(log, convos)
    (item,) = client.get("/api/items?unseen=1").json()
    assert client.post(f"/api/items/{item['id']}/entries/0", json={"done": True}).status_code == 200
    assert client.post(f"/api/items/{item['id']}/entries/9", json={"done": True}).status_code == 404
    assert client.get("/api/items?unseen=1").json() == [] and client.get("/api/status").json()["unseen_items"] == 0
    client.post(f"/api/items/{item['id']}/seen", json={"seen": False})  # Undo
    assert len(client.get("/api/items?unseen=1").json()) == 1


def test_sent_files_download_and_never_render(log, convos, client):
    c, _ = a_conversation(log, convos)
    t = convos.get(c)["turns"][1]["id"]
    f = convos.add_item(c, t, SentItem(FILE, "Photo", file_bytes=b"\x89PNG", file_name="cat.png", mime="image/png"))
    got = next(i for i in client.get("/api/items").json() if i["id"] == f)
    assert "file" not in got and got["url"] == got["preview"] == f"/api/items/{f}/file"
    r = client.get(got["url"])
    assert r.content == b"\x89PNG" and "attachment" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.delete(f"/api/items/{f}").status_code == 200
    assert client.get(got["url"]).status_code == 404


def test_a_stored_path_outside_the_folder_is_never_served(log, convos, client):
    c = convos.start(None)
    f = convos.add_item(
        c,
        convos.add_tars_turn(c, "Sent."),
        SentItem(FILE, "Notes", file_bytes=b"x", file_name="n.txt", mime="text/plain"),
    )
    convos.store.write("UPDATE items SET file=? WHERE id=?", ("../../../etc/hosts", f))
    assert client.get(f"/api/items/{f}/file").status_code == 404


def test_deleting_a_conversation_through_the_api(log, convos, client):
    c, _ = a_conversation(log, convos)
    assert client.delete(f"/api/conversations/{c}").status_code == 200
    assert client.get(f"/api/conversations/{c}").status_code == 404
    assert client.delete(f"/api/conversations/{c}").status_code == 404
    assert client.get("/api/conversations").json() == []
