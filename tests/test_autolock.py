"""Auto-lock that knows whether the door is shut (autolock.py): the rules, each with a clock TARS moves itself."""

import pytest

from voice_assistant.autolock import LOCK_SETTLE_S, AutoLock, AutoLockDevice, Off, Settings, problems
from voice_assistant.home import HomeError, Request

LOCK, DOOR = "lock.front_door", "binary_sensor.front_door"


class House:
    """Home Assistant as the auto-lock sees it, a clock, and whatever was said, posted and told."""

    def __init__(self, tmp_path, door="off", lock="locked", anyone_home=True, settings=None):
        self.t, self.states = 0.0, {LOCK: lock, DOOR: door}
        self.locked, self.said, self.posted, self.told = 0, [], [], []
        self.home, self.jammed = anyone_home, False
        self.off = Off(tmp_path, LOCK, clock=lambda: 1_800_000_000.0 + self.t)
        self.auto = AutoLock(
            "front door", LOCK, DOOR, settings or Settings(), self.off, self.read, self.lock, self.said.append,
            self.posted.append, self.told.append, lambda: self.home, clock=lambda: self.t,
        )  # fmt: skip
        self.auto.start_from(lock, door)

    def read(self, entity):
        state = self.states[entity]
        if state == "error":
            raise HomeError("Home Assistant didn't answer")
        return state

    def lock(self):
        self.locked += 1
        if not self.jammed:
            self.set(LOCK, "locked")

    def set(self, entity, state):
        self.states[entity] = state
        self.auto.changed(entity, state)

    def wait(self, seconds):
        """Steps every second, as the auto-lock's thread does."""
        for _ in range(int(seconds)):
            self.t += 1
            self.auto.step()


def test_the_door_opened_and_shut_is_locked_ten_seconds_after_it_shut(tmp_path):
    h = House(tmp_path)
    h.set(LOCK, "unlocked")
    h.wait(3)
    h.set(DOOR, "on")
    h.wait(120)  # carrying the shopping in: never locked while it's open
    assert h.locked == 0
    h.set(DOOR, "off")
    h.wait(9)
    assert h.locked == 0
    h.wait(1)
    assert h.locked == 1 and h.states[LOCK] == "locked"


def test_opened_again_before_the_time_starts_the_count_over(tmp_path):
    h = House(tmp_path)
    h.set(LOCK, "unlocked")
    h.set(DOOR, "on")
    h.set(DOOR, "off")
    h.wait(8)
    h.set(DOOR, "on")
    h.set(DOOR, "off")
    h.wait(8)
    assert h.locked == 0
    h.wait(2)
    assert h.locked == 1


def test_unlocked_and_never_opened_is_locked_after_a_minute(tmp_path):
    h = House(tmp_path)
    h.set(LOCK, "unlocked")  # Auto Unlock as someone walks past
    h.wait(59)
    assert h.locked == 0
    h.wait(1)
    assert h.locked == 1


def test_the_door_read_open_just_before_locking_isnt_locked(tmp_path):
    h = House(tmp_path)
    h.set(LOCK, "unlocked")
    h.set(DOOR, "on")
    h.set(DOOR, "off")
    h.states[DOOR] = "on"  # opened again, and Home Assistant's word on it hasn't come yet
    h.wait(10)
    assert h.locked == 0
    h.set(DOOR, "off")
    h.wait(10)
    assert h.locked == 1


def test_a_lock_that_doesnt_lock_is_told_once_and_not_tried_again_until_its_next_unlocked(tmp_path):
    h = House(tmp_path)
    h.jammed = True
    h.set(LOCK, "unlocked")
    h.wait(60 + LOCK_SETTLE_S + 5)
    assert h.locked == 1 and len(h.told) == 1 and "still unlocked" in h.told[0]
    h.wait(600)
    assert h.locked == 1 and len(h.told) == 1
    h.set(LOCK, "locked")  # by hand
    h.jammed = False
    h.set(LOCK, "unlocked")
    h.wait(60)
    assert h.locked == 2


