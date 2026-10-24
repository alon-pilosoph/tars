"""TARS's own speaker as a device: its volume read, set and turned up or down, through amixer on the Pi."""

from voice_assistant.home import PRESS, SET, Request
from voice_assistant.volume import QUIET, Alsa, OwnVolume

CARDS = """ 0 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones
                      bcm2835 Headphones
 2 [PowerConf      ]: USB-Audio - Anker PowerConf
                      Anker PowerConf at usb-xhci-hcd.1-1, full speed
"""
SCONTROLS = "Simple mixer control 'PCM',0\nSimple mixer control 'Mic',0\n"


def sget(percent: int, on: bool = True) -> str:
    state = "on" if on else "off"
    return f"""Simple mixer control 'PCM',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 64
  Front Left: Playback 38 [{percent}%] [-12.00dB] [{state}]
  Front Right: Playback 38 [{percent}%] [-12.00dB] [{state}]
"""


class FakeAmixer:
    """amixer on a card: its PCM level, changed as asked, and every command it was given."""

    def __init__(self, percent: int = 60, on: bool = True):
        self.percent, self.on, self.ran = percent, on, []

    def __call__(self, command: list[str]) -> str:
        self.ran.append(command)
        if command[-1] == "scontrols":
            return SCONTROLS
        if "sget" in command:
            return sget(self.percent, self.on)
        for word in command[command.index("sset") + 2 :]:
            if word.endswith("%"):
                self.percent = int(word[:-1])
            elif word in ("mute", "unmute"):
                self.on = word == "unmute"
        return ""


def speaker(tmp_path, amixer):
    (tmp_path / "cards").write_text(CARDS)
    return OwnVolume(Alsa.find("PowerConf", tmp_path / "cards", amixer))


def test_it_finds_the_output_devices_card_and_its_volume(tmp_path):
    amixer = FakeAmixer(60)
    found = speaker(tmp_path, amixer)
    assert (found.mixer.card, found.mixer.control) == ("2", "PCM")
    assert found.status() == "TARS's speaker: volume 60%"


def test_set_and_up_and_down(tmp_path):
    amixer = FakeAmixer(60)
    found, now = speaker(tmp_path, amixer), Request()
    assert found.call(SET, {"level": 40}, now) == "done: TARS's speaker set to 40%; volume 40%"
    assert "volume 60%" in found.call(PRESS, {"button": "volume up", "times": 2}, now)
    assert "volume 50%" in found.call(PRESS, {"button": "volume_down"}, now)
    assert ["amixer", "-q", "-c", "2", "sset", "PCM", "50%", "unmute"] in amixer.ran


def test_it_never_goes_so_low_it_cant_say_so_unless_muted(tmp_path):
    amixer = FakeAmixer(20)
    found, now = speaker(tmp_path, amixer), Request()
    assert f"volume {QUIET}%" in found.call(PRESS, {"button": "volume_down", "times": 5}, now)
    assert f"as low as it goes: {QUIET}%" in found.call(SET, {"level": 3}, now)
    assert found.call(SET, {"level": 0}, now).endswith(", muted")
    assert found.status() == f"TARS's speaker: volume {QUIET}%, muted"


def test_a_wrong_level_or_button_is_said_and_nothing_changes(tmp_path):
    amixer = FakeAmixer(60)
    found, now = speaker(tmp_path, amixer), Request()
    assert found.call(SET, {"level": 140}, now).startswith("error")
    assert found.call(PRESS, {"button": "play"}, now).startswith("error")
    assert amixer.percent == 60


def test_a_withdrawn_request_changes_nothing(tmp_path):
    amixer = FakeAmixer(60)
    withdrawn = Request(by_voice=True, may_act=lambda timeout: False)
    assert speaker(tmp_path, amixer).call(SET, {"level": 20}, withdrawn).startswith("error")
    assert amixer.percent == 60


def test_no_card_is_no_speaker(tmp_path):
    (tmp_path / "cards").write_text("--- no soundcards ---\n")
    assert Alsa.find("PowerConf", tmp_path / "cards", FakeAmixer()) is None
    assert Alsa.find("PowerConf", tmp_path / "missing", FakeAmixer()) is None
