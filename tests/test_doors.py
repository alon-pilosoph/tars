"""Doors whose locks say who opened them (doors.py): a Nuki's lockActionEvent, who's home, and keys learned."""

import pytest

from voice_assistant.config import PresenceConfig, _presence_problems
from voice_assistant.doors import LEARN_WINDOW_S, DoorEvent, DoorLog, Doors, parse
from voice_assistant.presence import Board, People, RouterStatus, State, Tracker, Watcher

NOW = 1_800_000_000.0
MIN = 60.0
PEOPLE = People({"Alon": ["ca:fe:00:00:00:01", "key:111"], "Stacey": ["92:e9:e8:00:00:02"]})


def event(payload: str, at: float = NOW, door: str = "front door") -> DoorEvent:
    e = parse(door, payload, at)
    assert e is not None
    return e


@pytest.mark.parametrize(
    "payload, opens, outside, words",
    [
        ("1,0,111,0,1", True, True, "unlocked by Auto Unlock"),
        ("3,0,111,0,1", True, True, "unlatched by Auto Unlock"),
        ("1,0,111,0,0", True, False, "unlocked"),  # from the app: that can be done from anywhere
        ("1,255,0,42,0", True, True, "unlocked with the keypad"),
        ("1,2,0,0,1", True, False, "unlocked"),  # its button, pressed once: from inside
        ("2,6,0,0,0", False, False, "locked"),  # auto lock
        ("1,172,0,0,0", True, False, "unlocked"),  # over MQTT: TARS or Home Assistant itself
    ],
)
def test_a_lock_action_says_what_was_done_and_whether_from_outside(payload, opens, outside, words):
    e = event(payload)
    assert (e.opens, e.from_outside, e.words()) == (opens, outside, words)


def test_the_key_is_the_keypad_code_when_there_is_one_and_the_app_otherwise():
    assert event("1,255,0,42,0").key == "code:42" and event("1,0,111,0,1").key == "key:111"


@pytest.mark.parametrize("payload", ["", "locked", "1,0,abc,0,1", "1,0"])
def test_anything_that_isnt_a_lock_action_is_ignored(payload):
    assert parse("front door", payload, NOW) is None


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def watching(log, clock, changes, **known):
    board = Board(log.store, PEOPLE, clock=clock)
    tracker = Tracker(PEOPLE.names, 10 * MIN, 0.0, {p: State(*s) for p, s in known.items()})
    return Watcher([], PEOPLE, tracker, board, 30, lambda *c: changes.append(c), clock, lambda: clock() - NOW)


def test_someone_out_who_opens_the_door_from_outside_is_home_at_once_and_said_to_come_by_it(log, capsys):
    clock, changes = Clock(), []
    w = watching(log, clock, changes, alon=(False, NOW - 60 * MIN), stacey=(True, NOW - 60 * MIN))
    doors = Doors(DoorLog(log.store, PEOPLE), w, clock=clock)
    doors.heard(event("1,0,111,0,1"))
    assert changes == [("Alon", True)]
    state = w.board.states()["alon"]
    assert state.home and state.since == NOW and state.via == "front door"
    assert "Alon: home (since" in w.board.summary() and "let in by the front door)" in w.board.summary()
    out = capsys.readouterr().out
    assert "(doors: the front door unlocked by Auto Unlock, by Alon)" in out and "came home, by the front door" in out


def test_opening_from_inside_or_from_the_app_or_when_already_home_changes_nobody(log):
    clock, changes = Clock(), []
    w = watching(log, clock, changes, alon=(False, NOW - 60 * MIN), stacey=(True, NOW - 60 * MIN))
    doors = Doors(DoorLog(log.store, PEOPLE), w, clock=clock)
    doors.heard(event("1,0,111,0,0"))  # the app, maybe from work
    doors.heard(event("1,2,111,0,1"))  # the button
    assert changes == [] and w.tracker.states["alon"].home is False
    doors.heard(event("1,0,111,0,1"))
    doors.heard(event("1,0,111,0,1"))
    assert changes == [("Alon", True)]


def test_every_lock_action_is_kept_with_whose_key_it_was(log):
    doors = Doors(DoorLog(log.store, PEOPLE), None)
    doors.heard(event("1,0,111,0,1"))
    doors.heard(event("2,6,0,0,0", at=NOW + 30))
    rows = log.store.rows("SELECT action, trigger, key, auto_unlock, person FROM door_events ORDER BY at")
    assert rows == [
        {"action": 1, "trigger": 0, "key": "key:111", "auto_unlock": 1, "person": "alon"},
        {"action": 2, "trigger": 6, "key": "key:0", "auto_unlock": 0, "person": None},
    ]


def test_a_phone_coming_home_after_the_door_is_still_home_by_the_door(log):
    clock, changes = Clock(), []
    w = watching(log, clock, changes, alon=(False, NOW - 60 * MIN), stacey=(True, NOW - 60 * MIN))
    Doors(DoorLog(log.store, PEOPLE), w, clock=clock).heard(event("1,0,111,0,1"))
    assert w.tracker.update({"alon", "stacey"}, True, 1.0, NOW + 1) == []
    assert w.tracker.states["alon"].via == "front door"


def test_coming_home_by_phone_alone_isnt_by_a_door(log):
    t = Tracker(PEOPLE.names, 10 * MIN, 0.0, {"alon": State(False, NOW - 60 * MIN, None, "front door")})
    assert t.update({"alon"}, True, 1.0, NOW) == [("alon", True)]
    assert t.states["alon"].via is None


