"""TARS's own speaker as a device the home tool works: "how loud are you?", "turn yourself down a bit", "set your
volume to 40". On the Pi that's the speakerphone's own volume ([audio] output_device, the PowerConf), set through
Linux's sound mixer (ALSA's amixer); on a Mac, the system's output volume, which is the speakerphone's when it's the
output. It's TARS's process that speaks through it, so only the voice has it: a chat works nothing in the house.

What it reports is the level the computer has set. A speakerphone's own buttons may change its volume without
telling the computer (the PowerConf's do on Linux), so after those the level said may be off until it's next set
here."""

import platform
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from .home import PRESS, SET, STATUS, Device, HomeError, Request, button, presses

DEVICE = "TARS's speaker"
STEP = 10  # percent a press of volume up or down: "a bit"
QUIET = 10  # the lowest it's set to unless asked for silence: below that, TARS can't be heard saying it's done
CARDS = Path("/proc/asound/cards")
# The playback controls a USB speakerphone's mixer has, most likely first.
CONTROLS = ("PCM", "Speaker", "Master", "Headphone", "Playback")
Run = Callable[[list[str]], str]


def _run(command: list[str]) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HomeError(f"{command[0]} didn't answer ({type(e).__name__})") from None
    if done.returncode != 0:
        raise HomeError(f"{command[0]} said: {(done.stderr or done.stdout).strip()[:200]}")
    return done.stdout


class Alsa:
    """A sound card's playback volume, through amixer: the card whose name has `wanted` in it, else the first."""

    def __init__(self, card: str, control: str, run: Run = _run):
        self.card, self.control, self._run = card, control, run

    @classmethod
    def find(cls, wanted: str, cards: Path = CARDS, run: Run = _run) -> Alsa | None:
        """The output device's card and its volume control; None without one."""
        try:
            listed = cards.read_text()
        except OSError:
            return None
        found = []  # (index, name), from lines like ` 2 [PowerConf      ]: USB-Audio - Anker PowerConf`
        for line in listed.splitlines():
            index, _, rest = line.strip().partition(" ")
            if index.isdigit() and "]:" in rest:
                found.append((index, rest))
        chosen = next((i for i, name in found if wanted and wanted.lower() in name.lower()), None)
        chosen = chosen or (found[0][0] if found else None)
        if chosen is None:
            return None
        try:
            controls = [line.split("'")[1] for line in run(["amixer", "-c", chosen, "scontrols"]).splitlines()
                        if line.count("'") >= 2]  # fmt: skip
        except HomeError:
            return None
        control = next((c for c in CONTROLS if c in controls), controls[0] if controls else None)
        return cls(chosen, control, run) if control else None

    def level(self) -> tuple[int, bool]:
        """(percent, muted), as the mixer says: `Front Left: Playback 38 [60%] [-12.00dB] [on]`."""
        said = self._run(["amixer", "-c", self.card, "sget", self.control])
        levels = [int(t[1:-2]) for t in said.split() if t.startswith("[") and t.endswith("%]") and t[1:-2].isdigit()]
        if not levels:
            raise HomeError(f"the {self.control} control has no volume")
        return round(sum(levels) / len(levels)), "[off]" in said

    def set(self, percent: int) -> None:
        self._run(["amixer", "-q", "-c", self.card, "sset", self.control, f"{percent}%", "unmute"])

    def mute(self, muted: bool) -> None:
        self._run(["amixer", "-q", "-c", self.card, "sset", self.control, "mute" if muted else "unmute"])


class MacVolume:
    """The Mac's output volume, through AppleScript: the speakerphone's, when it's the output."""

    def __init__(self, run: Run = _run):
        self._run = run

    def level(self) -> tuple[int, bool]:
        said = self._run(["osascript", "-e", "get {output volume, output muted} of (get volume settings)"])
        volume, _, muted = said.strip().partition(",")
        if not volume.strip().isdigit():
            raise HomeError("the Mac didn't say its volume")
        return int(volume), muted.strip() == "true"

    def set(self, percent: int) -> None:
        self._run(["osascript", "-e", f"set volume output volume {percent} without output muted"])

    def mute(self, muted: bool) -> None:
        self._run(["osascript", "-e", f"set volume {'with' if muted else 'without'} output muted"])


def mixer_for(output_device: str):
    """The volume of the speaker TARS talks through: ALSA's on Linux, the system's on a Mac; None without one."""
    system = platform.system()
    if system == "Darwin" and shutil.which("osascript"):
        return MacVolume()
    if system == "Linux" and shutil.which("amixer"):
        return Alsa.find(output_device)
    return None


class OwnVolume(Device):
    """The home tool's "TARS's speaker": its volume, read, set, and turned up or down."""

    actions = (STATUS, SET, PRESS)

    def __init__(self, mixer):
        super().__init__(DEVICE)
        self.mixer = mixer

    def what(self) -> str:
        return (
            "your own speaker, the one you talk through: status says its volume; set takes level (a percent); press "
            f"takes volume_up or volume_down ({STEP}% a press; times for more), mute or unmute"
        )

    def rules(self) -> str:
        return (
            f'"{DEVICE}" is you: "turn yourself down", "you\'re too loud", "how loud are you?" and "your volume" are '
            "about it. Say the level it answers, as a percent."
        )

    def status(self) -> str:
        percent, muted = self.mixer.level()
        return f"{DEVICE}: volume {percent}%" + (", muted" if muted else "")

    def call(self, action: str, args: dict, request: Request) -> str:
        if not request.go_ahead():
            return "error: the request was withdrawn before anything was done; nothing changed"
        before, muted = self.mixer.level()
        if action == SET:
            level = args.get("level")
            if not isinstance(level, int | float) or not 0 <= level <= 100:
                return "error: set takes level, a percent from 0 to 100"
            if level == 0:
                self.mixer.mute(True)
                return self._now("muted")
            self.mixer.set(max(QUIET, round(level)))
            return self._now(
                f"set to {max(QUIET, round(level))}%" + (f" (as low as it goes: {QUIET}%)" if level < QUIET else "")
            )
        pressed = button(args)
        if pressed in ("mute", "unmute"):
            self.mixer.mute(pressed == "mute")
            return self._now(f"{pressed}d")
        if pressed not in ("volume_up", "volume_down"):
            return "error: press takes volume_up, volume_down, mute or unmute"
        step = STEP * presses(args) * (1 if pressed == "volume_up" else -1)
        level = max(QUIET, min(100, before + step))
        self.mixer.set(level)
        return self._now(f"turned {'up' if step > 0 else 'down'} from {before}%" + (" (unmuted)" if muted else ""))

    def _now(self, what: str) -> str:
        """`what` was done: and the level as the mixer reads it now."""
        percent, muted = self.mixer.level()
        return f"done: {DEVICE} {what}; volume {percent}%" + (", muted" if muted else "")
