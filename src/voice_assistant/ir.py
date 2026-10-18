"""Remotes whose codes are already published: one fixed code per button, sent through the IR blaster, so nothing has
to be recorded. Unlike the AC's, these codes carry no settings: "volume up" is always the same code.

Codes, as the household's remotes send them:
- samsung-tv: Samsung's TVs (Samsung32 protocol, device 7), from IRDB (github.com/probonopd/irdb, codes/Samsung/TV)
  and Flipper-IRDB's Samsung remotes for the power-off code. Besides the power button (0x02), which switches it either
  way, Samsung has codes that only turn it on (0x99: E0E09966, as the codes are often written) and only off (0x98:
  E0E019E6). Not every set takes them: `python -m voice_assistant.ir test TV` tries each on the TV, and TARS uses what
  worked (voice_data/ir/).
- polk-re9641: Polk's RE9641 soundbar remote (NEC extended, address C8 91), the Signa S2's and MagniFi Mini's, from
  Flipper-IRDB (SoundBars/PolkAudio/POLK_RE9641_1.ir).
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

HEADER_NEC, HEADER_SAMSUNG = (9000, 4500), (4500, 4500)
MARK, ZERO, ONE = 560, 560, 1690
GAP_S = 0.15  # between presses of the same button
MAX_PRESSES = 15
TESTED = "voice_data/ir"  # what trying the power codes on each set showed


@dataclass(frozen=True)
class CodeSet:
    about: str
    header: tuple[int, int]
    address: tuple[int, int]  # the code's first two bytes
    buttons: dict[str, int]  # button: command byte
    on: str = ""  # the code that only turns it on, if there is one
    off: str = ""  # the code that only turns it off
    power: str = ""  # the power button, which switches it either way

    def presses(self) -> list[str]:
        """The buttons to press: power ones aren't, where there's an on and off to use instead."""
        return [b for b in self.buttons if not (self.on and b in (self.on, self.off, self.power))]

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
            "on": 0x99,
            "off": 0x98,
            "power": 0x02,
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
        on="on",
        off="off",
        power="power",
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
    """The home tool's IR device: presses its buttons through the blaster. IR goes one way: "sent" is all it knows.

    On and off use the codes that only turn it on or off, unless trying them on this set showed they don't work
    (`tested`, from `python -m voice_assistant.ir test`); then the power button, which switches it either way, but only
    when `seems_on()` (the Fire TV's say, through HDMI-CEC) says it's the other way now."""

    def __init__(
        self,
        name: str,
        codes: CodeSet,
        send: Callable[[list[int]], None],
        sleep=time.sleep,
        tested: Path | None = None,
        seems_on: Callable[[], bool | None] | None = None,
    ):
        self.name, self.codes, self._send, self._sleep = name, codes, send, sleep
        self._tested, self._seems_on = tested, seems_on

    def works(self) -> dict[str, bool]:
        """What trying the power codes on this set showed: {"on": True, "off": True, "power": True}; {} if untried."""
        try:
            return json.loads(self._tested.read_text()) if self._tested else {}
        except FileNotFoundError, ValueError:
            return {}

    def what(self) -> str:
        power = ", on, off" if self.codes.on else ""
        return f"{self.codes.about}, by IR: press a button ({', '.join(self.codes.presses())}){power}"

    def status(self) -> str:
        return f"{self.name}: unknown (IR can't tell)"

    def call(self, action: str, args: dict, request=None) -> str:
        if action in ("on", "off"):
            return self._power(action == "on")
        button = str(args.get("button") or "").strip().lower().replace(" ", "_").replace("-", "_")
        if button not in self.codes.presses():
            return f"error: the {self.name}'s buttons are {', '.join(self.codes.presses())}"
        try:
            times = max(1, min(MAX_PRESSES, int(args.get("times") or 1)))
        except TypeError, ValueError:
            times = 1
        for i in range(times):
            if i:
                self._sleep(GAP_S)
            self._send(self.codes.pulses(button))
        return f"sent: {button}{f' {times} times' if times > 1 else ''} to the {self.name} (IR can't confirm it)"

    def _power(self, on: bool) -> str:
        if not self.codes.on:
            return f"error: the {self.name} only has a power button, which switches it either way: press power"
        word, works = "on" if on else "off", self.works()
        own = self.codes.on if on else self.codes.off
        if works.get(word, True):
            self._send(self.codes.pulses(own))
            tried = "it works on this TV" if word in works else "not tried on this TV yet"
            return f"sent: {word}, the code that only turns it {word} ({tried}; IR can't confirm it)"
        if not works.get("power"):
            return f"error: the {self.name} didn't take its {word} code or its power button when they were tried"
        now = self._seems_on() if self._seems_on else None
        if now is None:
            return f"error: the {self.name}'s power button switches it either way, and TARS can't tell if it's {word}"
        if now == on:
            return f"already {word}, as far as the fire tv can tell, so nothing was sent"
        self._send(self.codes.pulses(self.codes.power))
        return f"sent: the power button, to turn it {word} (IR can't confirm it)"


def test(remote: Remote, path: Path, ask: Callable[[str], bool]) -> dict[str, bool]:
    """Tries the power codes on the set, with someone watching: which turn it on, off, or switch it."""
    codes, works = remote.codes, {}
    input(f"Turn the {remote.name} ON with its own remote, and watch it. Enter when it's on. ")
    remote._send(codes.pulses(codes.off))
    works["off"] = ask(f"Sent {remote.name} OFF. Did it turn off?")
    if not works["off"]:
        input("Turn it off with its own remote. Enter when it's off. ")
    remote._send(codes.pulses(codes.on))
    works["on"] = ask(f"Sent {remote.name} ON. Did it turn on?")
    if not works["on"]:
        input("Turn it on with its own remote. Enter when it's on. ")
    remote._send(codes.pulses(codes.power))
    works["power"] = ask("Sent the power button. Did it turn off?")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(works))
    return works


def main() -> None:
    """python -m voice_assistant.ir test NAME | press NAME BUTTON: trying a TV's or soundbar's codes."""
    import argparse

    from dotenv import dotenv_values

    from .ac import TUYA_KEY, Blaster
    from .config import load_config

    parser = argparse.ArgumentParser(prog="python -m voice_assistant.ir", description=main.__doc__)
    parser.add_argument("command", choices=["test", "press"])
    parser.add_argument("name", help="the device's name in config.toml")
    parser.add_argument("button", nargs="?")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()
    root = args.config.resolve().parent
    cfg = load_config(args.config)
    spec = next((d for d in cfg.home.devices if d["kind"] == "ir" and d["name"].lower() == args.name.lower()), None)
    if spec is None or not cfg.home.blaster:
        raise SystemExit(f"config.toml needs [home] blaster and an ir device called {args.name!r}: see docs/home.md")
    key = dotenv_values(root / ".env").get(TUYA_KEY)
    if not key:
        raise SystemExit(f"Put the blaster's local key in .env as {TUYA_KEY}")
    blaster = Blaster.from_spec(cfg.home.blaster, key)
    name = spec["name"].lower()
    remote = Remote(name, CODE_SETS[spec["codes"]], blaster.send, tested=root / TESTED / f"{name}.json")
    if args.command == "press":
        print(remote.call("press", {"button": args.button}))
    elif not remote.codes.on:
        print(f"The {name} has only a power button: nothing to test. Try: press {name} volume_up")
    else:
        works = test(remote, root / TESTED / f"{name}.json", lambda q: input(f"{q} (y/n) ").lower().startswith("y"))
        print(f"Saved: {works}. TARS turns the {name} on and off with what worked.")


if __name__ == "__main__":
    main()
