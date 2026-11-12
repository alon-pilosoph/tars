"""Home Assistant's devices, against a pretend Home Assistant: each change is "done" only when the entity reports it,
"sent" when the entity can't report back, and anything else is said as it is."""

import json

import httpx
import pytest

from evals.house import TOKEN, URL, PretendHA, entities
from voice_assistant.config import HOME_DOMAINS, HOME_SENSORS
from voice_assistant.hass import KINDS, Climate, Cover, HomeAssistant, Lock, MediaPlayer, Sensor, Switch, entity_device
from voice_assistant.home import SETTINGS, HomeError

TIMER = {"service": "switcher_kis.turn_on_with_timer", "field": "timer_minutes", "max": 150}


def device(spec: dict, house: dict | None = None, steps: int = 1, **flags):
    """The device for `spec`, over a pretend Home Assistant set up as `house`, with a clock that moves only when it
    waits."""
    ha = PretendHA(entities(house or {}), steps=steps, **flags)
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    hass = HomeAssistant(URL, TOKEN, ha.transport, ha.command)
    made = entity_device(spec["name"], spec, hass, clock=lambda: now[0], sleep=sleep)
    return made, ha


def do(d, action: str, **settings) -> str:
    return d.call(action, dict.fromkeys(SETTINGS) | settings, None)


LOCK = {"name": "front door", "entity": "lock.front_door", "door": "binary_sensor.front_door_door"}


def test_each_domain_gets_its_kind_of_device():
    kinds = {
        "lock.x": Lock,
        "switch.x": Switch,
        "light.x": Switch,
        "cover.x": Cover,
        "climate.x": Climate,
        "media_player.x": MediaPlayer,
        "binary_sensor.x": Sensor,
    }
    for entity, kind in kinds.items():
        assert type(entity_device("x", {"entity": entity}, None)) is kind


def test_every_domain_config_accepts_has_a_kind():
    assert set(KINDS) == set(HOME_DOMAINS + HOME_SENSORS)


def test_locking_waits_for_the_lock_to_say_it_is_locked():
    lock, ha = device(LOCK, steps=3)
    assert do(lock, "lock") == "done: front door reports locked"
    assert ha.calls == [("lock.lock", {"entity_id": "lock.front_door"})]


def test_a_lock_that_never_finishes_turning_is_not_said_to_be_locked():
    lock, _ = device(LOCK, steps=1000)
    assert do(lock, "lock") == "not confirmed: asked the front door to lock, and it still says locking"


def test_a_jammed_lock_is_said_to_be_jammed():
    lock, _ = device(LOCK, jams=True)
    assert do(lock, "lock") == "error: the front door jammed; someone needs to check it"


def test_an_open_door_is_not_locked():
    lock, ha = device(LOCK, {"door": "open"})
    assert "door sensor says the door is open" in do(lock, "lock") and ha.calls == []


def test_a_locks_status_says_its_door_too():
    lock, _ = device(LOCK, {"lock": "locked"})
    assert lock.status() == "front door: locked, door closed"
    assert lock.slow("lock", {}) and lock.slow("unlock", {})


def test_a_switch_turns_on_and_off_and_says_what_it_reports():
    boiler, ha = device({"name": "boiler", "entity": "switch.boiler"})
    assert do(boiler, "on") == "done: boiler reports on"
    assert do(boiler, "off") == "done: boiler reports off"
    assert [c for c, _ in ha.calls] == ["switch.turn_on", "switch.turn_off"]


def test_a_switch_with_a_timer_goes_on_for_some_minutes_with_its_own_timer():
    spec = {"name": "boiler", "entity": "switch.boiler", "timer": TIMER, "details": ["sensor.boiler_time_left"]}
    boiler, ha = device(spec)
    assert do(boiler, "on", minutes=40) == "done: boiler reports on, for 40 minutes"
    assert ha.calls == [("switcher_kis.turn_on_with_timer", {"entity_id": "switch.boiler", "timer_minutes": 40})]
    assert boiler.status() == "boiler: on, Boiler time left: 40 min"
    assert "or on for some minutes (up to 150)" in boiler.what()


@pytest.mark.parametrize("minutes, says", [(0, "1 to 150 minutes"), (151, "1 to 150 minutes"), ("x", "a number")])
def test_a_timer_it_cant_set_is_an_error(minutes, says):
    boiler, ha = device({"name": "boiler", "entity": "switch.boiler", "timer": TIMER})
    assert says in do(boiler, "on", minutes=minutes) and ha.calls == []


def test_without_a_timer_minutes_are_an_error_that_says_what_to_do():
    light, _ = device({"name": "light", "entity": "switch.boiler"})
    assert "set a reminder to turn it off" in do(light, "on", minutes=10)


