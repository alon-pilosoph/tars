"""Buttons: the speakerphone's own keys and a remote's codes turn TARS's speaker a step a press, a held one a step
every REPEAT_S; talk, stop and mute count once a press however long they're held (debounced); an HDMI port's
remote and the speakerphone's own codes are never taken for the remote's."""

import dataclasses
import os
from pathlib import Path

import pytest

from voice_assistant import check
from voice_assistant.buttons import (
    DOWN,
    EV_KEY,
    EV_MSC,
    EVENT,
    HELD,
    KEY_VOLUMEDOWN,
    KEY_VOLUMEUP,
    MSC_SCAN,
    MUTE,
    PRESSED,
    RELEASE_S,
    REPEAT_S,
    STOP,
    TALK,
    UP,
    Buttons,
    Input,
    Listener,
    inputs,
)
from voice_assistant.config import HomeConfig, load_config
from voice_assistant.speakerphone import Hid
from voice_assistant.volume import QUIET, Alsa, OwnVolume

from .test_volume import FakeAmixer

POWERCONF = Input("/dev/input/event0", "Anker PowerConf Consumer Control", remote=False)
POWERCONF_HID = Hid(
    "/dev/hidraw0",
    "Anker PowerConf",
    bytes.fromhex(
        "050c0901a1018501150025010 9e909ea09e209cd09b509b609b309b77501950881 42c0050b0905a1018502050b150025010920"
        "0997750195028123092f0921097009507501950481070906a10219b029bb1500250c7504950181 40c00907150025010509750195"
        "018102750195058101050815002501091709090918092009217501950591 22050b15002501099e750195019122750195 0a9101c0"
        "0600ff0901a101850309011500 26ff00953f750881020901150026ff00953f75089102c0".replace(" ", "")
    ),
)
RECEIVER = Input("/dev/input/event2", "gpio_ir_recv", remote=True)
REMOTE = {"volume_up": 0x80, "volume_down": 0x81, "talk": 0xCE, "stop": 0x90, "mute": 0x9D}  # the Argon remote's


def key(buttons: Buttons, code: int, value: int, now: float):
    return buttons.feed(POWERCONF, EV_KEY, code, value, now)


def scan(buttons: Buttons, code: int, now: float, source: Input = RECEIVER):
    return buttons.feed(source, EV_MSC, MSC_SCAN, code, now)


def test_the_speakerphone_s_keys_turn_it_a_step_a_press_and_letting_go_does_nothing():
    buttons = Buttons(REMOTE)
    assert key(buttons, KEY_VOLUMEUP, PRESSED, 0.0) == UP
    assert key(buttons, KEY_VOLUMEUP, 0, 0.05) is None
    assert key(buttons, KEY_VOLUMEUP, PRESSED, 0.1) == UP  # pressed again, quickly: a press is never a repeat
    assert key(buttons, KEY_VOLUMEDOWN, PRESSED, 0.2) == DOWN


def test_a_held_key_turns_it_a_step_every_repeat_s_not_a_step_a_repeat():
    buttons = Buttons(REMOTE)
    assert key(buttons, KEY_VOLUMEDOWN, PRESSED, 0.0) == DOWN
    steps = [key(buttons, KEY_VOLUMEDOWN, HELD, t / 30) for t in range(1, 30)]  # a second of repeats
    assert 2 <= steps.count(DOWN) <= round(1 / REPEAT_S) and set(steps) == {DOWN, None}


def test_the_remote_s_codes_turn_it_and_its_repeats_a_step_every_repeat_s():
    buttons = Buttons(REMOTE)
    assert scan(buttons, 0x80, 0.0) == UP
    assert scan(buttons, 0x80, 0.1) is None  # held: the remote says it again
    assert scan(buttons, 0x80, REPEAT_S + 0.01) == UP
    assert scan(buttons, 0x81, REPEAT_S + 0.05) == DOWN  # the other way counts at once
    assert scan(buttons, 0x42, 5.0) is None  # another button on the remote


