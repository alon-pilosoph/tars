"""The IR remotes: Samsung's TV and Polk's soundbar codes made exactly as published, and on and off sent the way the
set takes them."""

import json

from examples.my_house.ac_onboard import decode
from examples.my_house.ir import CODE_SETS, Remote, nec_pulses


def bits(data: list[int]) -> str:
    """As the onboarding decoder reads a press: each byte lowest bit first."""
    return "".join(format(byte, "08b")[::-1] for byte in data)


def test_samsungs_codes_are_device_7_twice_then_the_button_and_its_inverse():
    d = decode(CODE_SETS["samsung-tv"].pulses("volume_up"))
    assert d and d.bits == bits([0x07, 0x07, 0x07, 0xF8]) and d.timing["header"] == [4500, 4500]


def test_polks_codes_are_its_address_then_the_button_and_its_inverse():
    d = decode(CODE_SETS["polk-re9641"].pulses("mute"))
    assert d and d.bits == bits([0xC8, 0x91, 0x20, 0xDF]) and d.timing["header"] == [9000, 4500]


def test_a_code_is_a_header_32_bits_and_a_final_mark():
    pulses = nec_pulses([0, 0xFF, 1, 2], (9000, 4500))
    assert len(pulses) == 2 + 64 + 1 and pulses[-1] == 560


def test_a_remote_presses_a_button_as_many_times_as_asked():
    sent = []
    remote = Remote("soundbar", CODE_SETS["polk-re9641"], sent.append, sleep=lambda s: None)
    said = remote.call("press", {"button": "volume up", "times": 4})
    assert said.startswith("sent: volume_up 4 times") and len(sent) == 4
    assert sent[0] == CODE_SETS["polk-re9641"].pulses("volume_up")


def test_a_remote_says_what_it_has_rather_than_guess():
    sent = []
    soundbar = Remote("soundbar", CODE_SETS["polk-re9641"], sent.append)
    assert soundbar.call("press", {"button": "netflix"}).startswith("error: the soundbar's buttons are")
    assert "only has a power button" in soundbar.call("off", {})
    assert soundbar.call("press", {"button": "power"}).startswith("sent: power")  # its only power: a button


def samsung(tmp_path, works=None, seems_on=None):
    sent, tested = [], tmp_path / "tv.json"
    if works is not None:
        tested.write_text(json.dumps(works))
    tv = Remote("tv", CODE_SETS["samsung-tv"], sent.append, tested=tested, seems_on=seems_on)
    return tv, sent


def sent_buttons(sent):
    codes = CODE_SETS["samsung-tv"]
    return [next(b for b in codes.buttons if codes.pulses(b) == pulses) for pulses in sent]


def test_the_tv_turns_on_and_off_with_the_codes_that_only_do_that(tmp_path):
    tv, sent = samsung(tmp_path)
    assert "not tried on this TV yet" in tv.call("on", {})
    tv.call("off", {})
    assert sent_buttons(sent) == ["on", "off"]
    assert "power" not in tv.codes.presses() and "on" not in tv.codes.presses()


def test_a_tv_that_ignores_them_gets_the_power_button_only_when_its_the_other_way(tmp_path):
    state = {"on": False}
    tv, sent = samsung(tmp_path, {"on": False, "off": True, "power": True}, seems_on=lambda: state["on"])
    assert tv.call("on", {}) == "sent: the power button, to turn it on (IR can't confirm it)"
    state["on"] = True
    assert tv.call("on", {}).startswith("already on") and sent_buttons(sent) == ["power"]
    assert "it works on this TV" in tv.call("off", {})


def test_with_only_a_toggle_and_no_way_to_tell_it_sends_nothing(tmp_path):
    tv, sent = samsung(tmp_path, {"on": False, "off": False, "power": True})
    assert "can't tell if it's on" in tv.call("on", {}) and sent == []


def test_a_tv_with_on_and_off_codes_has_them_as_actions_and_a_soundbar_doesnt(tmp_path):
    tv, _ = samsung(tmp_path)
    soundbar = Remote("soundbar", CODE_SETS["polk-re9641"], lambda pulses: None)
    assert tv.actions == ("status", "press", "on", "off") and soundbar.actions == ("status", "press")
    assert "volume and mute are its" in soundbar.rules()


def test_check_says_when_the_on_and_off_codes_were_never_tried(tmp_path):
    tv, _ = samsung(tmp_path)
    (untried,) = tv.check()
    assert untried.status == "warn" and "ir test" in untried.fix
    tried, _ = samsung(tmp_path, {"on": True, "off": True, "power": True})
    assert tried.check() == []
