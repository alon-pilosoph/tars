"""TARS's own speaker as a device the home tool works: "how loud are you?", "turn yourself down a bit", "set your
volume to 40". TARS's voice speaks through it, so only the voice's processes have it, and only with [home]
own_speaker; a chat has no speaker of TARS's to turn.

On Linux (the Pi) it's the playback volume of the sound card TARS plays to ([audio] output_device, the PowerConf),
through ALSA's mixer (amixer, in alsa-utils). The card is the one PortAudio names the device by ("Anker PowerConf: USB
Audio (hw:2,0)" is card 2), and the control is one with a playback volume, never a microphone's. A device without a
card in its name (the system's "default") isn't one TARS can turn: it's left out, saying why. On a Mac it's the
system's output volume, so only when TARS plays to the system's default output.

TARS never mutes it or sets it below QUIET: a TARS that can't be heard can't say a timer is up, or that it's turned
itself back up. Whatever it sets leaves it unmuted, and the level is read back after: what it says is what the
computer reads. A speakerphone's own buttons may change its volume without telling the computer (not yet checked on
the PowerConf), so after those the level said may be off until it's next set by asking."""

import platform
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .home import PRESS, SET, STATUS, Check, Device, HomeError, Request, button, presses

DEVICE = "TARS's speaker"
STEP = 10  # percent a press of volume up or down: "a bit"
QUIET = 10  # the lowest TARS sets it to: below that, it can't be heard saying it's done
CLOSE_ENOUGH = 2  # percent a level read back may be off the one set by: the mixer's own steps round it
# Playback controls by name, the likeliest first, for a card with more than one.
CONTROLS = ("PCM", "Speaker", "Master", "Headphone")
Run = Callable[[list[str]], str]


def _run(command: list[str]) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HomeError(f"{command[0]} didn't answer ({type(e).__name__})") from None
    if done.returncode != 0:
        raise HomeError(f"{command[0]} said: {(done.stderr or done.stdout).strip()[:200]}")
    return done.stdout


class Mixer(Protocol):
    """A speaker's volume: Alsa or MacVolume."""

    about: str  # what it is, for --check: "sound card 2's 'PCM' control"

    def level(self) -> tuple[int, bool]:
        """(percent, muted), as the computer reads it now."""

    def set(self, percent: int) -> None:
        """Sets it to `percent`, and unmutes it."""


def card_of(name: str) -> int | None:
    """The ALSA card in PortAudio's name for a device: "Anker PowerConf: USB Audio (hw:2,0)" is card 2."""
    _, found, after = name.rpartition("(hw:")
    card = after.partition(",")[0]
    return int(card) if found and card.isdigit() else None


@dataclass(frozen=True)
class Control:
    """One of a card's mixer controls with a playback volume, as `amixer scontents` lists it."""

    name: str
    index: int
    switch: bool  # it can be muted and unmuted

    @property
    def id(self) -> str:
        return f"{self.name},{self.index}"


def playback_controls(scontents: str) -> list[Control]:
    """The controls with a playback volume (pvolume), in the order amixer lists them; a capture-only one (a
    microphone's) is never one."""
    found, name, index = [], None, 0
    head = "Simple mixer control '"
    for line in scontents.splitlines():
        if line.startswith(head):
            name, _, index_said = line[len(head) :].rpartition("',")
            index = int(index_said) if index_said.isdigit() else 0
        elif name is not None and line.strip().startswith("Capabilities:"):
            capabilities = line.partition(":")[2].split()
            if "pvolume" in capabilities or "pvolume-joined" in capabilities:
                switch = "pswitch" in capabilities or "pswitch-joined" in capabilities
                found.append(Control(name, index, switch))
    return found


def preferred(controls: list[Control]) -> Control | None:
    """The control to turn: one that can be unmuted first, then by CONTROLS' order, then as listed."""
    rank = {name: i for i, name in enumerate(CONTROLS)}
    return min(controls, key=lambda c: (not c.switch, rank.get(c.name, len(CONTROLS))), default=None)


