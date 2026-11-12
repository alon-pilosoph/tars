"""Doors whose locks say who opened them (doors.py): a Nuki's lockActionEvent, who's home, and keys learned."""

import dataclasses
from pathlib import Path

import pytest

from voice_assistant.config import PresenceConfig, _presence_problems, load_config
from voice_assistant.doors import LEARN_WINDOW_S, TELL_AFTER_S, DoorEvent, DoorLog, Doors, Link, parse
from voice_assistant.presence import Board, People, RouterStatus, State, Tracker, Watcher, make_presence

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


@pytest.mark.parametrize("payload", ["1,2,0,0,1", "1,1,0,0,0", "1,0,555,0,0"])  # its button, its knob, the app
def test_a_door_opened_from_inside_as_someone_comes_home_teaches_nothing(log, payload):
    clock, told = Clock(), []
    doors = learning(log, clock, told)
    for day in range(3):
        clock.t = NOW + day * 86400
        doors.phone_came_home("Alon", True)
        doors.heard(event(payload, at=clock.t + MIN))
        clock.t += LEARN_WINDOW_S + 2 * MIN
        doors.settle()
    assert doors.log.who(event(payload).key) is None and told == []


def test_an_unlock_and_an_unlatch_in_one_homecoming_are_one_arrival_not_two(log):
    clock, told = Clock(), []
    doors = learning(log, clock, told)
    for day in range(2):
        clock.t = NOW + day * 86400
        doors.phone_came_home("Stacey", True)
        doors.heard(event("1,0,999,0,1", at=clock.t + 5))
        doors.heard(event("3,0,999,0,1", at=clock.t + 8))
        clock.t += LEARN_WINDOW_S + MIN
        doors.settle()
        assert (doors.log.who("key:999") is None) == (day == 0)
    assert len(told) == 1


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


@pytest.mark.parametrize("mqtt", [{}, {"user": "tars"}, {"secret": "MQTT_PASSWORD"}])
def test_doors_need_tars_to_sign_in_to_the_broker_as_itself(mqtt):
    p = PresenceConfig(doors=[{"name": "front door", "topic": "nuki/4224E764"}], mqtt={"address": "localhost", **mqtt})
    assert any("need [presence.mqtt] user and secret" in problem for problem in _presence_problems(p))


@pytest.mark.parametrize("user", ["", "tars:x", "a user", 7])
def test_tars_s_user_on_the_broker_is_a_plain_name(user):
    p = PresenceConfig(mqtt={"address": "localhost", "user": user, "secret": "MQTT_PASSWORD"})
    assert any("user must be TARS's name on the broker" in problem for problem in _presence_problems(p))


def test_someone_let_in_by_the_door_counts_as_seen_then(log):
    t = Tracker(PEOPLE.names, 10 * MIN, 0.0, {"alon": State(False, NOW - 86400, NOW - 86400)})
    assert t.arrived("alon", "front door", 1000.0, NOW)
    assert t.update(set(), True, 1000.0 + 10 * MIN + 1, NOW + 10 * MIN + 1) == [("alon", False)]
    assert t.states["alon"].since == NOW  # out since they were last seen: at the door, not yesterday


def test_without_a_router_the_doors_are_only_recorded_and_say_so(log, tmp_path, monkeypatch, capsys):
    from voice_assistant import __main__ as cli
    from voice_assistant import doors

    monkeypatch.setattr(doors, "listen", lambda *a: None)
    monkeypatch.setattr(Doors, "start", lambda self: self)
    presence = dataclasses.replace(
        load_config(Path(__file__).parents[1] / "config.toml", local=False).presence,
        enabled=True,
        people={"Alon": ["key:111"]},
        doors=[{"name": "front door", "topic": "nuki/4224E764"}],
        mqtt={"address": "localhost", "user": "tars"},
    )
    cfg = dataclasses.replace(load_config(Path(__file__).parents[1] / "config.toml", local=False), presence=presence)
    cli.start_presence(cfg, tmp_path, make_presence(presence, log.store, lambda name: None))
    out = capsys.readouterr().out
    assert "(doors: no router to watch, so the doors are only recorded" in out
    assert "(doors: connecting to the MQTT broker at localhost, for the front door)" in out


