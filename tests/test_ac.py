"""The AC: onboarding reads a remote's presses and works out its code from them alone, checked against every press;
TARS then makes codes the way that remote does, and toggles the power only when it has to."""

import json
import random
from pathlib import Path

import pytest

from voice_assistant.ac import (
    AC,
    DISTANCE,
    MANCHESTER,
    Profile,
    Settings,
    State,
    frame_pulses,
    with_checksum,
)
from voice_assistant.ac_onboard import STEPS, Capture, _load_captures, capture, decode, fit, parse_screen
from voice_assistant.home import HomeError

ELECTRA_MODES = {"cool": 1, "heat": 2, "auto": 3, "dry": 4, "fan": 5}
ELECTRA_FANS = {"low": 0, "medium": 1, "high": 2, "auto": 3}
ELECTRA_TIMING = {"unit": 1000, "header": [3, 3], "trailer": 4, "one": "01", "gap": 0}


def electra_bits(s: Settings, power: bool, hide_fan_mode_temperature: bool = False) -> str:
    """The 34 bits Electra's remotes send, as published (github.com/barakwei/IRelectra), first bit first."""
    temperature = 0 if hide_fan_mode_temperature and s.mode == "fan" else s.temperature - 15
    value = (
        power << 33
        | ELECTRA_MODES[s.mode] << 30
        | ELECTRA_FANS[s.fan] << 28
        | s.swing << 25
        | temperature << 19
        | 1 << 1
    )
    return format(value, "034b")


def captured(pulses: list[int], rng: random.Random) -> list[int]:
    """As a Tuya blaster hears a remote: marks a little long, spaces a little short, none exactly."""
    return [max(50, p + (60 if i % 2 == 0 else -60) + rng.randint(-40, 40)) for i, p in enumerate(pulses)]


def press_through_the_steps(bits_for, scheme=MANCHESTER, timing=ELECTRA_TIMING, repeats=3) -> list[Capture]:
    """Every onboarding step, pressed on a remote that writes `bits_for(settings, power)`."""
    rng, screen, captures = random.Random(7), Settings("cool", 23, "auto"), []
    for step in STEPS:
        if step.setup.startswith("Set it to"):
            screen = Settings(screen.mode, int(step.setup.split()[3]), screen.fan, screen.swing)
        screen = step.expect(step.first(screen))
        pulses = captured(frame_pulses(bits_for(screen, step.power), scheme, timing, repeats), rng)
        captures.append(Capture(screen, step.power, pulses, step.name))
    return captures


def electra_profile() -> dict:
    result = fit(press_through_the_steps(electra_bits))
    assert result.profile, result.report
    return result.profile


# Reading presses.


def test_a_manchester_press_reads_back_as_its_bits_whatever_the_first_and_last_bit():
    rng = random.Random(1)
    for bits in ("1" + "0" * 32 + "1", "0" * 33 + "1", "0110" * 8 + "01", "1" * 34):
        pulses = captured(frame_pulses(bits, MANCHESTER, ELECTRA_TIMING, 3), rng)
        d = decode(pulses)
        assert d and (d.scheme, d.bits, d.repeats) == (MANCHESTER, bits, 3)
        assert d.timing["header"] == [3, 3] and d.timing["trailer"] == 4


def test_frames_with_a_gap_between_them_read_as_repeats():
    timing = {**ELECTRA_TIMING, "gap": 20000}
    d = decode(captured(frame_pulses("1100" * 8, MANCHESTER, timing, 2), random.Random(2)))
    assert d and d.bits == "1100" * 8 and d.repeats == 2 and 19000 < d.timing["gap"] < 21000


def test_a_pulse_distance_press_reads_back_as_its_bits():
    timing = {"header": [9000, 4500], "mark": 560, "zero": 560, "one": 1690, "trailer": 560, "gap": 40000}
    bits = "1100101000001111" * 2
    d = decode(captured(frame_pulses(bits, DISTANCE, timing, 2), random.Random(3)))
    assert d and (d.scheme, d.bits, d.repeats) == (DISTANCE, bits, 2)


def test_a_cut_off_press_doesnt_read():
    pulses = frame_pulses("1" * 34, MANCHESTER, ELECTRA_TIMING, 3)
    assert decode(pulses[5:]) is None and decode(pulses[:8]) is None


# Working out the code.