class Alsa:
    """A sound card's playback volume, through amixer, as mapped volume (-M): the percent alsamixer shows, which goes
    up as loudness sounds like it does."""

    def __init__(self, card: int, control: Control, run: Run = _run):
        self.card, self.control, self._run = card, control, run
        self.about = f"sound card {card}'s {control.name!r} control"

    @classmethod
    def on_card(cls, card: int, run: Run = _run) -> Alsa:
        control = preferred(playback_controls(run(["amixer", "-c", str(card), "scontents"])))
        if control is None:
            raise HomeError(f"sound card {card} has no playback volume")
        return cls(card, control, run)

    def _amixer(self, *args: str) -> str:
        return self._run(["amixer", "-M", "-c", str(self.card), *args])

    def level(self) -> tuple[int, bool]:
        """From the playback lines only: `Front Left: Playback 38 [60%] [-12.00dB] [on]`; muted when every channel
        is off."""
        levels, off = [], []
        for line in self._amixer("sget", self.control.id).splitlines():
            words = line.partition(":")[2].split()
            if not words or words[0] != "Playback":
                continue
            levels += [int(w[1:-2]) for w in words if w.startswith("[") and w.endswith("%]") and w[1:-2].isdigit()]
            off += [w == "[off]" for w in words if w in ("[on]", "[off]")]
        if not levels:
            raise HomeError(f"{self.about} said no playback volume")
        return round(sum(levels) / len(levels)), bool(off) and all(off)

    def set(self, percent: int) -> None:
        unmute = ["unmute"] if self.control.switch else []
        self._amixer("-q", "sset", self.control.id, f"{percent}%", *unmute)


class MacVolume:
    """The Mac's output volume, through AppleScript: the speakerphone's, when it's the system's output."""

    about = "the Mac's output volume"

    def __init__(self, run: Run = _run):
        self._run = run

    def level(self) -> tuple[int, bool]:
        said = self._run(["osascript", "-e", "get {output volume, output muted} of (get volume settings)"])
        volume, _, muted = said.strip().partition(",")  # "60, false"; "missing value, ..." for one without a volume
        if not volume.strip().isdigit():
            raise HomeError(f"the Mac didn't say its output volume ({said.strip()[:60]!r})")
        return int(volume), muted.strip() == "true"

    def set(self, percent: int) -> None:
        self._run(["osascript", "-e", f"set volume output volume {percent} without output muted"])


def mixer_for(output: str, default: str, system: str, run: Run = _run, which=shutil.which) -> Mixer:
    """The volume of `output`, the device TARS plays to, by PortAudio's name for it (`default`: the system's default
    output's name): its sound card's on Linux, the system's on a Mac when it's the default. Its level is read once
    here, so one that can't say it isn't offered. Raises HomeError saying why there's none."""
    if system == "Darwin":
        if output != default:
            raise HomeError(f"on a Mac only the system's output volume can be set, and TARS plays to {output!r}, "
                            f"not the default output ({default!r})")  # fmt: skip
        if not which("osascript"):
            raise HomeError("osascript isn't there to set the Mac's volume with")
        mixer: Mixer = MacVolume(run)
    elif system == "Linux":
        if not which("amixer"):
            raise HomeError("amixer isn't installed (sudo apt install alsa-utils)")
        card = card_of(output)
        if card is None:
            raise HomeError(f"TARS plays to {output!r}, which doesn't say its sound card: set [audio] output_device "
                            "to the speakerphone (voice-assistant --list-devices)")  # fmt: skip
        mixer = Alsa.on_card(card, run)
    else:
        raise HomeError(f"there's no volume control for {system}")
    mixer.level()
    return mixer


