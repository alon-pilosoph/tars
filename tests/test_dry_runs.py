"""Dry runs: a skill waiting on a device is replayed over the device's last week before it's kept (skills.py), from
its Home Assistant history worded as its status is now (hass.Entity.past)."""

from datetime import datetime

import httpx
import pytest

from evals.house import TOKEN, URL
from voice_assistant.hass import HomeAssistant, entity_device
from voice_assistant.home import Device, HomeError
from voice_assistant.skills import COOLDOWN_S, Skills, dry_run, dry_run_note, fired
from voice_assistant.store import Store

T0 = datetime(2026, 11, 2, 8, 0).astimezone().timestamp()
UNLOCKED = {
    "event": "device",
    "device": "front door",
    "says": "unlocked",
    "above": None,
    "below": None,
    "reading": None,
}
CO2 = {"event": "device", "device": "air quality", "says": None, "above": 1000, "below": None, "reading": "co2"}


def test_a_word_fires_only_when_the_status_comes_to_say_it():
    assert fired(UNLOCKED, "front door: locked", "front door: unlocked")
    assert not fired(UNLOCKED, "front door: unlocked", "front door: unlocked, door open")
    assert not fired(UNLOCKED, "front door: unlocked", "front door: locked")


def test_a_reading_fires_going_over_its_limit_not_staying_there():
    assert fired(CO2, "air quality: CO2 950 ppm", "air quality: CO2 1100 ppm")
    assert not fired(CO2, "air quality: CO2 1100 ppm", "air quality: CO2 1200 ppm")


def test_a_dry_run_counts_each_time_as_the_clock_would_with_its_cooldown():
    past = [
        (T0, "front door: locked"),
        (T0 + 60, "front door: unlocked"),
        (T0 + 120, "front door: locked"),
        (T0 + 180, "front door: unlocked"),  # within the cooldown: the clock wouldn't run it again
        (T0 + 240, "front door: locked"),
        (T0 + 60 + COOLDOWN_S + 1, "front door: unlocked"),
    ]
    assert dry_run(UNLOCKED, past) == [T0 + 60, T0 + 60 + COOLDOWN_S + 1]


def test_the_note_says_how_often_and_when_and_when_to_bring_it_up():
    note = dry_run_note(UNLOCKED, [T0 + 3600 * i for i in (0, 24, 48)])
    assert note.startswith("checked against the last 7 days of the front door's history: it would have run 3 times")
    assert "(most often around 08:00)" in note and "Say so only if" in note
    assert ": it never would have." in dry_run_note(UNLOCKED, [])


def state(entity: str, value: str, at: float, **attributes) -> dict:
    return {
        "entity_id": entity,
        "state": value,
        "attributes": attributes,
        "last_changed": datetime.fromtimestamp(at).astimezone().isoformat(),
    }


def test_a_devices_past_is_its_history_worded_as_its_status_is_now():
    asked = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        lock = [
            state("lock.front_door", "locked", T0),
            state("lock.front_door", "unlocked", T0 + 60),
            state("lock.front_door", "locked", T0 + 300),
        ]
        door = [state("binary_sensor.front_door", "off", T0), state("binary_sensor.front_door", "on", T0 + 90)]
        return httpx.Response(200, json=[lock, door])

    hass = HomeAssistant(URL, TOKEN, httpx.MockTransport(handle))
    lock = entity_device("front door", {"entity": "lock.front_door", "door": "binary_sensor.front_door"}, hass)
    past = lock.past(T0, T0 + 3600)
    assert past == [
        (T0, "front door: locked, door closed"),
        (T0 + 60, "front door: unlocked, door closed"),
        (T0 + 90, "front door: unlocked, door open"),
        (T0 + 300, "front door: locked, door open"),
    ]
    query = asked[0].url.params
    assert "no_attributes" not in query and query["filter_entity_id"] == "lock.front_door,binary_sensor.front_door"


def test_a_device_without_history_has_no_past():
    class Remote(Device):
        pass

    with pytest.raises(HomeError, match="keeps no history"):
        Remote("tv").past(T0, T0 + 1)


def saving(tmp_path, past):
    skills = Skills(Store(tmp_path / "db"), clock=lambda: T0 + 7 * 86400)
    skills.devices, skills.status = (lambda: {"front door", "air quality"}), (lambda d: f"{d}: locked")
    skills.past = past
    return skills.call(
        {"action": "save", "name": "door", "instructions": "Tell Alon.", "trigger": dict(UNLOCKED)},
        type("Request", (), {"speaker": "Alon", "by_chat": False})(),
    )


def test_saving_a_device_skill_tells_the_model_what_its_dry_run_found(tmp_path):
    asked = []

    def past(device, start, end):
        asked.append((device, end - start))
        return [(T0 + i * 3600, "front door: unlocked" if i % 2 else "front door: locked") for i in range(60)]

    said = saving(tmp_path, past)
    assert asked == [("front door", 7 * 86400.0)]
    assert said.startswith("done: kept") and "Dry run: checked against the last 7 days" in said
    assert "it would have run 30 times" in said


def test_with_no_history_to_read_its_kept_without_a_dry_run(tmp_path, capsys):
    def past(device, start, end):
        raise HomeError("Home Assistant didn't answer")

    said = saving(tmp_path, past)
    assert said.startswith("done: kept") and "Dry run" not in said
    assert "no dry run for the front door" in capsys.readouterr().out
