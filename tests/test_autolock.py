"""Auto-lock that knows whether the door is shut (autolock.py): the rules, each with a clock TARS moves itself."""

import pytest

from voice_assistant.autolock import (
    LOCK_SETTLE_S,
    MAX_TRIES,
    RESYNC_S,
    RETRY_S,
    SENSOR_GRACE_S,
    AutoLock,
    AutoLockDevice,
    Off,
    Settings,
    problems,
)
from voice_assistant.home import HomeError, Request

LOCK, DOOR = "lock.front_door", "binary_sensor.front_door"


class House:
    """Home Assistant as the auto-lock sees it, a clock, and whatever was said, posted and told."""

    def __init__(self, tmp_path, door="off", lock="locked", anyone_home=True, settings=None):
        self.t, self.states = 0.0, {LOCK: lock, DOOR: door}
        self.locked, self.said, self.posted, self.told = 0, [], [], []
        self.home, self.jammed, self.failing, self.call_s, self.answer = anyone_home, False, 0, 0.0, None
        self.off = Off(tmp_path, LOCK, clock=lambda: 1_800_000_000.0 + self.t)
        self.auto = AutoLock(
            "front door", LOCK, DOOR, settings or Settings(), self.off, self.read, self.lock, self.said.append,
            self.posted.append, self.told.append, lambda: self.home, clock=lambda: self.t,
        )  # fmt: skip
        self.auto.resync()  # as the first step does

    def read(self, entity):
        state = self.states[entity]
        if state == "error":
            raise HomeError("Home Assistant didn't answer")
        return state

    def lock(self):
        """Home Assistant's lock.lock: refused `failing` more times, taking `call_s` (the clock moves on as it's
        waited for), answering `answer`."""
        self.t += self.call_s
        if self.failing:
            self.failing -= 1
            raise HomeError("Home Assistant said HTTP 500")
        self.locked += 1
        if not self.jammed:
            self.set(LOCK, "locked")
        return self.answer

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
    assert h.locked == 1 and len(h.told) == 1 and "still says unlocked" in h.told[0] and "bolt" in h.told[0]
    h.wait(600)
    assert h.locked == 1 and len(h.told) == 1
    h.set(LOCK, "locked")  # by hand
    h.jammed = False
    h.set(LOCK, "unlocked")
    h.wait(60)
    assert h.locked == 2


def test_a_call_to_lock_home_assistant_didnt_take_is_tried_again_reading_the_door_first(tmp_path):
    h = House(tmp_path)
    h.failing = 1
    h.set(LOCK, "unlocked")
    h.wait(60)
    assert h.locked == 0 and h.auto.tries == 1
    h.wait(RETRY_S[0])
    assert h.locked == 1 and h.states[LOCK] == "locked" and h.told == []


def test_a_call_to_lock_that_keeps_failing_is_tried_max_tries_times_then_told_what_failed(tmp_path):
    h = House(tmp_path)
    h.failing = 99
    h.set(LOCK, "unlocked")
    h.wait(60 + sum(RETRY_S) + 600)
    assert h.auto.tries == MAX_TRIES and h.locked == 0 and len(h.told) == 1
    assert "Home Assistant didn't take the call to lock it (Home Assistant said HTTP 500)" in h.told[0]
    assert "bolt" not in h.told[0] and "own auto-lock" in h.told[0]


def test_a_retry_finds_the_door_opened_meanwhile_and_waits_for_it_to_shut(tmp_path):
    h = House(tmp_path)
    h.failing = 1
    h.set(LOCK, "unlocked")
    h.wait(60)
    h.states[DOOR] = "on"  # opened, and Home Assistant's word on it hasn't come yet
    h.wait(RETRY_S[0])
    assert h.locked == 0 and h.auto.door_state == "on"
    h.set(DOOR, "off")
    h.wait(10)
    assert h.locked == 1


def test_a_slow_call_to_lock_is_given_its_settling_time_from_when_it_returns(tmp_path):
    """The call can take SERVICE_TIMEOUT_S (30 s), longer than LOCK_SETTLE_S: a lock that says locked soon after
    it returns isn't told as one that didn't lock."""
    h = House(tmp_path)
    h.jammed, h.call_s, h.answer = True, 25.0, False  # Home Assistant didn't answer in time
    h.set(LOCK, "unlocked")
    h.wait(60)
    h.wait(3)
    h.set(LOCK, "locked")
    h.wait(LOCK_SETTLE_S * 2)
    assert h.locked == 1 and h.told == []


def test_a_slow_call_to_a_lock_that_never_locks_says_home_assistant_didnt_answer(tmp_path):
    h = House(tmp_path)
    h.jammed, h.call_s, h.answer = True, 30.0, False
    h.set(LOCK, "unlocked")
    h.wait(60 + LOCK_SETTLE_S - 1)
    assert h.told == []
    h.wait(1)
    assert len(h.told) == 1 and "still says unlocked (Home Assistant hadn't answered the call in time)" in h.told[0]