def test_electras_layout_is_found_from_the_presses_alone():
    profile = electra_profile()
    fields = profile["fields"]
    assert fields["power"]["at"] == [0] and fields["power"]["values"] == {"press": "1", "none": "0"}
    assert fields["mode"]["values"] == {"cool": "001", "heat": "010", "auto": "011", "dry": "100", "fan": "101"}
    assert fields["fan"]["values"] == {"auto": "11", "low": "00", "medium": "01", "high": "10"}
    assert sorted(int(t) for t in fields["temperature"]["values"]) == list(range(16, 31))
    p = Profile(profile)
    for t in range(16, 31):  # every temperature, heard or only counted
        s = Settings("heat", t, "high", True)
        assert p.bits(s, True) == electra_bits(s, True)


def test_every_code_tars_makes_reads_back_as_the_remote_would_send_it():
    p = Profile(electra_profile())
    for s, toggle in [(Settings("cool", 22, "low"), False), (Settings("dry", 27, "auto", True), True)]:
        d = decode(p.pulses(s, toggle))
        assert d and d.bits == electra_bits(s, toggle) and d.repeats == 3


def test_a_mode_that_writes_the_temperature_its_own_way_is_left_out_not_sent_wrong():
    result = fit(press_through_the_steps(lambda s, power: electra_bits(s, power, hide_fan_mode_temperature=True)))
    assert result.profile and "fan" not in result.profile["fields"]["mode"]["values"]
    assert any("fan mode is left out" in line for line in result.report)


def test_a_checksum_in_the_last_byte_is_found_and_made():
    modes = {"cool": 1, "heat": 2, "auto": 3, "dry": 4, "fan": 5}
    fans = {"low": 0, "medium": 1, "high": 2, "auto": 3}

    def bits_for(s: Settings, power: bool) -> str:
        first = format(power << 7 | modes[s.mode] << 4 | fans[s.fan] << 2 | s.swing << 1, "08b")
        frame = "11000011" + first + format(s.temperature - 16, "05b") + "000" + "00000000"
        return with_checksum(frame, {"order": "lsb"})

    timing = {"header": [9000, 4500], "mark": 560, "zero": 560, "one": 1690, "trailer": 560, "gap": 40000}
    result = fit(press_through_the_steps(bits_for, DISTANCE, timing, 2))
    assert result.profile and result.profile["checksum"] == {"kind": "sum8", "order": "lsb"}
    p = Profile(result.profile)
    s = Settings("heat", 19, "medium")
    assert p.bits(s, False) == bits_for(s, False)


def test_a_bit_that_follows_no_setting_means_no_profile():
    counter = iter(range(1000))
    result = fit(press_through_the_steps(lambda s, power: electra_bits(s, power)[:-3] + str(next(counter) % 2) + "10"))
    assert result.profile is None


def test_without_a_power_press_there_is_no_profile():
    captures = [c for c in press_through_the_steps(electra_bits) if not c.power]
    result = fit(captures)
    assert result.profile is None and any("power" in line for line in result.report)


def test_a_typo_on_the_screen_is_caught_not_learned():
    captures = press_through_the_steps(electra_bits)
    captures[2].settings = Settings("cool", 25, "auto")  # said 25; the remote sent 24
    result = fit(captures)
    assert result.profile is None and "different codes" in result.report[-1]


@pytest.mark.parametrize(
    ("typed", "settings"),
    [
        ("", Settings("cool", 24, "auto")),
        ("heat 26 high swing", Settings("heat", 26, "high", True)),
        ("fan - 2", Settings("fan", 24, "medium")),
        ("dry 22 a noswing", Settings("dry", 22, "auto")),
    ],
)
def test_what_the_screen_shows_is_read(typed, settings):
    assert parse_screen(typed, Settings("cool", 24, "auto")) == settings


@pytest.mark.parametrize("typed", ["freeze 24 auto", "cool hot auto", "cool 24 turbo", "cool 24 auto swing extra x"])
def test_a_screen_it_cant_read_says_why(typed):
    with pytest.raises(ValueError):
        parse_screen(typed, Settings())


def test_capturing_with_every_guess_taken_gives_a_profile(tmp_path, monkeypatch):
    """The whole capture, as someone does it who only presses Enter: the remote does what the steps say it will."""
    rng, screen = random.Random(5), [Settings("cool", 23, "auto")]
    steps = iter(STEPS)

    class Remote:
        def learn(self, timeout_s=20.0):
            step = next(steps)
            if step.setup.startswith("Set it to"):
                screen[0] = Settings("cool", int(step.setup.split()[3]), "auto")
            screen[0] = step.expect(step.first(screen[0]))
            return captured(frame_pulses(electra_bits(screen[0], step.power), MANCHESTER, ELECTRA_TIMING, 3), rng)

    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    capture(Remote(), tmp_path, again=None)
    saved = json.loads((tmp_path / "captures.json").read_text())
    assert len(saved) == len(STEPS) and fit(_load_captures(tmp_path)).profile