def test_a_cover_on_its_way_has_started():
    blinds, ha = device({"name": "blinds", "entity": "cover.living_room"}, steps=1000)
    assert do(blinds, "close") == "started: blinds reports closing"
    assert ha.calls == [("cover.close_cover", {"entity_id": "cover.living_room"})]


def test_an_ac_home_assistant_only_assumes_is_sent_never_done():
    ac, ha = device({"name": "AC", "entity": "climate.living_room"})
    said = do(ac, "on", mode="cool", temperature=22)
    assert (
        said == "sent: asked the AC to turn on at cool, 22 degrees; it can't report back, so nothing can tell if it did"
    )
    assert [c for c, _ in ha.calls] == ["climate.set_hvac_mode", "climate.set_temperature"]


def test_a_thermostat_that_reports_back_is_done_with_what_it_reports():
    house = entities({})
    house["climate.living_room"][1].pop("assumed_state")
    ha = PretendHA(house)
    ac = entity_device("AC", {"entity": "climate.living_room"}, HomeAssistant(URL, TOKEN, ha.transport))
    said = do(ac, "on", mode="heat", temperature=21, fan="low")
    assert said == "done: AC: heat, set to 21 degrees, fan low (it says so itself)"


def test_a_thermostat_takes_only_the_modes_fans_and_temperatures_it_lists():
    ac, ha = device({"name": "AC", "entity": "climate.living_room"})
    assert "modes off, cool, heat" in do(ac, "on", mode="turbo")
    assert "fan speeds low, medium" in do(ac, "on", fan="hurricane")
    assert "16 to 30 degrees" in do(ac, "on", temperature=40)
    assert ha.calls == []
    assert do(ac, "on", mode="fan").startswith("sent: asked the AC to turn on at fan_only")  # as people say it


def test_changing_settings_while_off_says_to_turn_it_on():
    house = entities({"ac": "off"})
    house["climate.living_room"][1].pop("assumed_state")
    ha = PretendHA(house)
    ac = entity_device("AC", {"entity": "climate.living_room"}, HomeAssistant(URL, TOKEN, ha.transport))
    assert "use on" in do(ac, "set", temperature=21) and ha.calls == []
    assert "set needs a mode" in device({"name": "AC", "entity": "climate.living_room"}, {"ac": "cool"})[0].call(
        "set", dict.fromkeys(SETTINGS), None
    )


def test_an_ac_home_assistant_only_guesses_is_off_is_still_set_and_its_status_says_its_a_guess():
    # Someone turned it on with its remote: Home Assistant still has it off, and an IR one sends nothing while it does.
    ac, ha = device({"name": "AC", "entity": "climate.living_room"}, {"ac": "off"})
    assert ac.status().startswith("AC: off (Home Assistant's guess: it can't report back)")
    assert do(ac, "set", temperature=21).startswith("sent: asked the AC to turn on at 21 degrees")
    assert [c for c, _ in ha.calls] == ["climate.turn_on", "climate.set_temperature"]
    assert do(ac, "off").startswith("sent: asked the AC to turn off")  # whatever Home Assistant has it as


def test_a_media_player_presses_its_buttons_and_says_where_it_is_now():
    soundbar, ha = device({"name": "soundbar", "entity": "media_player.soundbar"})
    assert (
        do(soundbar, "press", button="volume up", times=3)
        == "done: pressed volume_up 3 times; now soundbar: on, volume 45%"
    )
    assert do(soundbar, "press", button="mute").endswith("now soundbar: on, muted")
    assert ha.calls[-1] == ("media_player.volume_mute", {"entity_id": "media_player.soundbar", "is_volume_muted": True})
    assert do(soundbar, "press", button="eject").startswith("error: the soundbar's buttons are")


def test_mute_is_pressed_once_however_many_times_it_was_asked():
    soundbar, ha = device({"name": "soundbar", "entity": "media_player.soundbar"})
    assert do(soundbar, "press", button="mute", times=3) == "done: pressed mute; now soundbar: on, muted"
    assert [c for c, _ in ha.calls] == ["media_player.volume_mute"]


class Plays(PretendHA):
    """A media player whose play and pause show in its state."""

    def _serve(self, domain, service, entity, data):
        super()._serve(domain, service, entity, data)
        if service == "media_play_pause":
            self.states[entity][0] = "paused" if self.states[entity][0] == "playing" else "playing"


def test_a_press_is_done_only_once_what_the_player_reports_shows_it():
    soundbar, _ = device({"name": "soundbar", "entity": "media_player.soundbar"})
    unchanged = "sent: play_pause to the soundbar, and what it reports hasn't changed"
    assert do(soundbar, "press", button="play pause").startswith(unchanged)
    loudest, ha = device({"name": "soundbar", "entity": "media_player.soundbar"})
    ha.states["media_player.soundbar"][1]["volume_level"] = 1.0
    assert do(loudest, "press", button="volume_up").startswith("sent: volume_up to the soundbar, and what it")
    playing = Plays(entities({}))
    player = entity_device(
        "soundbar", {"entity": "media_player.soundbar"}, HomeAssistant(URL, TOKEN, playing.transport)
    )
    assert do(player, "press", button="play_pause").startswith("done: pressed play_pause; now soundbar: playing")


