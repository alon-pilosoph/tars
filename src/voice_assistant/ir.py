"""Remotes whose codes are already published: one fixed code per button, sent through the IR blaster, so nothing has
to be recorded. Unlike the AC's, these codes carry no settings: "volume up" is always the same code.

Codes, as the household's remotes send them:
- samsung-tv: Samsung's TVs (Samsung32 protocol, device 7), from IRDB (github.com/probonopd/irdb, codes/Samsung/TV)
  and Flipper-IRDB's Samsung remotes for the power-off code: "off" only ever turns it off. The power button (0x02)
  toggles, so it isn't offered: there's no published power-on code that works on every set, and the Fire TV turns the
  TV on (HDMI-CEC) instead.
- polk-re9641: Polk's RE9641 soundbar remote (NEC extended, address C8 91), the Signa S2's and MagniFi Mini's, from
  Flipper-IRDB (SoundBars/PolkAudio/POLK_RE9641_1.ir).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

HEADER_NEC, HEADER_SAMSUNG = (9000, 4500), (4500, 4500)
MARK, ZERO, ONE = 560, 560, 1690
GAP_S = 0.15  # between presses of the same button
MAX_PRESSES = 15


@dataclass(frozen=True)
class CodeSet:
    about: str
    header: tuple[int, int]
    address: tuple[int, int]  # the code's first two bytes
    buttons: dict[str, int]  # button: command byte
    off: str = ""  # the button that only turns it off, if there is one

    def pulses(self, button: str) -> list[int]:
        """The marks and spaces, in microseconds, of one press: KeyError for a button it doesn't have."""
        command = self.buttons[button]
        return nec_pulses([*self.address, command, command ^ 0xFF], self.header)


def nec_pulses(data: list[int], header: tuple[int, int]) -> list[int]:
    """NEC's pulse-distance code (Samsung's is the same with a shorter header): each byte lowest bit first, a 1 a
    long space, a 0 a short one, and a final mark."""
    pulses = list(header)
    for byte in data:
        for i in range(8):
            pulses += [MARK, ONE if byte >> i & 1 else ZERO]
    return [*pulses, MARK]


CODE_SETS = {
    "samsung-tv": CodeSet(
        "a Samsung TV",
        HEADER_SAMSUNG,
        (0x07, 0x07),
        {
            "off": 0x98,
            "volume_up": 0x07,
            "volume_down": 0x0B,
            "mute": 0x0F,
            "source": 0x01,
            "hdmi1": 0xE9,
            "hdmi2": 0xBE,
            "hdmi3": 0xC2,
            "hdmi4": 0xC5,
            "up": 0x60,
            "down": 0x61,
            "right": 0x62,
            "left": 0x65,
            "select": 0x68,
            "back": 0x58,
            "exit": 0x2D,
            "menu": 0x1A,
            "home": 0x79,
            "info": 0x1F,
        },
        off="off",
    ),
    "polk-re9641": CodeSet(
        "a Polk soundbar",
        HEADER_NEC,
        (0xC8, 0x91),
        {
            "power": 0x00,
            "mute": 0x20,
            "volume_up": 0x1E,
            "volume_down": 0x1F,
            "bass_up": 0x5C,
            "bass_down": 0x5D,
            "voice_up": 0x6A,
            "voice_down": 0x6B,
            "night": 0x71,
            "movie": 0x77,
            "music": 0x78,
            "sports": 0x79,
            "tv": 0x0F,
            "optical": 0x0E,
            "aux": 0x0D,
            "play_pause": 0x21,
            "next": 0x24,
            "previous": 0x26,
        },
    ),
}


class Remote:
    """The home tool's IR device: presses its buttons through the blaster. IR goes one way: "sent" is all it knows."""

    def __init__(self, name: str, codes: CodeSet, send: Callable[[list[int]], None], sleep=time.sleep):
        self.name, self.codes, self._send, self._sleep = name, codes, send, sleep

    def what(self) -> str:
        off = ", off (only ever turns it off)" if self.codes.off else ""
        return f"{self.codes.about}, by IR: press a button ({', '.join(self.codes.buttons)}){off}"

    def status(self) -> str:
        return f"{self.name}: unknown (IR can't tell)"

    def call(self, action: str, args: dict, request=None) -> str:
        if action == "off":
            if not self.codes.off:
                return f"error: the {self.name} has no off of its own; press power, which switches it either way"
            button, times = self.codes.off, 1
        else:
            button = str(args.get("button") or "").strip().lower().replace(" ", "_").replace("-", "_")
            if button not in self.codes.buttons:
                return f"error: the {self.name}'s buttons are {', '.join(self.codes.buttons)}"
            try:
                times = max(1, min(MAX_PRESSES, int(args.get("times") or 1)))
            except TypeError, ValueError:
                times = 1
        for i in range(times):
            if i:
                self._sleep(GAP_S)
            self._send(self.codes.pulses(button))
        return f"sent: {button}{f' {times} times' if times > 1 else ''} to the {self.name} (IR can't confirm it)"