def test_a_brokers_password_missing_from_env_leaves_the_doors_out(log, tmp_path, monkeypatch, capsys):
    from voice_assistant import __main__ as cli
    from voice_assistant import doors

    listened = []
    monkeypatch.setattr(doors, "listen", lambda *a: listened.append(a))
    cfg = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    presence = dataclasses.replace(
        cfg.presence,
        doors=[{"name": "front door", "topic": "nuki/4224E764"}],
        mqtt={"address": "localhost", "user": "tars", "secret": "MQTT_PASSWORD"},
    )
    (tmp_path / ".env").write_text("OTHER=x\n")
    cli.listen_to_doors(dataclasses.replace(cfg, presence=presence), tmp_path, None)
    assert listened == []
    assert "(doors: leaving out the MQTT broker at localhost: no MQTT_PASSWORD in .env" in capsys.readouterr().out


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

    def msg(topic, payload, retain=False):
        return type("Msg", (), {"topic": topic, "payload": payload, "retain": retain})()

    client.on_message(client, None, msg("nuki/4224E764/lockActionEvent", b"1,0,999,0,1", retain=True))  # old news
    client.on_message(client, None, msg("nuki/4224E764/lockActionEvent", b"1,0,111,0,1"))
    client.on_message(client, None, msg("nuki/4224E764/state", b"1"))
    assert client.subscribed == ["nuki/4224E764/lockActionEvent"] and client.auth == ("tars", "hunter2")
    assert client.at == ("localhost", 1883) and [(e.door, e.key, e.from_outside) for e in heard] == [
        ("front door", "key:111", True)
    ]


class FakeClient:
    """paho's Client, as listen() uses it: what it was given, and its callbacks to call."""

    def __init__(self, *args, **kw):
        self.subscribed, self.auth = [], None

    def username_pw_set(self, user, password):
        self.auth = (user, password)

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    reconnect_delay_set = connect_async = loop_start = lambda self, *a: None


OK = type("Reason", (), {"is_failure": False})()
NOT_AUTHORIZED = type("Reason", (), {"is_failure": True, "__str__": lambda self: "Not authorized"})()


def listening(monkeypatch, told, clock):
    paho = pytest.importorskip("paho.mqtt.client")
    monkeypatch.setattr(paho, "Client", FakeClient)
    from voice_assistant.doors import listen

    link = Link("localhost", "front door", told.append, clock)
    return listen({"address": "localhost"}, None, [{"name": "front door", "topic": "nuki/1"}], print, link), link


def test_losing_the_broker_and_getting_it_back_are_each_said_once(monkeypatch, capsys):
    client, _ = listening(monkeypatch, [], Clock(0.0))
    client.on_connect(client, None, None, OK)
    client.on_disconnect(client, None, None, "unspecified error")
    client.on_connect_fail(client, None)  # each try to reconnect
    client.on_disconnect(client, None, None, "unspecified error")
    client.on_connect_fail(client, None)
    client.on_connect(client, None, None, OK)
    client.on_connect(client, None, None, OK)
    out = capsys.readouterr().out
    assert out.count("(doors: listening to the front door on the MQTT broker at localhost)") == 1
    assert out.count("(doors: lost the MQTT broker at localhost (unspecified error); trying again") == 1
    assert "can't reach" not in out and out.count("(doors: the MQTT broker at localhost is back)") == 1
    assert client.subscribed == ["nuki/1/lockActionEvent"] * 3  # again on each connection


def test_a_broker_down_when_tars_starts_is_said_and_told_once_it_lasts_and_again_when_its_back(monkeypatch, capsys):
    told, clock = [], Clock(0.0)
    client, link = listening(monkeypatch, told, clock)
    for _ in range(3):
        client.on_connect_fail(client, None)  # nothing listening there: paho's on_connect_fail, never on_disconnect
    assert capsys.readouterr().out.count("(doors: can't reach the MQTT broker at localhost; trying again") == 1
    clock.t = TELL_AFTER_S - 1
    link.look()
    assert told == []  # a broker restarting, or the network coming up, tells nobody
    clock.t = TELL_AFTER_S + 60
    link.look()
    link.look()
    assert told == [
        (
            "I can't hear the front door: can't reach the MQTT broker at localhost, for 11 minutes now, so opening it "
            "lets nobody in as home until it's back. Is Mosquitto running? On the Pi: cd deploy/home-assistant && "
            "docker compose --profile mqtt up -d."
        )
    ]
    client.on_connect(client, None, None, OK)
    assert told[1:] == ["I can hear the front door again: the MQTT broker at localhost answers."]
    assert "(doors: listening to the front door on the MQTT broker at localhost)" in capsys.readouterr().out


