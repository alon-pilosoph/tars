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
