"""The speakerphone's own mute, as TARS's: found from its HID report descriptor, set off-hook with mute or without
(on-hook, the PowerConf ignores the computer's mute), and followed when its button is pressed; one mute, whichever
button worked it."""

import os

import pytest

from voice_assistant.speakerphone import Bit, Hid, Layout, SpeakerphoneMute, find_mute, layout

from .test_buttons import POWERCONF_HID

POWERCONF = Layout(button=Bit(2, 2), mute=Bit(2, 1), off_hook=Bit(2, 0), output_bytes=2)
# Its mute button pressed and let go, as it sent them on the Pi (report 2; its other bits are always on).
PRESSED, LET_GO = bytes.fromhex("020700"), bytes.fromhex("020300")


def test_the_powerconf_s_mute_is_read_from_its_descriptor():
    assert layout(POWERCONF_HID.descriptor) == POWERCONF


def test_a_device_without_a_phone_mute_and_its_lights_has_none():
    volume_only = bytes.fromhex("050c0901a1018501150025010 9e909ea75019502810 2750195068101c0".replace(" ", ""))
    assert layout(volume_only) is None
    assert layout(b"") is None


@pytest.fixture
def mute(tmp_path):
    """The PowerConf's mute, its reports written to a file instead."""
    changes = []
    device = tmp_path / "hidraw0"
    device.write_bytes(b"")
    found = SpeakerphoneMute(str(device), POWERCONF, changes.append)
    found.changes, found.device = changes, device
    yield found
    os.close(found._fd)


def test_it_s_muted_and_unmuted_as_its_own_button_does_and_each_change_is_told(mute):
    assert mute.toggle() == "muted" and mute.muted
    assert mute.toggle() == "unmuted" and not mute.muted
    # Off-hook with mute, then off-hook without: as the PowerConf took them on the Pi, its own mute undone too.
    assert mute.device.read_bytes() == bytes.fromhex("020300020100")
    assert mute.changes == [True, False]
    mute.set(False)  # already unmuted: written again, nothing told
    assert mute.changes == [True, False]


def test_its_button_pressed_flips_it_and_letting_go_does_nothing(mute):
    assert mute.pressed(PRESSED) and mute.muted
    assert not mute.pressed(PRESSED)  # the same press, reported again
    assert not mute.pressed(LET_GO) and mute.muted
    assert mute.pressed(PRESSED) and not mute.muted
    assert not mute.pressed(bytes.fromhex("01400000"))  # its volume keys' report
    assert mute.changes == [True, False]


def test_the_remote_and_its_own_button_are_one_mute(mute):
    mute.toggle()  # the remote mutes it
    mute.pressed(PRESSED)  # its own button unmutes it
    mute.pressed(LET_GO)
    assert not mute.muted and mute.toggle() == "muted"  # and the remote mutes it again


def test_found_by_the_speakerphone_s_name_and_unmuted_to_start_with(tmp_path):
    device = tmp_path / "hidraw0"
    device.write_bytes(b"")
    hid = Hid(str(device), "Anker PowerConf", POWERCONF_HID.descriptor)
    found, why = find_mute("powerconf", print, lambda: [hid])
    assert found and why == "" and device.read_bytes() == bytes.fromhex("020100")  # off-hook, unmuted
    os.close(found._fd)
    assert find_mute("", print, lambda: [hid]) == (None, "[audio] output_device doesn't name the speakerphone")
    assert find_mute("Jabra", print, lambda: [hid])[1] == "no HID device named like 'Jabra'"
    plain = Hid(str(device), "Anker PowerConf", b"")
    assert find_mute("PowerConf", print, lambda: [plain])[1] == "Anker PowerConf has no mute button with a mute light"


def test_one_it_may_not_use_says_which_rule_lets_it(tmp_path):
    device = tmp_path / "hidraw0"
    device.write_bytes(b"")
    device.chmod(0)
    if os.access(device, os.R_OK):
        pytest.skip("running as root: nothing is unreadable")
    found, why = find_mute("PowerConf", print, lambda: [Hid(str(device), "Anker PowerConf", POWERCONF_HID.descriptor)])
    assert found is None and "99-tars-speakerphone.rules" in why