def test_a_broker_that_refuses_tars_says_to_check_its_password(monkeypatch, capsys):
    told, clock = [], Clock(0.0)
    client, link = listening(monkeypatch, told, clock)
    client.on_connect(client, None, None, NOT_AUTHORIZED)
    client.on_disconnect(client, None, None, "Not authorized")  # paho's, after the refusal: already said
    client.on_connect(client, None, None, NOT_AUTHORIZED)
    out = capsys.readouterr().out
    assert out.count("(doors: the MQTT broker at localhost refused TARS: Not authorized; trying again") == 1
    assert "lost" not in out and client.subscribed == []
    clock.t = TELL_AFTER_S
    link.look()
    assert len(told) == 1 and "refused TARS: Not authorized, for 10 minutes now" in told[0]
    assert "its password in .env" in told[0]


def test_a_short_outage_tells_nobody(monkeypatch):
    told, clock = [], Clock(0.0)
    client, link = listening(monkeypatch, told, clock)
    client.on_connect(client, None, None, OK)
    clock.t = 3600.0
    client.on_disconnect(client, None, None, "keepalive timeout")
    clock.t += TELL_AFTER_S - 1
    link.look()
    client.on_connect(client, None, None, OK)
    clock.t += TELL_AFTER_S
    link.look()
    assert told == [] and link.up


class ProbedClient(FakeClient):
    """A broker for broker_answers: lets TARS in or not, and sends back what TARS writes only without an ACL."""

    answer, echoes = OK, False

    def connect(self, address, port, keepalive=60):
        self.at = (address, port)

    def loop(self, timeout=1.0):
        if not getattr(self, "connected", False):
            self.connected = True
            self.on_connect(self, None, None, self.answer)

    def publish(self, topic, payload, qos=0):
        self.published = topic
        if self.echoes and topic in self.subscribed:
            self.on_message(self, None, type("Msg", (), {"topic": topic})())

    def disconnect(self):
        pass


OPEN = (
    "it lets TARS write on nuki/4224E764, a lock's own topic: without an ACL keeping it to the lock, any device with a "
    "password there could say the door was opened"
)


@pytest.mark.parametrize(
    "answer, echoes, said",
    [(OK, False, ("", "")), (NOT_AUTHORIZED, False, ("it refused TARS: Not authorized", "")), (OK, True, ("", OPEN))],
)
def test_check_says_whether_the_broker_lets_tars_in_and_keeps_the_locks_topics_to_the_locks(
    monkeypatch, answer, echoes, said
):
    paho = pytest.importorskip("paho.mqtt.client")
    made = []

    class Broker(ProbedClient):
        def __init__(self, *a, **kw):
            super().__init__()
            self.answer, self.echoes = answer, echoes
            made.append(self)

    monkeypatch.setattr(paho, "Client", Broker)
    from voice_assistant.doors import broker_answers

    mqtt = {"address": "localhost", "user": "tars"}
    assert broker_answers(mqtt, "hunter2", ["nuki/4224E764"], timeout_s=0.5, probe_s=0.5) == said
    if answer is OK:  # beside the lock's events, never a topic the lock acts on
        assert made[0].published == "nuki/4224E764/tars-check" and made[0].auth == ("tars", "hunter2")


# The door's sensor: greeting whoever let themselves in, and the routers asked as it moves.


class Hurried:
    def __init__(self):
        self.calls = []

    def hurry(self, inside=False, opened=None):
        self.calls.append(inside)
        self.opened = opened

    def at_door(self, person, door):
        return True


def sensing(log, clock):
    came, watcher = [], Hurried()
    doors = Doors(DoorLog(log.store, PEOPLE), watcher, clock=clock, came_in=lambda names, door: came.append(names))
    return doors, came, watcher


