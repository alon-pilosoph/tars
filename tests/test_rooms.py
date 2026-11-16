"""The rooms' desk (rooms.py): where each thing TARS says by itself is said, and that it's said once."""

import threading

import pytest

from voice_assistant.rooms import CLAIM_S, Rooms


def key(item):
    return item["id"]


def person(item):
    return item.get("for")


@pytest.fixture
def desk():
    now = [1000.0]
    rooms = Rooms("living room", recent_s=3600, clock=lambda: now[0])
    rooms.add("bedroom")
    rooms.now = now
    return rooms


def test_one_room_says_everything():
    alone = Rooms("home")
    assert alone.single and alone.room_for("Alon") == "home" and alone.room_for(None) == "home"
    items = [{"id": 1, "for": "alon"}, {"id": 2}]
    assert alone.take("home", items, key, person) == items


def test_a_persons_things_go_where_they_were_last_heard_while_its_recent(desk):
    assert desk.room_for("Stacey") == "living room"  # never heard: the main room
    desk.heard("bedroom", "Stacey")
    assert desk.room_for("stacey") == "bedroom" and desk.room_for(None) == "living room"
    desk.heard("living room", "Stacey")  # moved: only the latest room counts
    assert desk.room_for("Stacey") == "living room"
    desk.heard("bedroom", "Stacey")
    desk.now[0] += 3601
    assert desk.room_for("Stacey") == "living room"  # too long ago: the main room


def test_a_room_whose_body_is_gone_gets_nothing_and_a_muted_one_is_passed_over(desk):
    desk.heard("bedroom", "Stacey")
    desk.connected("bedroom", False)
    assert desk.room_for("Stacey") == "living room"
    desk.connected("bedroom", True)
    desk.muted("bedroom", True)
    assert desk.room_for("Stacey") == "living room"
    desk.muted("living room", True)  # every room muted: still said, as a muted TARS always has
    assert desk.room_for(None) == "living room"
    desk.connected("living room", False)
    assert desk.room_for(None) == "bedroom"  # the main room gone: one that's here
    desk.connected("bedroom", False)
    assert desk.room_for(None) is None


def test_two_rooms_taking_at_once_never_hold_the_same_item(desk):
    """Stacey keeps moving between the rooms while both take and finish her reminders: an item is never held by two
    rooms at once, and every one is taken."""
    items = [{"id": i, "for": "stacey"} for i in range(50)]
    holding, clash, said, lock, stop = {}, [], set(), threading.Lock(), threading.Event()

    def room(name):
        while not stop.is_set():
            for item in desk.take(name, items, key, person):
                with lock:
                    if item["id"] in holding:
                        clash.append(item["id"])
                    holding[item["id"]] = name
                    said.add(item["id"])
                with lock:
                    del holding[item["id"]]
                desk.done(name, [item["id"]])

    def wander():
        for i in range(2000):
            desk.heard("bedroom" if i % 2 else "living room", "Stacey")
        stop.set()

    threads = [threading.Thread(target=room, args=(n,)) for n in ("bedroom", "living room")]
    threads.append(threading.Thread(target=wander))
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert clash == [] and said == {i["id"] for i in items}


def test_what_one_room_took_is_no_other_rooms_until_done(desk):
    item = [{"id": 7, "for": "stacey"}]
    desk.heard("bedroom", "Stacey")
    assert desk.take("bedroom", item, key, person) == item
    desk.heard("living room", "Stacey")  # she moved while the bedroom was saying it
    assert desk.take("living room", item, key, person) == [] and not desk.waiting("living room", item, key, person)
    desk.done("bedroom", [7])
    assert desk.take("living room", item, key, person) == item


def test_a_body_that_goes_before_saying_it_gives_it_back(desk):
    item = [{"id": 9, "for": "stacey"}]
    desk.heard("bedroom", "Stacey")
    assert desk.take("bedroom", item, key, person) == item
    desk.connected("bedroom", False)  # its body went: the item is free, and it's the main room's now
    assert desk.waiting("living room", item, key, person)
    assert desk.take("living room", item, key, person) == item


def test_a_take_never_finished_lapses(desk):
    stuck = [{"id": 4, "for": "stacey"}]
    desk.heard("bedroom", "Stacey")
    assert desk.take("bedroom", stuck, key, person) == stuck  # and the bedroom's assistant stopped, mid-saying
    desk.heard("living room", "Stacey")
    assert desk.take("living room", stuck, key, person) == []
    desk.now[0] += CLAIM_S + 1
    assert desk.take("living room", stuck, key, person) == stuck


def test_the_assistant_tells_the_desk_who_it_heard():
    from voice_assistant.rooms import Rooms

    from .conftest import make_assistant

    assistant, _ = make_assistant([b"q", None], ["what time is it"])
    assistant.rooms = desk = Rooms("home")
    desk.add("bedroom")
    assistant.room = "bedroom"
    assistant._who = lambda identifying: type("W", (), {"name": "Stacey", "score": 0.9, "embedding": None})()
    assistant.converse(follow_up_s=0.0)
    assert desk.room_for("Stacey") == "bedroom"