# The AC in use.


class Sent:
    def __init__(self, fail=False):
        self.codes, self.fail = [], fail

    def __call__(self, pulses):
        if self.fail:
            raise HomeError("the IR blaster didn't answer")
        d = decode(pulses)
        self.codes.append((d.bits[0] == "1", d.bits))


def ac(tmp_path: Path, sensor=None, sent=None) -> tuple[AC, Sent, Path]:
    sent = sent or Sent()
    path = tmp_path / "state.json"
    hass = None
    if sensor is not None:
        hass = type("Hass", (), {"state": lambda self, entity: {"state": sensor()}})()
    clock = [0.0]

    def sleep(s):
        clock[0] += s

    unit = AC(Profile(electra_profile()), sent, path, hass, "binary_sensor.ac_light" * (sensor is not None))
    unit._clock, unit._sleep = (lambda: clock[0]), sleep
    return unit, sent, path


NOTHING = {"mode": None, "temperature": None, "fan": None}


def test_on_toggles_only_when_tars_thinks_its_off(tmp_path):
    unit, sent, path = ac(tmp_path)
    said = unit.call("on", {**NOTHING, "temperature": 22})
    assert said.startswith("sent: turn on, set to cool 22, fan auto") and sent.codes[-1][0] is True
    assert State.load(path).on
    said = unit.call("on", {**NOTHING, "temperature": 23})
    assert said.startswith("sent: cool 23") and sent.codes[-1][0] is False  # already on: settings only


def test_off_when_already_off_sends_nothing(tmp_path):
    unit, sent, _ = ac(tmp_path)
    assert unit.call("off", NOTHING).startswith("already off") and sent.codes == []


def test_set_while_off_says_so_and_sends_nothing(tmp_path):
    unit, sent, _ = ac(tmp_path)
    assert unit.call("set", {**NOTHING, "mode": "heat"}).startswith("error: the AC is off") and sent.codes == []


def test_being_told_the_ac_is_on_sends_nothing_and_makes_off_toggle(tmp_path):
    unit, sent, path = ac(tmp_path)
    assert unit.call("is_on", NOTHING) == "noted: the AC is on; nothing was sent" and sent.codes == []
    assert "someone said so" in unit.status()
    unit.call("off", NOTHING)
    assert sent.codes[-1][0] is True and not State.load(path).on


def test_settings_it_has_no_code_for_are_an_error(tmp_path):
    unit, sent, _ = ac(tmp_path)
    assert "from 16 to 30" in unit.call("on", {**NOTHING, "temperature": 35})
    assert "modes cool" in unit.call("on", {**NOTHING, "mode": "turbo"})
    assert sent.codes == []


def test_the_last_settings_are_kept_for_next_time(tmp_path):
    unit, sent, _ = ac(tmp_path)
    unit.call("on", {"mode": "heat", "temperature": 26, "fan": "low"})
    unit.call("off", NOTHING)
    unit.call("on", NOTHING)
    assert sent.codes[-1] == (True, electra_bits(Settings("heat", 26, "low"), True))


def test_with_the_light_sensor_it_checks_and_sends_once_more_if_it_was_missed(tmp_path):
    light = iter(["off"] * 3 + ["off"] * 13 + ["on"] * 10)  # before; missed the first toggle; then on
    unit, sent, path = ac(tmp_path, sensor=lambda: next(light))
    said = unit.call("on", NOTHING)
    assert said == "done: the AC is on (on the second try), its light says so"
    assert [toggle for toggle, _ in sent.codes] == [True, True] and State.load(path).how == "sensor"


def test_with_the_light_sensor_on_already_means_no_toggle(tmp_path):
    unit, sent, _ = ac(tmp_path, sensor=lambda: "on")
    assert unit.call("on", NOTHING).startswith("sent: cool 24") and sent.codes[-1][0] is False
    assert unit.status().startswith("AC: on (its light says so)")


def test_a_blaster_that_doesnt_answer_changes_nothing(tmp_path):
    unit, _, path = ac(tmp_path, sent=Sent(fail=True))
    with pytest.raises(HomeError):
        unit.call("on", NOTHING)
    assert not State.load(path).on


def test_a_profile_from_another_version_is_refused(tmp_path):
    with pytest.raises(ValueError):
        Profile({"scheme": "morse"})
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({**electra_profile(), "fields": {}}))
    with pytest.raises(ValueError, match="no power bit"):
        Profile.load(path)