def speaker_mixer(output_device: str) -> Mixer:
    """The volume of the speaker TARS plays to ([audio] output_device; "" is the system's default output), or a
    HomeError saying why it has none."""
    from .audio import NoDeviceError, device_names, find_device

    names, _, default = device_names()
    try:
        index = find_device(output_device, "output")
    except NoDeviceError as e:
        raise HomeError(str(e)) from None
    if index is None and not 0 <= default < len(names):
        raise HomeError("there's no default output")
    default_name = names[default] if 0 <= default < len(names) else ""
    return mixer_for(names[default if index is None else index], default_name, platform.system())


class OwnVolume(Device):
    """The home tool's "TARS's speaker": its volume, read, set, and turned up or down; never muted, or set below
    QUIET (the module's docstring says why)."""

    actions = (STATUS, SET, PRESS)

    def __init__(self, mixer: Mixer):
        super().__init__(DEVICE)
        self.mixer = mixer

    def what(self) -> str:
        return (
            f"your own speaker, the one you talk through: status says its volume; set takes level, a percent ({QUIET} "
            f"to 100); press takes volume_up or volume_down ({STEP}% a press; times for more), or unmute"
        )

    def rules(self) -> str:
        return (
            f'"{DEVICE}" is you: "turn yourself down", "you\'re too loud", "how loud are you?" and "your volume" are '
            f"about it. A bit louder or quieter is one press of volume_up or volume_down, a lot is three. It never "
            f"goes below {QUIET}% and never mutes, since then you couldn't be heard, even saying a timer is up: asked "
            f"to be quiet or to mute yourself, set it to {QUIET}% and say that's as low as you go. Say the level it "
            "answers, as a percent."
        )

    def status(self) -> str:
        percent, muted = self.mixer.level()
        return f"{DEVICE}: volume {percent}%" + (", muted" if muted else "")

    def call(self, action: str, args: dict, request: Request) -> str:
        before, muted = self.mixer.level()
        if action == SET:
            level = args.get("level")
            if isinstance(level, bool) or not isinstance(level, int | float) or not 0 <= level <= 100:
                return "error: set takes level, a percent from 0 to 100 (40 for 40%); nothing changed"
            if 0 < level < 1:
                return "error: level is a percent from 0 to 100 (40 for 40%), not a fraction; nothing changed"
            wanted = max(QUIET, round(level))
            return self._set(wanted, f"set to {wanted}%" + (" (as low as it goes)" if level < QUIET else ""), muted)
        pressed = button(args)
        if pressed == "mute":
            return f"refused: {DEVICE} doesn't mute (then you couldn't be heard); the lowest it goes is {QUIET}%"
        if pressed == "unmute":
            return self._set(max(before, QUIET), "unmuted" if muted else "wasn't muted", was_muted=False)
        if pressed not in ("volume_up", "volume_down"):
            return "error: press takes volume_up, volume_down or unmute; nothing changed"
        up, step = pressed == "volume_up", STEP * presses(args)
        # Down never raises it: one set below QUIET elsewhere stays where it is.
        wanted = min(100, before + step) if up else max(min(before, QUIET), before - step)
        if wanted == before and not muted:
            return f"done: {DEVICE} is already as {'loud' if up else 'low'} as it goes; volume {before}%"
        return self._set(wanted, f"turned {'up' if up else 'down'} from {before}%", muted)

    def _set(self, percent: int, what: str, was_muted: bool) -> str:
        """Sets it to `percent`, and says `what` was done only if the level read back is that."""
        self.mixer.set(percent)
        now, muted = self.mixer.level()
        if muted or abs(now - percent) > CLOSE_ENOUGH:
            return f"not confirmed: {DEVICE} was set to {percent}%, and reads {now}%" + (", muted" if muted else "")
        unmuted = " (it had been muted; it isn't now)" if was_muted else ""
        return f"done: {DEVICE} {what}; volume {now}%{unmuted}"

    def check(self) -> list[Check]:
        percent, muted = self.mixer.level()
        if muted:
            return [Check("warn", f"muted ({self.mixer.about})", "TARS can't be heard: ask it to unmute itself")]
        return [Check("ok", f"volume {percent}% ({self.mixer.about})")]