def test_someone_let_in_from_outside_has_come_in_as_the_door_shuts_behind_them_and_its_kept(log, capsys):
    clock = Clock()
    doors, came, watcher = sensing(log, clock)
    doors.heard(event("1,0,111,0,1"))  # Alon's Auto Unlock
    clock.t += 8
    doors.sensed("front door", True)
    assert came == []  # not while they're still in the doorway
    clock.t += 5
    doors.sensed("front door", False)
    assert came == [["Alon"]]
    assert watcher.calls == [False, False]  # asked at once each time; opened to let someone in, not to leave
    out = capsys.readouterr().out
    assert "(doors: the front door opened)" in out and "(doors: Alon came in by the front door)" in out
    rows = doors.log.came_in_since(0)
    assert [(r["door"], r["person"], r["at"]) for r in rows] == [("front door", "alon", NOW + 13)]
    assert doors.log.came_in_since(rows[-1]["id"]) == [] and doors.log.last_id() >= rows[-1]["id"]
    assert doors.log.unsettled(NOW + 3600) == []  # nothing to learn a key from


def test_the_door_opened_with_nobody_let_in_is_someone_inside_using_it_and_nobody_is_welcomed(log):
    clock = Clock()
    doors, came, watcher = sensing(log, clock)
    doors.heard(event("1,0,111,0,0"))  # the app: from anywhere
    doors.sensed("front door", True)
    clock.t += 6
    doors.sensed("front door", False)
    assert came == [] and watcher.calls == [False, True]  # told at the shut: by then a late lock would be heard
    assert watcher.opened == NOW  # someone left from when it opened


def test_a_lock_opened_long_before_the_door_lets_nobody_in_by_it(log):
    clock = Clock()
    doors, came, watcher = sensing(log, clock)
    doors.heard(event("1,0,111,0,1"))
    clock.t += 10 * MIN  # Auto Unlock, and then nobody came through
    doors.sensed("front door", True)
    doors.sensed("front door", False)
    assert came == [] and watcher.calls == [False, True]


def test_two_let_in_together_come_in_together_and_each_time_they_come_in_counts(log):
    clock = Clock()
    doors, came, _ = sensing(log, clock)
    doors.heard(event("1,0,111,0,1"))
    doors.heard(event("1,255,0,42,0"))  # Stacey's keypad code, once it's hers
    doors.log.store.write("INSERT INTO door_keys (key, person, learned) VALUES ('code:42', 'stacey', 0)")
    doors.heard(event("3,255,0,42,0"))
    doors.sensed("front door", True)
    doors.sensed("front door", False)
    assert came == [["Alon", "Stacey"]]
    clock.t += 5 * MIN  # out with the rubbish, and back by the keypad: whether to say anything is the skill's
    doors.heard(event("1,0,111,0,1", at=clock.t))
    doors.sensed("front door", True)
    doors.sensed("front door", False)
    assert came == [["Alon", "Stacey"], ["Alon"]]


def test_the_lock_heard_while_the_door_is_already_open_still_lets_them_in_and_nobody_left(log):
    clock = Clock()
    doors, came, watcher = sensing(log, clock)
    doors.sensed("front door", True)
    clock.t += 2
    doors.heard(event("3,0,111,0,1", at=clock.t))  # the lock publishes once its motor has finished
    clock.t += 5
    doors.sensed("front door", False)
    assert came == [["Alon"]] and True not in watcher.calls


def test_the_door_bouncing_or_opened_again_just_after_someone_came_in_isnt_anyone_leaving(log):
    clock = Clock()
    doors, came, watcher = sensing(log, clock)
    doors.heard(event("1,0,111,0,1"))
    doors.sensed("front door", True)
    clock.t += 5
    doors.sensed("front door", False)
    clock.t += 1
    doors.sensed("front door", True)  # it bounced off the frame
    doors.sensed("front door", False)
    clock.t += 30
    doors.sensed("front door", True)  # back out to the car for the shopping
    clock.t += 20
    doors.sensed("front door", False)
    assert came == [["Alon"]] and True not in watcher.calls
    clock.t += 2 * MIN
    doors.sensed("front door", True)  # a while later: someone inside using it
    doors.sensed("front door", False)
    assert watcher.calls[-1] is True


def test_someone_let_in_before_their_phone_is_on_the_wifi_isnt_out_when_the_door_is_opened_again(log):
    """Alon let in by Auto Unlock, his phone not on the Wi-Fi yet, and the door opened again 30 s later."""
    clock, changes = Clock(), []
    w = watching(log, clock, changes, alon=(False, NOW - 60 * MIN), stacey=(True, NOW - 60 * MIN))
    doors = Doors(DoorLog(log.store, PEOPLE), w, clock=clock)
    w.tracker.update({"stacey"}, True, 0.0, NOW)
    doors.heard(event("1,0,111,0,1"))
    doors.sensed("front door", True)
    clock.t += 5
    doors.sensed("front door", False)
    clock.t += 30
    doors.sensed("front door", True)
    clock.t += 5
    doors.sensed("front door", False)
    while clock.t < NOW + 5 * MIN:
        clock.t += 10
        changes += w.tracker.update({"stacey"}, True, clock.t - NOW, clock.t)
    assert changes == [("Alon", True)] and w.tracker.states["alon"].home is True


