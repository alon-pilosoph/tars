"""Buttons: the speakerphone's own + and -, and a remote's buttons through an IR receiver. Volume up and down turn
TARS's own speaker a step a press, as asking TARS to does ([home] own_speaker, volume.py), so the same floor holds:
never muted, never below QUIET. A remote's talk, stop and mute buttons wake TARS, stop it, and mute it (assistant.py,
speakerphone.py).

On Linux (the Pi) both arrive as input events (/dev/input/event*, readable by the `input` group). The speakerphone's
buttons are volume keys (KEY_VOLUMEUP, KEY_VOLUMEDOWN): the PowerConf doesn't change its own volume when they're
pressed, it leaves that to the computer, and on a Pi with no desktop nothing else does. A remote's buttons are the
codes the kernel's IR decoder reads (MSC_SCAN), which [home] remote names, from a receiver like the Argon ONE case's
(docs/home.md#buttons). An HDMI port's remote (the TV's, over CEC) is a receiver too, and is left out: its
codes aren't the remote's.

A held button repeats (a remote's code about every 0.1 s, a key as the kernel repeats it). Held, volume turns a
step every REPEAT_S, not a step a repeat; talk, stop and mute count once a press, however long it's held: a press
of one is its code after RELEASE_S without it (debounced, so a held mute never toggles twice)."""

import contextlib
import os
import select
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

EV_KEY, EV_MSC, MSC_SCAN = 1, 4, 4
KEY_VOLUMEDOWN, KEY_VOLUMEUP = 114, 115
PRESSED, HELD = 1, 2  # a key event's value: 0 is let go
EVENT = struct.Struct("llHHi")  # struct input_event: its time (two longs), type, code, value
REPEAT_S = 0.3  # held, the volume turns a step this often
RELEASE_S = 0.25  # a remote's button not repeated for this long was let go: well over its 0.11 s between repeats
RESCAN_S = 10.0  # how often devices plugged in since are looked for
UP, DOWN = "volume_up", "volume_down"  # [home] remote's buttons: the volume's,
TALK, STOP, MUTE = "talk", "stop", "mute"  # and those that count once a press
BUTTONS = (UP, DOWN, TALK, STOP, MUTE)


@dataclass(frozen=True)
class Input:
    """An input device: its event file, its name, and whether it's an IR receiver whose codes are a remote's."""

    path: str
    name: str
    remote: bool