@pytest.mark.parametrize("button, code", [(TALK, 0xCE), (STOP, 0x90), (MUTE, 0x9D)])
def test_talk_stop_and_mute_count_once_a_press_however_long_they_re_held(button, code):
    buttons = Buttons(REMOTE)
    held = [scan(buttons, code, t * 0.11) for t in range(30)]  # held 3 s: the remote repeats it every 0.11 s
    assert held == [button] + [None] * 29
    assert scan(buttons, code, 29 * 0.11 + RELEASE_S / 2) is None  # a repeat a little late is still the same press
    assert scan(buttons, code, 29 * 0.11 + RELEASE_S / 2 + RELEASE_S) == button  # let go, then pressed again


def test_a_double_press_counts_twice_once_the_button_was_let_go_between():
    buttons = Buttons(REMOTE)
    assert scan(buttons, 0x9D, 0.0) == MUTE
    assert scan(buttons, 0x9D, 0.11) is None
    assert scan(buttons, 0x9D, 0.11 + RELEASE_S) == MUTE
    assert scan(buttons, 0xCE, 0.11 + RELEASE_S + 0.01) == TALK  # another button's press is its own


def test_only_a_receiver_s_codes_are_the_remote_s_never_the_speakerphone_s():
    buttons = Buttons({"volume_up": 0xC00E9})  # the PowerConf's own code for its + key
    assert scan(buttons, 0xC00E9, 0.0, source=POWERCONF) is None


def test_with_volume_keys_off_only_the_remote_turns_it():
    buttons = Buttons(REMOTE, keys=False)
    assert key(buttons, KEY_VOLUMEUP, PRESSED, 0.0) is None
    assert scan(buttons, 0x81, 1.0) == DOWN


def device(sys_input: Path, event: str, name: str, under: str, protocols: str | None = None) -> None:
    folder = sys_input.parent / "devices" / under / event
    folder.mkdir(parents=True)
    (folder / "name").write_text(name + "\n")
    if protocols is not None:
        (folder.parent / "protocols").write_text(protocols + "\n")
    (sys_input / event).mkdir(parents=True)
    (sys_input / event / "device").symlink_to(folder)


def test_a_remote_s_receiver_is_an_rc_device_that_isn_t_hdmi_cec(tmp_path):
    sys_input = tmp_path / "class" / "input"
    device(sys_input, "event0", "Anker PowerConf Consumer Control", "usb1/1-1.2")
    device(sys_input, "event2", "gpio_ir_recv", "ir-receiver@17/rc/rc0", "rc-5 [nec] [rc-6] [lirc]")
    device(sys_input, "event10", "vc4-hdmi-0", "fef00700.hdmi/rc/rc1", "[cec]")
    found = inputs(sys_input, Path("/dev/input"))
    assert found == [
        Input("/dev/input/event0", "Anker PowerConf Consumer Control", False),
        Input("/dev/input/event2", "gpio_ir_recv", True),
        Input("/dev/input/event10", "vc4-hdmi-0", False),
    ]


def events(*each: tuple[int, int, int]) -> bytes:
    return b"".join(EVENT.pack(0, 0, kind, code, value) for kind, code, value in each)


def test_the_listener_turns_the_speaker_for_what_a_device_says_and_logs_it():
    read, write = os.pipe()
    os.set_blocking(read, False)
    pressed, said = [], []
    act = {DOWN: lambda: pressed.append(DOWN) or "done", MUTE: lambda: pressed.append(MUTE) or "muted"}
    listener = Listener(Buttons(REMOTE), act, said.append, list)
    listener.open[read] = RECEIVER
    os.write(write, events((EV_MSC, MSC_SCAN, 0x81), (0, 0, 0), (EV_MSC, MSC_SCAN, 0xCE), (EV_MSC, MSC_SCAN, 0x9D)))
    listener.read(read)
    assert pressed == [DOWN, MUTE]  # talk has nothing to do here: skipped
    assert said == ["(buttons: gpio_ir_recv, volume down: done)", "(buttons: gpio_ir_recv, mute: muted)"]
    listener.read(read)  # nothing more to read
    assert pressed == [DOWN, MUTE]
    os.close(write)
    os.close(read)