def test_someone_opening_the_door_from_inside_and_going_is_out_a_minute_and_a_half_after_the_door(log):
    clock, changes = Clock(), []
    w = watching(log, clock, changes, alon=(True, NOW - 60 * MIN), stacey=(True, NOW - 60 * MIN))
    doors = Doors(DoorLog(log.store, PEOPLE), w, clock=clock)
    w.tracker.update({"alon", "stacey"}, True, 0.0, NOW)
    clock.t += 3
    doors.sensed("front door", True)
    clock.t += 8
    doors.sensed("front door", False)  # decided now, counted from when it opened
    assert w.tracker._doors_used == [3.0]
    while not changes and clock.t < NOW + 5 * MIN:
        clock.t += 10
        changes += w.tracker.update({"stacey"}, True, clock.t - NOW, clock.t)
    assert changes == [("alon", False)] and clock.t == NOW + 101  # 90 s from the door, the first round after


def test_the_door_sensors_are_followed_for_their_moves_only(tmp_path, monkeypatch):
    """Home Assistant restarting, or the sensor heard again, isn't the door moving (hass.follow's moves_only)."""
    import threading
    import types

    import voice_assistant.hass as hass_module
    from voice_assistant.__main__ import follow_door_sensors

    (tmp_path / ".env").write_text("HOME_ASSISTANT_TOKEN=t\n")
    followed, done = [], threading.Event()

    def follow(url, token, entities, on_state, **kw):
        followed.append((entities, kw))
        on_state("binary_sensor.front_door", "on")
        done.set()

    monkeypatch.setattr(hass_module, "follow", follow)
    door = {"name": "front door", "topic": "nuki/1", "sensor": "binary_sensor.front_door"}
    cfg = types.SimpleNamespace(presence=PresenceConfig(doors=[door]), home=types.SimpleNamespace(url="http://ha"))
    sensed = types.SimpleNamespace(calls=[])
    sensed.sensed = lambda name, is_open: sensed.calls.append((name, is_open))
    follow_door_sensors(cfg, tmp_path, sensed)
    assert done.wait(5)
    assert followed == [(["binary_sensor.front_door"], {"moves_only": True})]
    assert sensed.calls == [("front door", True)]


def test_a_door_sensor_is_a_binary_sensor_and_needs_home_assistant(tmp_path):
    bad = PresenceConfig(doors=[{"name": "front door", "topic": "nuki/1", "sensor": "sensor.door"}])
    assert any("sensor must be" in p for p in _presence_problems(bad))
    good = PresenceConfig(doors=[{"name": "front door", "topic": "nuki/1", "sensor": "binary_sensor.front_door"}])
    assert not any("sensor" in p for p in _presence_problems(good))
    assert any("quiet must be" in p for p in _presence_problems(PresenceConfig(quiet="late")))
    from voice_assistant.config import ConfigError

    (tmp_path / "config.toml").write_text(
        '[presence]\n[[presence.doors]]\nname = "front door"\ntopic = "nuki/1"\nsensor = "binary_sensor.door"\n'
    )
    with pytest.raises(ConfigError, match=r"sensor is read from Home Assistant: set \[home\] url"):
        load_config(tmp_path / "config.toml", local=False)


@pytest.mark.parametrize(
    "spec, hour, minute, quiet",
    [("22:00-07:00", 23, 0, True), ("22:00-07:00", 6, 59, True), ("22:00-07:00", 7, 0, False),
     ("22:00-07:00", 12, 0, False), ("13:30-14:00", 13, 45, True), ("", 3, 0, False)],
)  # fmt: skip
def test_quiet_hours_wrap_past_midnight(spec, hour, minute, quiet):
    from datetime import datetime

    from voice_assistant.config import is_quiet

    at = datetime.now().astimezone().replace(hour=hour, minute=minute, second=0).timestamp()
    assert is_quiet(spec, at) is quiet