class Rounds(PretendHA):
    """A thermostat that keeps its temperature in halves of a degree, as many do."""

    def _serve(self, domain, service, entity, data):
        super()._serve(domain, service, entity, data)
        if service == "set_temperature":
            self.states[entity][1]["temperature"] = round(data["temperature"] * 2) / 2


def test_a_temperature_the_thermostat_rounds_to_its_own_steps_is_done():
    house = entities({"ac": "heat"})
    house["climate.living_room"][1].pop("assumed_state")
    ha = Rounds(house)
    ac = entity_device("AC", {"entity": "climate.living_room"}, HomeAssistant(URL, TOKEN, ha.transport))
    assert do(ac, "set", temperature=21.3) == "done: AC: heat, set to 21.5 degrees, fan auto (it says so itself)"


class Slow(PretendHA):
    """Home Assistant doing the service, but not answering before TARS stops waiting: a lock that took a while."""

    def handle(self, request):
        response = super().handle(request)
        if request.url.path.startswith("/api/services/"):
            raise httpx.ReadTimeout("timed out", request=request)
        return response


def test_a_service_call_home_assistant_answers_late_is_read_back_not_said_to_have_failed():
    ha = Slow(entities({"lock": "locked"}))
    hass = HomeAssistant(URL, TOKEN, ha.transport)
    assert hass.call("lock.lock", {"entity_id": "lock.front_door"}) is False  # sent, and no answer
    lock = entity_device("front door", LOCK, hass, sleep=lambda s: None)
    assert do(lock, "unlock") == "done: front door reports unlocked"


def test_presses_stop_at_the_first_home_assistant_doesnt_answer_and_what_it_did_is_read_back():
    ha = Slow(entities({}))
    soundbar = entity_device("soundbar", {"entity": "media_player.soundbar"}, HomeAssistant(URL, TOKEN, ha.transport))
    said = do(soundbar, "press", button="volume_up", times=8)
    assert said.startswith("done: pressed volume_up (of 8: Home Assistant stopped answering); now soundbar: on")
    assert len(ha.calls) == 1


def test_home_assistant_not_reached_for_a_service_is_still_an_error():
    ha = PretendHA(entities({}), down=True)
    with pytest.raises(HomeError, match="didn't answer"):
        HomeAssistant(URL, TOKEN, ha.transport).call("lock.unlock", {"entity_id": "lock.front_door"})


def test_a_sensor_is_read_in_words_and_never_changed():
    room, _ = device({"name": "living room", "entity": "sensor.living_room_temperature"})
    door, _ = device({"name": "door", "entity": "binary_sensor.front_door_door"}, {"door": "open"})
    assert room.status() == "living room: 23.5 °C" and door.status() == "door: open"
    assert room.actions == ("status", "history") and door.actions == ("status",)
    assert do(room, "on").startswith("error")


def test_home_assistant_not_answering_is_an_error():
    lock, _ = device(LOCK, down=True)
    with pytest.raises(HomeError, match="didn't answer"):
        do(lock, "lock")


def test_a_refused_token_says_where_the_token_goes():
    ha = PretendHA(entities({}))
    hass = HomeAssistant(URL, "wrong", ha.transport)
    with pytest.raises(HomeError, match="HOME_ASSISTANT_TOKEN"):
        hass.ping()


def test_check_finds_every_entity_a_device_reads():
    lock, _ = device(LOCK)
    assert [c.status for c in lock.check()] == ["ok"]
    missing, _ = device({**LOCK, "battery": "sensor.front_door_battery"})
    (found,) = missing.check()
    assert found.status == "fail" and "sensor.front_door_battery" in found.detail


def test_an_entity_home_assistant_cant_reach_is_said_so():
    ha = PretendHA({"lock.front_door": ("unavailable", {})})
    lock = entity_device("front door", {"entity": "lock.front_door"}, HomeAssistant(URL, TOKEN, ha.transport))
    assert lock.status() == "front door: not reachable (Home Assistant can't reach it)"
    assert lock.check()[0].status == "warn"


def test_home_assistant_says_why_it_refused_a_service():
    def handler(request):
        return httpx.Response(400, text="Entity does not support this service.")

    hass = HomeAssistant(URL, TOKEN, httpx.MockTransport(handler))
    with pytest.raises(HomeError, match="HTTP 400: Entity does not support"):
        hass.call("cover.open_cover", {"entity_id": "cover.x"})