def inputs(sys_input: Path = Path("/sys/class/input"), dev: Path = Path("/dev/input")) -> list[Input]:
    """Every input device there is. One is a remote's receiver when it's a remote-control (rc) device that decodes
    more than HDMI-CEC."""
    found = []
    for event in sorted(sys_input.glob("event*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 0):
        try:
            device = (event / "device").resolve()
            name = (device / "name").read_text().strip()
        except OSError:
            continue
        rc = device.parent
        remote = False
        if rc.name.startswith("rc"):
            try:
                remote = (rc / "protocols").read_text().split() != ["[cec]"]
            except OSError:
                pass
        found.append(Input(str(dev / event.name), name, remote))
    return found


class Buttons:
    """Which button an input event is a press of: what's fed is each event, and what's said back is the button
    (UP, DOWN, TALK, STOP or MUTE), or None: not one of them, or a repeat of one held."""

    def __init__(self, remote: dict[str, int], keys: bool = True):
        self.codes = {code: button for button, code in remote.items()}
        self.keys = keys
        self._step: tuple[str, float] | None = None  # the volume's last step: which way, and when
        self._heard: dict[int, float] = {}  # when each remote code was last read, pressed or repeated

    def feed(self, source: Input, kind: int, code: int, value: int, now: float) -> str | None:
        if kind == EV_KEY and self.keys and code in (KEY_VOLUMEUP, KEY_VOLUMEDOWN) and value in (PRESSED, HELD):
            return self._volume(UP if code == KEY_VOLUMEUP else DOWN, value == PRESSED, now)
        if not (kind == EV_MSC and code == MSC_SCAN and source.remote and value in self.codes):
            return None
        # A remote says its code again while held, and once a press: a fresh press is one after RELEASE_S without it.
        last, self._heard[value] = self._heard.get(value), now
        fresh = last is None or now - last >= RELEASE_S
        button = self.codes[value]
        if button in (UP, DOWN):
            return self._volume(button, fresh, now)
        return button if fresh else None

    def _volume(self, button: str, fresh: bool, now: float) -> str | None:
        if not fresh and self._step and self._step[0] == button and now - self._step[1] < REPEAT_S:
            return None
        self._step = (button, now)
        return button


class Listener:
    """Reads the input devices in a thread of its own, and calls `act[button]()` for each press, which says what it
    did; `say(line)` for what's logged. A button with nothing to do is skipped. Devices that come or go (the
    speakerphone, unplugged and back) are found again every RESCAN_S."""

    def __init__(
        self,
        buttons: Buttons,
        act: dict[str, Callable[[], str]],
        say: Callable[[str], None] = print,
        find: Callable[[], list[Input]] = inputs,
        clock: Callable[[], float] = time.monotonic,
        each: Callable[[Input, int, int, int], None] | None = None,
    ):
        self.buttons, self.act, self.say, self.find, self.clock = buttons, act, say, find, clock
        self.each = each  # every event read, as it is: main()'s, to show what a button sends
        self.open: dict[int, Input] = {}
        self._refused: set[str] = set()
        self.stop = threading.Event()

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="buttons", daemon=True)
        thread.start()
        return thread

    def rescan(self) -> None:
        held = {i.path for i in self.open.values()}
        for found in self.find():
            if found.path in held:
                continue
            try:
                self.open[os.open(found.path, os.O_RDONLY | os.O_NONBLOCK)] = found
            except PermissionError:
                if found.path not in self._refused:
                    self._refused.add(found.path)
                    self.say(f"(buttons: can't read {found.name} ({found.path}): is TARS's user in the input group? "
                             "docs/home.md#buttons)")  # fmt: skip
            except OSError:
                pass  # gone already

    def run(self) -> None:
        next_scan = 0.0
        while not self.stop.is_set():
            if self.clock() >= next_scan:
                self.rescan()
                next_scan = self.clock() + RESCAN_S
            if not self.open:
                self.stop.wait(1.0)
                continue
            ready, _, _ = select.select(list(self.open), [], [], 1.0)
            for fd in ready:
                self.read(fd)

    def read(self, fd: int) -> None:
        source = self.open[fd]
        try:
            data = os.read(fd, EVENT.size * 64)
        except BlockingIOError:
            return
        except OSError:  # unplugged
            del self.open[fd]
            with contextlib.suppress(OSError):
                os.close(fd)
            return
        for at in range(0, len(data) - EVENT.size + 1, EVENT.size):
            _, _, kind, code, value = EVENT.unpack_from(data, at)
            value = value & 0xFFFFFFFF if kind == EV_MSC else value  # a code is unsigned
            if self.each:
                self.each(source, kind, code, value)
            button = self.buttons.feed(source, kind, code, value, self.clock())
            if button is None or button not in self.act:
                continue
            try:
                said = self.act[button]()
            except Exception as e:  # noqa: BLE001 - a button that didn't work never stops the listening
                said = f"error: {e}"
            self.say(f"(buttons: {source.name}, {button.replace('_', ' ')}: {said})")


def main() -> None:
    """`python -m voice_assistant.buttons`: what each button sends, to name a remote's in [home] remote."""
    found = inputs()
    receivers = [i for i in found if i.remote]
    if not receivers:
        print("No IR receiver: turn it on (dtoverlay=gpio-ir in /boot/firmware/config.txt), docs/home.md#buttons")
    watched = receivers + [i for i in found if not i.remote]
    print(f"Watching {', '.join(i.name for i in watched)}.")
    print("Hold each of the remote's buttons a second, one at a time (Ctrl-C to stop).")
    seen: set[tuple[str, int]] = set()

    def show(source: Input, kind: int, code: int, value: int) -> None:
        if kind == EV_MSC and code == MSC_SCAN and source.remote and (source.name, value) not in seen:
            seen.add((source.name, value))
            print(f"  {source.name}: {value:#x}")
        elif kind == EV_KEY and code in (KEY_VOLUMEUP, KEY_VOLUMEDOWN) and value == PRESSED:
            print(f"  {source.name}: volume {'up' if code == KEY_VOLUMEUP else 'down'} key (volume_keys)")

    try:
        Listener(Buttons({}, keys=False), {}, print, lambda: watched, each=show).run()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