def learning(log, clock, told):
    return Doors(DoorLog(log.store, PEOPLE), None, told.append, clock=clock)


def test_a_key_that_opens_the_door_as_one_persons_phone_comes_home_twice_is_learned_as_theirs(log):
    clock, told = Clock(), []
    doors = learning(log, clock, told)
    for day in range(2):
        start = NOW + day * 86400
        clock.t = start
        doors.heard(event("1,0,222,0,1", at=start))
        clock.t = start + 2 * MIN
        doors.phone_came_home("Stacey", True)
        clock.t = start + LEARN_WINDOW_S + 1
        doors.settle()
    assert doors.log.who("key:222") == "stacey"
    said = "I can tell when Stacey opens the front door now: the lock's record of it matched Stacey's phone coming"
    assert told == [f"{said} home, twice."]


def test_a_key_seen_with_different_people_or_two_at_once_is_never_learned(log):
    clock, told = Clock(), []
    doors = learning(log, clock, told)
    for day, who in enumerate([["Stacey"], ["Alon", "Stacey"], ["Alon"], ["Stacey"]]):
        start = NOW + day * 86400
        clock.t = start
        doors.heard(event("1,0,333,0,1", at=start))
        for name in who:
            doors.phone_came_home(name, True)
        clock.t = start + LEARN_WINDOW_S + 1
        doors.settle()
    assert doors.log.who("key:333") is None and told == []


def test_a_phone_coming_home_long_after_the_door_isnt_taken_for_whose_key_it_was(log):
    clock, told = Clock(), []
    doors = learning(log, clock, told)
    for day in range(3):
        start = NOW + day * 86400
        clock.t = start
        doors.heard(event("1,0,444,0,1", at=start))
        clock.t = start + LEARN_WINDOW_S + 5 * MIN
        doors.phone_came_home("Stacey", True)
        doors.settle()
    assert doors.log.who("key:444") is None


def test_a_key_named_in_the_settings_needs_no_learning_and_a_learned_one_survives_a_restart(log):
    clock, told = Clock(), []
    doors = learning(log, clock, told)
    for day in range(2):
        clock.t = NOW + day * 86400
        doors.heard(event("1,0,222,0,1", at=clock.t))
        doors.phone_came_home("Stacey", True)
        clock.t += LEARN_WINDOW_S + 1
        doors.settle()
    assert DoorLog(log.store, PEOPLE).who("key:222") == "stacey" and DoorLog(log.store, PEOPLE).who("key:111") == "alon"


def test_the_doors_settings_are_checked():
    ok = PresenceConfig(
        people={"Alon": ["key:111", "code:7", "ca:fe:00:00:00:01"]},
        doors=[{"name": "front door", "topic": "nuki/4224E764"}],
        mqtt={"address": "localhost", "port": 1883, "user": "tars", "secret": "MQTT_PASSWORD"},
    )
    assert _presence_problems(ok) == []
    bad = PresenceConfig(
        people={"Alon": ["key:abc"]},
        doors=[{"name": "front door", "topic": "nuki/#"}, {"name": "Front Door", "topic": "x", "pin": 1}],
        mqtt={"port": "1883", "password": "x"},
    )
    problems = _presence_problems(bad)
    assert any("'key:abc' isn't a hardware address" in p for p in problems)
    assert any("#1 needs topic" in p for p in problems) and any("two doors are called" in p for p in problems)
    assert any("#2 has no setting pin" in p for p in problems) and any(
        "need [presence.mqtt] address" in p for p in problems
    )
    assert any("port must be" in p for p in problems) and any(
        "[presence.mqtt] has no setting password" in p for p in problems
    )


def test_the_board_keeps_how_someone_came_home(log):
    board = Board(log.store, PEOPLE, clock=lambda: NOW)
    board.write(
        {"alon": State(True, NOW, NOW, "front door")}, [RouterStatus("192.168.1.1", "technicolor", NOW, 2)], NOW
    )
    assert board.states()["alon"].via == "front door" and board.states()["stacey"].via is None


def test_listening_subscribes_to_each_doors_lock_and_hands_on_what_it_says(monkeypatch):
    paho = pytest.importorskip("paho.mqtt.client")
    made = []

    class FakeClient:
        def __init__(self, *args, **kw):
            self.subscribed, self.auth = [], None
            made.append(self)

        def username_pw_set(self, user, password):
            self.auth = (user, password)

        def subscribe(self, topic, qos=0):
            self.subscribed.append(topic)

        def reconnect_delay_set(self, *args):
            pass

        def connect_async(self, address, port):
            self.at = (address, port)

        def loop_start(self):
            pass

    monkeypatch.setattr(paho, "Client", FakeClient)
    from voice_assistant.doors import listen

    heard = []
    mqtt = {"address": "localhost", "user": "tars", "secret": "MQTT_PASSWORD"}
    client = listen(mqtt, "hunter2", [{"name": "front door", "topic": "nuki/4224E764/"}], heard.append)
    ok = type("Reason", (), {"is_failure": False})()
    client.on_connect(client, None, None, ok)
    msg = type("Msg", (), {"topic": "nuki/4224E764/lockActionEvent", "payload": b"1,0,111,0,1"})()
    client.on_message(client, None, msg)
    client.on_message(client, None, type("Msg", (), {"topic": "nuki/4224E764/state", "payload": b"1"})())
    assert client.subscribed == ["nuki/4224E764/lockActionEvent"] and client.auth == ("tars", "hunter2")
    assert client.at == ("localhost", 1883) and [(e.door, e.key, e.from_outside) for e in heard] == [
        ("front door", "key:111", True)
    ]