# Following a door sensor (hass.follow): its changes as they happen, and Home Assistant coming back.


class FakeSocket:
    def __init__(self, messages, auth="auth_ok"):
        self.sent, self._in = [], [{"type": "auth_required"}, {"type": auth}, *messages]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        if not self._in:
            raise ConnectionResetError("Home Assistant restarting")
        return json.dumps(self._in.pop(0))


def changed(entity, new, old="off"):
    return {
        "type": "event",
        "event": {"data": {"entity_id": entity, "new_state": {"state": new}, "old_state": {"state": old}}},
    }


def test_following_hands_on_each_change_of_the_door_and_comes_back_after_home_assistant_goes(monkeypatch, capsys):
    import voice_assistant.hass as hass_module

    door = "binary_sensor.front_door"
    sockets = [
        FakeSocket([changed(door, "on"), changed("light.hall", "on"), changed(door, "on", old="on")]),
        OSError("connection refused"),
        FakeSocket([changed(door, "unavailable", old="on"), changed(door, "off", old="unavailable")]),
    ]
    made = []

    def connect(url, **kw):
        made.append(url)
        s = sockets.pop(0)
        if isinstance(s, Exception):
            raise s
        return s

    monkeypatch.setattr(hass_module, "connect", connect)
    monkeypatch.setattr(hass_module.time, "sleep", lambda s: None)
    seen = []
    done = lambda: (door, "off") in seen or len(made) > 3
    hass_module.follow("http://127.0.0.1:8123/", "token", [door], lambda e, s: seen.append((e, s)), done)
    assert seen == [(door, "on"), (door, "off")]  # not the hall light, nor attributes, nor unavailable
    assert made[0] == "ws://127.0.0.1:8123/api/websocket"
    out = capsys.readouterr().out
    assert "(home: following binary_sensor.front_door)" in out and "following again" in out
    assert out.count("can't follow") == 1  # said once, however long it takes


def test_following_says_each_time_its_subscribed_and_hands_on_unknown_with_unknown_too(monkeypatch):
    """on_subscribed: the first time and after a reconnect, so what changed meanwhile can be read."""
    import voice_assistant.hass as hass_module

    door = "binary_sensor.front_door"
    taken = {"id": 1, "type": "result", "success": True, "result": None}
    sockets = [
        FakeSocket([taken, changed(door, "unavailable")]),
        FakeSocket([{"id": 1, "type": "result", "success": False}, taken, changed(door, "off", old="unavailable")]),
    ]
    monkeypatch.setattr(hass_module, "connect", lambda url, **kw: sockets.pop(0))
    monkeypatch.setattr(hass_module.time, "sleep", lambda s: None)
    seen = []
    on_state = lambda e, s: seen.append(s)
    on_subscribed = lambda: seen.append("subscribed")
    hass_module.follow(
        "http://ha:8123", "token", [door], on_state, lambda: not sockets and "off" in seen,
        unknown_too=True, on_subscribed=on_subscribed,
    )  # fmt: skip
    assert seen == ["subscribed", "unavailable", "subscribed", "off"]


def test_a_token_home_assistant_refuses_is_said(monkeypatch, capsys):
    import voice_assistant.hass as hass_module

    tries = []
    monkeypatch.setattr(
        hass_module, "connect", lambda url, **kw: tries.append(1) or FakeSocket([], auth="auth_invalid")
    )
    monkeypatch.setattr(hass_module.time, "sleep", lambda s: None)
    hass_module.follow("http://ha:8123", "bad", ["binary_sensor.d"], lambda e, s: None, lambda: len(tries) >= 3)
    assert "refused the token" in capsys.readouterr().out


def test_following_moves_only_leaves_out_a_change_from_unknown_or_unavailable(monkeypatch):
    """Home Assistant restarting, or a Bluetooth sensor heard again, says where the door is, not that it moved."""
    import voice_assistant.hass as hass_module

    door = "binary_sensor.front_door"
    restored = {"type": "event", "event": {"data": {"entity_id": door, "new_state": {"state": "on"}}}}
    socket = FakeSocket(
        [
            changed(door, "on", old="unavailable"),
            changed(door, "off", old="unknown"),
            restored,  # no old state at all: Home Assistant starting
            changed(door, "on", old="off"),
            changed(door, "off", old="on"),
        ]
    )
    monkeypatch.setattr(hass_module, "connect", lambda url, **kw: socket)
    monkeypatch.setattr(hass_module.time, "sleep", lambda s: None)
    seen = []
    done = lambda: len(seen) >= 2 or not socket._in
    hass_module.follow("http://ha:8123", "token", [door], lambda e, s: seen.append(s), done, moves_only=True)
    assert seen == ["on", "off"]