def test_a_lock_that_says_it_jammed_is_told_at_once_and_not_tried_again(tmp_path):
    h = House(tmp_path)
    h.jammed = True
    h.set(LOCK, "unlocked")
    h.wait(60)
    h.set(LOCK, "jammed")
    h.wait(1)
    assert len(h.told) == 1 and "the lock says it jammed" in h.told[0]
    h.wait(600)
    assert h.locked == 1 and len(h.told) == 1


@pytest.mark.parametrize("flap", ["unavailable", "unknown"])
def test_a_lock_dropping_out_of_reach_and_back_isnt_a_new_unlocking(tmp_path, flap):
    """Its Bluetooth flapping, or Home Assistant restarting: a jammed lock is tried once, and told once."""
    h = House(tmp_path)
    h.jammed = True
    h.set(LOCK, "unlocked")
    h.wait(60 + LOCK_SETTLE_S)
    assert h.locked == 1 and len(h.told) == 1
    for _ in range(3):
        h.set(LOCK, flap)
        h.set(LOCK, "unlocked")
        h.wait(600)
    assert h.locked == 1 and len(h.told) == 1


def test_unlatching_isnt_a_new_unlocking_and_keeps_its_count(tmp_path):
    h = House(tmp_path)
    for state in ("unlocking", "unlocked"):
        h.set(LOCK, state)
    h.wait(50)
    for state in ("opening", "open", "unlocked"):  # Nuki's unlatch, the door never opened
        h.set(LOCK, state)
    h.wait(9)
    assert h.locked == 0
    h.wait(1)
    assert h.locked == 1  # a minute after it was unlocked, not after it unlatched


@pytest.mark.parametrize("path", [("opening", "open"), ("unlocking", "unlocked"), ("jammed", "unlocked"),
                                  ("unavailable", "unlocked")])  # fmt: skip
def test_any_way_from_locked_to_unlocked_is_a_new_unlocking(tmp_path, path):
    h = House(tmp_path)
    h.jammed = True
    h.set(LOCK, "unlocked")
    h.wait(60 + LOCK_SETTLE_S)
    assert h.locked == 1 and len(h.told) == 1
    h.set(LOCK, "locked")  # by hand
    h.jammed = False
    for state in path:
        h.set(LOCK, state)
    h.wait(60)
    assert h.locked == 2 and len(h.told) == 1


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


def test_a_sensor_gone_quiet_while_the_door_is_open_never_gets_it_locked(tmp_path):
    """Its last word was open: nothing is locked until it's back and says shut, and the maintainers are told once."""
    h = House(tmp_path)
    h.set(LOCK, "unlocked")
    h.set(DOOR, "on")
    h.wait(300)  # propped open
    h.set(DOOR, "unavailable")
    h.wait(SENSOR_GRACE_S - 1)
    assert h.locked == 0 and h.told == []  # a minute out, as Home Assistant restarting is, isn't told
    h.wait(600)
    assert h.locked == 0 and len(h.told) == 1
    assert "sensor isn't answering (unavailable)" in h.told[0] and "won't lock it until it's back" in h.told[0]
    assert "It's unlocked now" in h.told[0] and "own auto-lock is the backstop" in h.told[0]
    h.set(DOOR, "on")  # back, and still open
    assert len(h.told) == 2 and "answering again" in h.told[1]
    h.wait(600)
    assert h.locked == 0
    h.set(DOOR, "off")
    h.wait(9)
    assert h.locked == 0
    h.wait(1)
    assert h.locked == 1


def test_a_sensor_out_as_it_was_unlocked_counts_the_door_shut_from_when_its_back(tmp_path):
    """It may have opened and shut unseen: back and shut, it's locked after_shut_s later, not by the clock before."""
    h = House(tmp_path)
    h.set(DOOR, "unknown")
    h.set(LOCK, "unlocked")
    h.wait(30 * 60)
    assert h.locked == 0 and len(h.told) == 1 and "sensor isn't answering (unknown)" in h.told[0]
    h.set(DOOR, "off")
    h.wait(9)
    assert h.locked == 0
    h.wait(1)
    assert h.locked == 1 and "answering again" in h.told[1]


def test_a_sensor_that_flickers_out_for_a_moment_isnt_told(tmp_path):
    h = House(tmp_path)
    h.set(DOOR, "unavailable")
    h.wait(10)
    h.set(DOOR, "off")
    h.wait(SENSOR_GRACE_S * 2)
    assert h.told == []


@pytest.mark.parametrize("read", ["unknown", "unavailable", ""])
def test_the_door_read_as_anything_but_shut_just_before_locking_isnt_locked(tmp_path, read):
    h = House(tmp_path)
    h.set(LOCK, "unlocked")
    h.states[DOOR] = read  # its word hasn't come over the websocket
    h.wait(60 + SENSOR_GRACE_S)
    assert h.locked == 0 and len(h.told) == 1 and "sensor isn't answering" in h.told[0]
    h.set(DOOR, "off")
    h.wait(10)
    assert h.locked == 1


