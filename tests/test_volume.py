"""TARS's own speaker as a device: its volume read, set and turned up or down, through amixer on the Pi and
AppleScript on a Mac; never muted or set below QUIET; said done only when the level read back is the one set."""

import dataclasses
from pathlib import Path

import pytest

from voice_assistant import volume
from voice_assistant.__main__ import make_home
from voice_assistant.config import HomeConfig, load_config
from voice_assistant.home import PRESS, SET, HomeError, Request
from voice_assistant.volume import (
    DEVICE,
    QUIET,
    Alsa,
    Control,
    MacVolume,
    OwnVolume,
    card_of,
    mixer_for,
    playback_controls,
    preferred,
)

POWERCONF = "Anker PowerConf: USB Audio (hw:2,0)"
# A USB speakerphone's controls, as amixer -c 2 scontents lists them: its microphone first, which is never turned.
SCONTENTS = """Simple mixer control 'Mic',0
  Capabilities: cvolume cvolume-joined cswitch cswitch-joined
  Capture channels: Mono
  Limits: Capture 0 - 16
  Mono: Capture 12 [75%] [12.00dB] [on]
Simple mixer control 'Auto Gain Control',0
  Capabilities: pswitch pswitch-joined
  Playback channels: Mono
  Mono: Playback [on]
Simple mixer control 'PCM',0
  Capabilities: pvolume pswitch pswitch-joined
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 64
  Mono:
  Front Left: Playback 38 [60%] [-12.00dB] [on]
  Front Right: Playback 38 [60%] [-12.00dB] [on]
"""


def sget(percent: int, on: bool = True, capture: bool = False) -> str:
    state = "on" if on else "off"
    also = "  Front Left: Capture 10 [99%] [on]\n" if capture else ""
    return f"""Simple mixer control 'PCM',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 64
  Front Left: Playback 38 [{percent}%] [-12.00dB] [{state}]
  Front Right: Playback 38 [{percent}%] [-12.00dB] [{state}]
{also}"""


class FakeAmixer:
    """amixer on card 2: its PCM level, changed as asked (or, `stuck`, not), and every command it was given."""

    def __init__(self, percent: int = 60, on: bool = True, stuck: bool = False):
        self.percent, self.on, self.stuck, self.ran = percent, on, stuck, []

    def __call__(self, command: list[str]) -> str:
        self.ran.append(command)
        if command[-1] == "scontents":
            return SCONTENTS
        if "sget" in command:
            return sget(self.percent, self.on)
        if self.stuck:
            return ""
        for word in command[command.index("sset") + 2 :]:
            if word.endswith("%"):
                self.percent = int(word[:-1])
            elif word in ("mute", "unmute"):
                self.on = word == "unmute"
        return ""


def config(own_speaker: bool = False):
    """The defaults, as with no config.toml at all."""
    defaults = load_config(Path("/nonexistent/config.toml"))
    return dataclasses.replace(defaults, home=HomeConfig(own_speaker=own_speaker))


def speaker(amixer: FakeAmixer) -> OwnVolume:
    return OwnVolume(Alsa.on_card(2, amixer))


def test_the_card_is_the_one_portaudio_names_the_device_by():
    assert card_of(POWERCONF) == 2
    assert card_of("bcm2835 Headphones: - (hw:0,0)") == 0
    assert card_of("USB Audio Device: Speaker (hw:12,3)") == 12
    assert card_of("default") is card_of("pulse") is card_of("dmix") is None
    assert card_of("(hw:x,0)") is None


def test_only_a_playback_volume_is_ever_turned_never_the_microphone():
    controls = playback_controls(SCONTENTS)
    assert controls == [Control("PCM", 0, True)]
    assert preferred(playback_controls(SCONTENTS.split("Simple mixer control 'PCM'")[0])) is None


def test_one_that_can_be_unmuted_comes_first_then_by_name():
    master = Control("Master", 0, False)
    assert preferred([master, Control("Speaker", 0, True)]).name == "Speaker"
    assert preferred([Control("Headset", 0, True), Control("PCM", 1, True)]).id == "PCM,1"
    assert preferred([Control("Headset", 0, True), Control("Earpiece", 0, True)]).name == "Headset"


def test_it_turns_the_cards_playback_volume_as_mapped_volume():
    amixer = FakeAmixer(60)
    found = speaker(amixer)
    assert (found.mixer.card, found.mixer.control.id) == (2, "PCM,0")
    assert found.status() == f"{DEVICE}: volume 60%"
    assert found.call(SET, {"level": 40}, Request()) == f"done: {DEVICE} set to 40%; volume 40%"
    assert ["amixer", "-M", "-c", "2", "-q", "sset", "PCM,0", "40%", "unmute"] in amixer.ran
    assert all(c[:2] == ["amixer", "-M"] for c in amixer.ran if "scontents" not in c)