def test_a_door_left_open_is_said_aloud_once_or_posted_when_nobodys_home(tmp_path):
    h = House(tmp_path)
    h.set(DOOR, "on")
    h.wait(119)
    assert h.said == []
    h.wait(300)
    assert h.said == ["The front door has been open for 2 minutes."]
    h.home = False
    h.set(DOOR, "off")
    h.set(DOOR, "on")
    h.wait(120)
    assert h.posted == ["The front door has been open for 2 minutes. Nobody's home, as far as I can tell."]


def test_a_sensor_that_doesnt_answer_isnt_trusted_and_the_lock_goes_by_the_clock(tmp_path):
    h = House(tmp_path)
    h.set(DOOR, "unavailable")
    h.set(LOCK, "unlocked")
    h.wait(29)
    assert h.locked == 0 and len(h.told) == 1 and "sensor isn't answering" in h.told[0]
    h.wait(1)
    assert h.locked == 1
    h.set(DOOR, "off")
    assert len(h.told) == 2 and "answering again" in h.told[1]


def test_turned_off_it_locks_nothing_and_says_nothing_then_comes_back_by_itself(tmp_path):
    h = House(tmp_path)
    device = AutoLockDevice("front door", Settings(), h.off)
    h.off.set(1_800_000_000.0 + 30 * 60)  # off for half an hour, as the device does it
    assert device.status().startswith("front door auto-lock: off until ")
    h.set(LOCK, "unlocked")
    h.set(DOOR, "on")
    h.wait(10 * 60)
    h.set(DOOR, "off")
    h.wait(10 * 60)
    assert h.locked == 0 and h.said == []
    h.wait(10 * 60 + 1)  # the half hour is up: it's shut, and was long ago
    assert h.locked == 1 and device.status() == "front door auto-lock: on"


def test_already_unlocked_when_tars_starts_is_locked_once_the_door_is_shut(tmp_path):
    h = House(tmp_path, lock="unlocked", door="off")
    h.wait(60)
    assert h.locked == 1


def test_the_device_turns_it_off_for_a_while_and_back_on_and_a_doors_lock_cant(tmp_path):
    off = Off(tmp_path, LOCK)
    device = AutoLockDevice("front door", Settings(), off)
    assert device.call("off", {"minutes": 30}, Request()).startswith("done: front door auto-lock is off until ")
    assert off.until() is not None
    assert device.call("off", {"minutes": 5000}, Request()).startswith("error:")
    assert device.call("off", {"minutes": "soon"}, Request()).startswith("error:")
    assert device.call("on", {}, Request()) == "done: front door auto-lock is on" and off.until() is None
    assert device.call("off", {}, Request(by_door="front door")).startswith("refused:")
    assert Off(tmp_path, "lock.back_door").until() is None  # each lock its own


@pytest.mark.parametrize(
    "value, says",
    [(True, None), ({"after_shut_s": 5, "open_alert_min": 0}, None), ("yes", "must be true, or a table"),
     ({"after_shut_s": 900}, "after_shut_s must be between"), ({"pin": 1}, "has no setting pin")],
)  # fmt: skip
def test_the_settings_are_checked(value, says):
    found = problems(value, "[[home.devices]] #1")
    assert (found == []) if says is None else any(says in p for p in found)


def test_auto_lock_needs_the_door_sensor(tmp_path):
    from voice_assistant.config import ConfigError, load_config

    (tmp_path / "config.toml").write_text(
        '[home]\nurl = "http://127.0.0.1:8123"\n[[home.devices]]\nname = "front door"\nentity = "lock.front_door"\n'
        "auto_lock = true\n"
    )
    with pytest.raises(ConfigError, match="auto_lock needs door"):
        load_config(tmp_path / "config.toml", local=False)