def test_a_door_that_cant_be_read_just_before_locking_isnt_locked_and_is_tried_again_then_told(tmp_path):
    """Home Assistant not answering the read: tried MAX_TRIES times, RETRY_S apart, then the maintainers are told
    the real reason, and nothing more until it's next unlocked."""
    h = House(tmp_path)
    h.set(LOCK, "unlocked")
    h.states[DOOR] = "error"
    h.wait(60)
    assert h.locked == 0 and h.auto.tries == 1
    h.wait(RETRY_S[0])
    assert h.auto.tries == 2
    h.wait(RETRY_S[1] - 1)
    assert h.auto.tries == 2 and h.told == []
    h.wait(1)
    assert h.auto.tries == MAX_TRIES and h.locked == 0 and len(h.told) == 1
    assert "I couldn't read its sensor to be sure it's shut (Home Assistant didn't answer)" in h.told[0]
    assert "bolt" not in h.told[0]
    h.states[DOOR] = "off"
    h.wait(600)
    assert h.auto.tries == MAX_TRIES and h.locked == 0 and len(h.told) == 1
    h.set(LOCK, "locked")  # by hand
    h.set(LOCK, "unlocked")
    h.wait(60)
    assert h.locked == 1


def test_a_door_read_again_once_home_assistant_answers_is_locked(tmp_path):
    h = House(tmp_path)
    h.set(LOCK, "unlocked")
    h.states[DOOR] = "error"
    h.wait(60)
    h.states[DOOR] = "off"
    h.wait(RETRY_S[0])
    assert h.locked == 1 and h.told == []


def test_a_door_shut_while_home_assistant_wasnt_followed_is_read_once_its_followed_again(tmp_path):
    h = House(tmp_path)
    h.set(LOCK, "unlocked")
    h.set(DOOR, "on")
    h.wait(5)
    h.states[DOOR] = "off"  # shut while the websocket was down: no change heard
    h.auto.subscribed()  # following again
    h.wait(1)
    assert h.auto.door_state == "off" and h.locked == 0
    h.wait(9)
    assert h.locked == 0
    h.wait(1)
    assert h.locked == 1  # after_shut_s from when it was read shut
    h.wait(300)
    assert h.said == []  # not "open for 2 minutes" for a door that's shut


def test_a_lock_unlocked_while_home_assistant_wasnt_followed_counts_from_when_its_read(tmp_path):
    h = House(tmp_path)
    h.wait(5)
    h.states[LOCK] = "unlocked"  # Auto Unlock as someone walked past, unheard
    h.auto.subscribed()
    h.wait(1 + 59)
    assert h.locked == 0
    h.wait(1)
    assert h.locked == 1


def test_a_lock_locked_by_hand_unheard_isnt_told_as_one_that_didnt_lock(tmp_path):
    h = House(tmp_path)
    h.jammed = True
    h.set(LOCK, "unlocked")
    h.wait(60)
    h.states[LOCK] = "locked"  # turned by hand as the websocket dropped
    h.auto.subscribed()
    h.wait(LOCK_SETTLE_S * 2)
    assert h.locked == 1 and h.told == []


def test_both_are_read_again_every_half_minute_besides(tmp_path):
    """A change missed with nothing saying following dropped: caught within RESYNC_S."""
    h = House(tmp_path)
    h.set(LOCK, "unlocked")
    h.set(DOOR, "on")
    h.states[DOOR] = "off"
    h.wait(RESYNC_S + 10)
    assert h.locked == 1


def test_a_read_again_never_undoes_a_newer_change(tmp_path):
    """The door opens as it's read: what the read found shut is older than the change heard, and isn't taken."""
    h = House(tmp_path)
    h.set(LOCK, "unlocked")
    h.set(DOOR, "on")
    h.set(DOOR, "off")
    h.wait(5)
    read = h.read

    def opening_as_its_read(entity):
        found = read(entity)
        if entity == DOOR:
            h.set(DOOR, "on")
        return found

    h.auto._read = opening_as_its_read
    h.auto.subscribed()
    h.wait(1)
    h.auto._read = read
    assert h.auto.door_state == "on"
    h.wait(600)
    assert h.locked == 0


def test_tars_starting_with_the_door_open_and_unlocked_waits_for_it_to_shut(tmp_path):
    h = House(tmp_path, door="on", lock="unlocked")
    h.wait(119)
    assert h.locked == 0 and h.said == []
    h.wait(1)
    assert h.said == ["The front door has been open for 2 minutes."]
    h.set(DOOR, "off")
    h.wait(10)
    assert h.locked == 1


def test_tars_starting_with_home_assistant_unreachable_locks_nothing_until_it_answers(tmp_path):
    h = House(tmp_path, door="error", lock="error")
    h.wait(SENSOR_GRACE_S)
    assert h.locked == 0 and len(h.told) == 1
    assert "sensor isn't answering (I couldn't read it: Home Assistant didn't answer)" in h.told[0]
    h.states.update({DOOR: "off", LOCK: "unlocked"})  # it's back
    h.auto.subscribed()
    h.wait(1)
    assert len(h.told) == 2 and "answering again" in h.told[1]
    h.wait(59)
    assert h.locked == 0
    h.wait(1)
    assert h.locked == 1


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