def test_its_level_is_the_playback_lines_alone():
    mixer = Alsa(2, Control("PCM", 0, True), lambda command: sget(30, capture=True))
    assert mixer.level() == (30, False)
    assert Alsa(2, Control("PCM", 0, True), lambda command: sget(30, on=False)).level() == (30, True)
    with pytest.raises(HomeError, match="no playback volume"):
        Alsa(2, Control("PCM", 0, True), lambda command: "  Limits: Playback 0 - 64\n").level()


def test_up_and_down_a_step_a_press():
    amixer = FakeAmixer(60)
    found, now = speaker(amixer), Request()
    assert "volume 80%" in found.call(PRESS, {"button": "volume up", "times": 2}, now)
    assert found.call(PRESS, {"button": "volume_down"}, now) == f"done: {DEVICE} turned down from 80%; volume 70%"
    amixer.percent = 100
    assert "already as loud as it goes" in found.call(PRESS, {"button": "volume_up"}, now)


def test_it_never_goes_so_low_it_cant_be_heard():
    amixer = FakeAmixer(20)
    found, now = speaker(amixer), Request()
    assert f"volume {QUIET}%" in found.call(PRESS, {"button": "volume_down", "times": 5}, now)
    assert "already as low as it goes" in found.call(PRESS, {"button": "volume_down"}, now)
    assert found.call(SET, {"level": 3}, now) == f"done: {DEVICE} set to {QUIET}% (as low as it goes); volume 10%"
    assert found.call(SET, {"level": 0}, now).endswith(f"volume {QUIET}%")
    assert found.call(PRESS, {"button": "mute"}, now).startswith("refused")
    assert amixer.on


def test_down_never_raises_one_set_lower_elsewhere():
    amixer = FakeAmixer(4)
    assert "already as low as it goes; volume 4%" in speaker(amixer).call(PRESS, {"button": "volume_down"}, Request())
    assert amixer.percent == 4 and not any("sset" in c for c in amixer.ran)


def test_whatever_it_sets_leaves_it_unmuted_and_says_so():
    amixer = FakeAmixer(50, on=False)
    found = speaker(amixer)
    assert found.status() == f"{DEVICE}: volume 50%, muted"
    assert found.call(PRESS, {"button": "volume_down"}, Request()).endswith("(it had been muted; it isn't now)")
    amixer.on = False
    assert found.call(PRESS, {"button": "unmute"}, Request()) == f"done: {DEVICE} unmuted; volume 40%"
    assert amixer.on


def test_a_level_it_doesnt_read_back_is_not_confirmed():
    amixer = FakeAmixer(60, stuck=True)
    said = speaker(amixer).call(SET, {"level": 30}, Request())
    assert said == f"not confirmed: {DEVICE} was set to 30%, and reads 60%"


@pytest.mark.parametrize("level", [140, -5, True, "40", "40%", None, 0.4, float("nan")])
def test_a_wrong_level_is_said_and_nothing_changes(level):
    amixer = FakeAmixer(60)
    assert speaker(amixer).call(SET, {"level": level}, Request()).startswith("error")
    assert amixer.percent == 60 and not any("sset" in c for c in amixer.ran)


def test_a_wrong_button_is_said_and_nothing_changes():
    amixer = FakeAmixer(60)
    assert speaker(amixer).call(PRESS, {"button": "play"}, Request()).startswith("error")
    assert amixer.percent == 60


class FakeOsascript:
    def __init__(self, said: str = "60, false"):
        self.said, self.ran = said, []

    def __call__(self, command: list[str]) -> str:
        self.ran.append(command[-1])
        if command[-1].startswith("set volume output volume"):
            self.said = f"{command[-1].split()[4]}, false"
        return self.said


def test_on_a_mac_its_the_systems_output_volume():
    osascript = FakeOsascript("35, true")
    mac = MacVolume(osascript)
    assert mac.level() == (35, True)
    found = OwnVolume(mac)
    assert found.call(SET, {"level": 50}, Request()).startswith(f"done: {DEVICE} set to 50%; volume 50%")
    assert "set volume output volume 50 without output muted" in osascript.ran
    with pytest.raises(HomeError, match="didn't say its output volume"):
        MacVolume(FakeOsascript("missing value, missing value")).level()


def everywhere(name: str) -> str:
    return f"/usr/bin/{name}"


def test_the_mixer_is_the_cards_on_linux_and_the_systems_on_a_mac():
    linux = mixer_for(POWERCONF, "default", "Linux", FakeAmixer(), everywhere)
    assert isinstance(linux, Alsa) and linux.card == 2
    assert isinstance(
        mixer_for("MacBook Speakers", "MacBook Speakers", "Darwin", FakeOsascript(), everywhere), MacVolume
    )


