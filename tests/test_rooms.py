"""The rooms' desk (rooms.py): where each thing TARS says by itself is said, and that it's said once."""

import threading
import time

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


# Several assistants, one desk


def two_rooms(tmp_path):
    """The living room's assistant (the main room) and the bedroom's, on one desk and one reminders table, with a
    reminder for Stacey due now."""
    from voice_assistant.reminders import MESSAGE, WEB, NewReminder, Reminders, ReminderTools

    from .test_assistant import logged_assistant

    desk = Rooms("living room")
    made = {}
    store = None
    for room in ("living room", "bedroom"):
        assistant, convos = logged_assistant(tmp_path / room, [None], [], (), None)
        assistant.journal.end_conversation()
        store = store or convos.store
        assistant.rooms, assistant.room = desk, room
        desk.add(room)
        assistant.voice.stream = lambda text: iter([text.encode()])
        made[room] = assistant
    tools = ReminderTools(Reminders(store))
    for assistant in made.values():
        assistant.use_reminders(tools)
    rid = tools.reminders.add(NewReminder(MESSAGE, "dinner's at eight", for_name="stacey", from_name="alon",
                                          due=time.time(), needs_ack=False), WEB)  # fmt: skip
    return desk, made, tools.reminders, rid


def say_at_once(made):
    barrier = threading.Barrier(len(made))

    def say(assistant):
        barrier.wait()
        assistant.say_reminders()

    threads = [threading.Thread(target=say, args=(a,)) for a in made.values()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)


def said_in(assistant):
    return [c.decode() for c in assistant.body.played if len(c) < 200]


def test_a_reminder_is_said_once_in_the_room_its_person_was_last_heard(tmp_path):
    desk, made, reminders, rid = two_rooms(tmp_path)
    desk.heard("bedroom", "Stacey")
    say_at_once(made)
    assert said_in(made["bedroom"]) == ["Stacey, a message from Alon: dinner's at eight."]
    assert said_in(made["living room"]) == [] and reminders.get(rid)["tries"] == 1


def test_with_the_bedrooms_body_gone_its_reminder_is_said_in_the_main_room(tmp_path):
    desk, made, _, _ = two_rooms(tmp_path)
    desk.heard("bedroom", "Stacey")
    desk.connected("bedroom", False)
    say_at_once(made)
    assert said_in(made["living room"]) == ["Stacey, a message from Alon: dinner's at eight."]
    assert said_in(made["bedroom"]) == []


def test_only_the_rooms_with_something_due_wake_for_it(tmp_path):
    desk, made, _, _ = two_rooms(tmp_path)
    desk.heard("bedroom", "Stacey")
    for assistant in made.values():
        assistant._reminder_clock.read()
    assert made["bedroom"]._due() and not made["living room"]._due()


def test_a_rooms_conversation_is_kept_with_its_room(log):
    from voice_assistant.journal import Journal

    journal = Journal(log, room="bedroom")
    journal.heard(b"\0\0" * 1600, "what time is it", "stacey", 0.9, None, first=True)
    journal.answered("Nine.", [], asker="stacey")
    journal.flush()
    rows = log.store.rows("SELECT room FROM conversations")
    assert rows == [{"room": "bedroom"}]


def test_the_brain_is_told_the_room_once_there_is_more_than_one(tmp_path):
    from .conftest import make_assistant

    assistant, brain = make_assistant([b"q", None], ["what time is it"])
    assistant.converse(follow_up_s=0.0)
    assert "[Room:" not in brain.asked[0] and brain.requests[0].room == ""  # one room: as ever
    assistant, brain = make_assistant([b"q", None], ["turn off the light"])
    assistant.rooms = Rooms("living room")
    assistant.rooms.add("bedroom")
    assistant.room = "bedroom"
    assistant.converse(follow_up_s=0.0)
    assert brain.asked[0].startswith("[Room: bedroom]") and brain.requests[0].room == "bedroom"