def test_a_device_unplugged_is_let_go_and_a_failed_turn_never_stops_the_listening():
    read, write = os.pipe()
    listener = Listener(Buttons(REMOTE), {UP: lambda: 1 / 0}, lambda line: None, list)
    listener.open[read] = POWERCONF
    os.write(write, events((EV_KEY, KEY_VOLUMEUP, PRESSED)))
    listener.read(read)  # the turn raised: logged, and the device kept
    assert read in listener.open
    os.close(read)
    listener.read(read)  # read from a closed one: as an unplugged device is
    assert read not in listener.open
    os.close(write)


def test_a_device_it_may_not_read_is_said_once(tmp_path):
    locked = tmp_path / "event0"
    locked.write_text("")
    locked.chmod(0)
    if os.access(locked, os.R_OK):
        pytest.skip("running as root: nothing is unreadable")
    said = []
    listener = Listener(Buttons(REMOTE), {}, said.append, lambda: [Input(str(locked), "PowerConf", False)])
    listener.rescan()
    listener.rescan()
    assert len(said) == 1 and "input group" in said[0]


def test_a_button_turns_it_as_asking_does_never_below_quiet():
    speaker = OwnVolume(Alsa.on_card(2, FakeAmixer(QUIET + 5)))
    assert speaker.turn(False).startswith("done: TARS's speaker turned down") and speaker.mixer.level()[0] == QUIET
    assert "as low as it goes" in speaker.turn(False)
    assert speaker.turn(True) and speaker.mixer.level()[0] == QUIET + 10


def config(output_device: str = "PowerConf", **home):
    defaults = load_config(Path("/nonexistent/config.toml"))
    audio = dataclasses.replace(defaults.audio, output_device=output_device)
    return dataclasses.replace(defaults, home=HomeConfig(**home), audio=audio)


def test_check_says_what_the_buttons_do_and_what_s_missing():
    found = [POWERCONF, RECEIVER]
    hid = [POWERCONF_HID]
    linux = {"system": "Linux", "readable": lambda path, mode: True, "find_hids": lambda: hid}
    assert check.buttons(config(speakerphone_mute=False), lambda: found, **linux).status == check.SKIP
    on = config(own_speaker=True, remote={"volume_up": 0x80, "volume_down": 0x81})
    assert check.buttons(on, lambda: found, system="Darwin").status == check.SKIP
    pressed, muting = check.buttons(on, lambda: found, **linux)
    assert pressed.status == check.OK
    assert pressed.detail == "volume keys and the remote on gpio_ir_recv (volume_up 0x80, volume_down 0x81)"
    assert muting.status == check.OK and muting.detail.startswith("Anker PowerConf's")
    no_receiver, _ = check.buttons(on, lambda: [POWERCONF], **linux)
    assert no_receiver.status == check.WARN and "dtoverlay=gpio-ir" in no_receiver.fix
    locked, locked_mute = check.buttons(on, lambda: found, system="Linux", readable=lambda path, mode: False,
                                        find_hids=lambda: hid)  # fmt: skip
    assert locked.status == locked_mute.status == check.FAIL and "input group" in locked.fix
    assert "99-tars-speakerphone.rules" in locked_mute.fix
    no_mute = check.buttons(config(), find_hids=list, **{k: v for k, v in linux.items() if k != "find_hids"})
    assert [r.status for r in no_mute] == [check.WARN] and "no HID device" in no_mute[0].detail


def test_every_event_read_is_shown_as_it_is_for_reading_a_remote_s_codes():
    read, write = os.pipe()
    os.set_blocking(read, False)
    shown = []
    listener = Listener(Buttons({}, keys=False), {}, print, list, each=lambda *event: shown.append(event))
    listener.open[read] = RECEIVER
    os.write(write, events((EV_MSC, MSC_SCAN, -2147483520)))  # 0x80000080, as the kernel's signed value
    listener.read(read)
    assert shown == [(RECEIVER, EV_MSC, MSC_SCAN, 0x80000080)]
    os.close(write)
    os.close(read)