@pytest.mark.parametrize(
    "output, default, system, which, says",
    [
        ("default", "default", "Linux", everywhere, "doesn't say its sound card"),
        (POWERCONF, "default", "Linux", lambda name: None, "alsa-utils"),
        ("PowerConf", "MacBook Speakers", "Darwin", everywhere, "not the default output"),
        ("x", "x", "Windows", everywhere, "no volume control"),
    ],
)
def test_without_a_volume_to_turn_it_says_why(output, default, system, which, says):
    with pytest.raises(HomeError, match=says):
        mixer_for(output, default, system, FakeAmixer(), which)


def test_one_that_cant_say_its_volume_isnt_offered():
    with pytest.raises(HomeError):
        mixer_for("MacBook", "MacBook", "Darwin", FakeOsascript("missing value, missing value"), everywhere)


def test_its_rules_are_its_own_and_its_check_says_its_level():
    found = speaker(FakeAmixer(60))
    assert "never mutes" in found.rules() and "one press" in found.rules()
    assert found.check() == [("ok", "volume 60% (sound card 2's 'PCM' control)", "")]
    assert speaker(FakeAmixer(60, on=False)).check()[0].status == "warn"


def test_the_home_tool_works_it_as_one_of_its_devices(tmp_path):
    amixer = FakeAmixer(60)
    tools = make_home(config(), tmp_path, announce=lambda line: None, speaker=speaker(amixer))
    assert list(tools.devices) == [DEVICE.lower()]
    assert DEVICE in tools.rules() and "3 times" not in tools.rules()
    said = tools.call({"device": DEVICE, "action": SET, "level": 35}, Request())
    assert said == f"done: {DEVICE} set to 35%; volume 35%" and amixer.percent == 35


def test_without_it_and_without_devices_there_is_no_home_tool(tmp_path):
    assert make_home(config(), tmp_path, announce=lambda line: None) is None


def test_its_off_unless_asked_for_and_left_out_saying_why_when_it_cant_be_turned(monkeypatch, capsys):
    from voice_assistant import __main__ as cli
    from voice_assistant import volume

    assert cli.own_speaker(config()) is None

    def nothing_to_turn(output_device: str):
        raise HomeError("TARS plays to 'default', which doesn't say its sound card")

    monkeypatch.setattr(volume, "speaker_mixer", nothing_to_turn)
    assert cli.own_speaker(config(own_speaker=True)) is None
    assert f"{DEVICE} isn't connected: TARS plays to 'default'" in capsys.readouterr().out
    monkeypatch.setattr(volume, "speaker_mixer", lambda output_device: Alsa.on_card(2, FakeAmixer()))
    assert isinstance(cli.own_speaker(config(own_speaker=True)), OwnVolume)


def test_check_says_whether_it_can_be_turned():
    from voice_assistant import check

    assert check.own_speaker(config(), lambda output_device: None).status == check.SKIP
    on = config(own_speaker=True)
    assert check.own_speaker(on, lambda output_device: Alsa.on_card(2, FakeAmixer(45)))[0].detail.startswith(
        "volume 45%"
    )

    def none(output_device: str):
        raise HomeError("amixer isn't installed (sudo apt install alsa-utils)")

    failed = check.own_speaker(on, none)
    assert failed.status == check.FAIL and "alsa-utils" in failed.detail


CARDS = """ 0 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones
                      bcm2835 Headphones
 1 [PowerConf      ]: USB-Audio - PowerConf
                      Anker PowerConf at usb-0000:01:00.0-1.2, full speed
 2 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
                      vc4-hdmi-0
"""


def test_a_card_is_found_by_name_in_alsa_s_own_list():
    assert volume.card_named("PowerConf", CARDS) == 1
    assert volume.card_named("anker", CARDS) == 1  # its second line counts too
    assert volume.card_named("hdmi-0", CARDS) == 2
    assert volume.card_named("Jabra", CARDS) is None


def test_a_speaker_the_running_assistant_has_open_is_still_found_by_its_card(tmp_path, monkeypatch):
    """On Linux a device one program has open isn't listed to another (--check, beside the running assistant)."""
    from voice_assistant import audio

    def unlisted(name, kind):
        raise audio.NoDeviceError(f"No {kind} device matching {name!r}.")

    cards = tmp_path / "cards"
    cards.write_text(CARDS)
    made = []
    monkeypatch.setattr(audio, "device_names", lambda: (["bcm2835 Headphones: - (hw:0,0)"], 0, 0))
    monkeypatch.setattr(audio, "find_device", unlisted)
    monkeypatch.setattr(volume.platform, "system", lambda: "Linux")
    monkeypatch.setattr(volume, "mixer_for", lambda output, default, system: made.append(output) or "mixer")
    assert volume.speaker_mixer("PowerConf", cards) == "mixer" and made == ["PowerConf (hw:1,0)"]
    with pytest.raises(HomeError, match="No output device matching 'Jabra'"):
        volume.speaker_mixer("Jabra", cards)
    monkeypatch.setattr(volume.platform, "system", lambda: "Darwin")
    with pytest.raises(HomeError, match="No output device"):
        volume.speaker_mixer("PowerConf", cards)
