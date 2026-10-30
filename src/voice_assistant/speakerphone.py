"""The speakerphone's own mute, as TARS's: its mute button mutes its mic in the speakerphone itself (its light goes
red), and TARS can mute and unmute it the same way, so a remote's mute button and the speakerphone's are one mute,
never two that disagree. While it's muted TARS doesn't listen for its name (assistant.py).

A USB speakerphone says what its buttons and lights are in its HID report descriptor, in the USB standard's
telephony terms: a "phone mute" button it reports pressed and let go, and "mute" and "off-hook" lights the computer
sets. The PowerConf takes the computer's mute only while off-hook (in a call): off-hook with mute mutes it, and
off-hook without unmutes it, whichever muted it, its button too; on-hook, it keeps its own and the computer's mute is
ignored. So TARS keeps it off-hook (no light shows it) and sets only the mute. Its reports go through its hidraw
device (/dev/hidraw*), root's alone unless a udev rule says otherwise (deploy/99-tars-speakerphone.rules gives TARS's
user the PowerConf's).

The speakerphone doesn't say whether it's muted, only that its button was pressed, so TARS keeps count: unmuted
when TARS starts (it unmutes it then, to be sure), and flipped by each press. Linux only."""

import contextlib
import os
import select
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

TELEPHONY, LEDS = 0x0B, 0x08  # HID usage pages
PHONE_MUTE = 0x2F  # telephony: the mute button
MUTE_LED, OFF_HOOK_LED = 0x09, 0x17  # LEDs: mute, and off-hook (in a call)
INPUT, OUTPUT = 0x80, 0x90  # main items, tag and type bits


@dataclass(frozen=True)
class Bit:
    """Where one button or light is: its report, and its bit in that report's data (after the report id)."""

    report: int
    bit: int


@dataclass(frozen=True)
class Layout:
    """Where the mute button is in what the speakerphone sends, and the mute and off-hook lights in what it takes,
    with the length of that report's data."""

    button: Bit
    mute: Bit
    off_hook: Bit
    output_bytes: int


def layout(descriptor: bytes) -> Layout | None:
    """The speakerphone's mute, from its HID report descriptor, or None if it has no phone mute button with mute and
    off-hook lights. Reads the short items (a long item, rare, is skipped) and keeps the state each needs: the usage
    page, report id, size and count, and the usages listed since the last main item."""
    page = report = size = count = 0
    usages: list[tuple[int, int]] = []
    bits: dict[tuple[int, int], int] = {}  # (main item, report) -> bits so far
    found: dict[tuple[int, int, int], Bit] = {}  # (main item, page, usage) -> where
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        if prefix == 0xFE:  # a long item: its length is the next byte
            i += 3 + (descriptor[i + 1] if i + 1 < len(descriptor) else 0)
            continue
        length = (0, 1, 2, 4)[prefix & 0x03]
        data = descriptor[i + 1 : i + 1 + length]
        value = int.from_bytes(data, "little")
        tag = prefix & 0xFC
        i += 1 + length
        if tag == 0x04:
            page = value
        elif tag == 0x84:
            report = value
        elif tag == 0x74:
            size = value
        elif tag == 0x94:
            count = value
        elif tag == 0x08:  # a usage: with four bytes, its page in the top two
            usages.append((value >> 16, value & 0xFFFF) if length == 4 else (page, value))
        elif tag in (INPUT, OUTPUT):
            at = bits.get((tag, report), 0)
            # Padding lists no usages. A button marked constant still counts: the PowerConf marks its own so.
            for n, (usage_page, usage) in enumerate(usages[:count] if size == 1 else []):
                found.setdefault((tag, usage_page, usage), Bit(report, at + n))
            bits[(tag, report)] = at + size * count
            usages = []
        elif tag in (0xA0, 0xC0):  # a collection opens or closes
            usages = []
    try:
        button = found[(INPUT, TELEPHONY, PHONE_MUTE)]
        mute, off_hook = found[(OUTPUT, LEDS, MUTE_LED)], found[(OUTPUT, LEDS, OFF_HOOK_LED)]
    except KeyError:
        return None
    if mute.report != off_hook.report:
        return None
    return Layout(button, mute, off_hook, (bits[(OUTPUT, mute.report)] + 7) // 8)


@dataclass(frozen=True)
class Hid:
    """A speakerphone's hidraw device: its file, its name, and its report descriptor."""

    path: str
    name: str
    descriptor: bytes


def hids(sys_hidraw: Path = Path("/sys/class/hidraw"), dev: Path = Path("/dev")) -> list[Hid]:
    found = []
    for raw in sorted(sys_hidraw.glob("hidraw*")):
        try:
            device = (raw / "device").resolve()
            fields = dict(line.partition("=")[::2] for line in (device / "uevent").read_text().splitlines())
            found.append(
                Hid(str(dev / raw.name), fields.get("HID_NAME", ""), (device / "report_descriptor").read_bytes())
            )
        except OSError:
            continue
    return found


class SpeakerphoneMute:
    """The speakerphone's mute: `toggle()` from a button, `muted` now, and `on_change(muted)` each time it changes,
    whichever button changed it. Its presses are read in a thread of its own (`start()`)."""

    def __init__(self, path: str, layout: Layout, on_change: Callable[[bool], None] = lambda muted: None):
        self.path, self.layout, self.on_change = path, layout, on_change
        self.muted = False
        self._lock = threading.Lock()
        self._fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self._pressed = False  # the button's bit in the last report: a press is it going on
        self.stop = threading.Event()

    def _report(self, muted: bool) -> bytes:
        """Off-hook always, since on-hook it ignores the computer's mute; and the mute, on or off."""
        data = bytearray(self.layout.output_bytes)
        for bit in (self.layout.off_hook, *([self.layout.mute] if muted else [])):
            data[bit.bit // 8] |= 1 << bit.bit % 8
        return bytes([self.layout.mute.report]) + bytes(data)

    def set(self, muted: bool) -> None:
        """Mutes or unmutes it, as its own button does: the mic in the speakerphone, and its light."""
        with self._lock:
            os.write(self._fd, self._report(muted))
            changed, self.muted = self.muted != muted, muted
        if changed:
            self.on_change(muted)

    def toggle(self) -> str:
        self.set(not self.muted)
        return "muted" if self.muted else "unmuted"

    def pressed(self, report: bytes) -> bool:
        """Takes one report the speakerphone sent; True if it's the mute button being pressed, which has muted or
        unmuted it already. Letting go, and its other buttons' reports, are nothing."""
        button = self.layout.button
        if not report or report[0] != button.report or len(report) <= 1 + button.bit // 8:
            return False
        down = bool(report[1 + button.bit // 8] >> button.bit % 8 & 1)
        was, self._pressed = self._pressed, down
        if not down or was:
            return False
        with self._lock:
            self.muted = not self.muted
            muted = self.muted
        self.on_change(muted)
        return True

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="speakerphone-mute", daemon=True)
        thread.start()
        return thread

    def run(self) -> None:
        while not self.stop.is_set():
            ready, _, _ = select.select([self._fd], [], [], 1.0)
            if not ready:
                continue
            try:
                report = os.read(self._fd, 64)
            except BlockingIOError:
                continue
            except OSError:  # unplugged: TARS stops too, without its speakerphone
                with contextlib.suppress(OSError):
                    os.close(self._fd)
                return
            if self.pressed(report):
                print(f"(speakerphone: its mute button, {'muted' if self.muted else 'unmuted'})")


def find_mute(
    name: str, on_change: Callable[[bool], None], found: Callable[[], list[Hid]] = hids
) -> tuple[SpeakerphoneMute | None, str]:
    """The mute of the speakerphone whose name has `name` in it ([audio] output_device), unmuted to start with, or
    None, and why there's none."""
    if not name:
        return None, "[audio] output_device doesn't name the speakerphone"
    matching = [h for h in found() if name.lower() in h.name.lower()]
    if not matching:
        return None, f"no HID device named like {name!r}"
    for hid in matching:
        if (where := layout(hid.descriptor)) is None:
            continue
        try:
            mute = SpeakerphoneMute(hid.path, where, on_change)
            mute.set(False)
        except PermissionError:
            return None, f"TARS's user may not use {hid.path} (deploy/99-tars-speakerphone.rules)"
        except OSError as e:
            return None, f"{hid.path}: {e.strerror or e}"
        return mute, ""
    return None, f"{matching[0].name} has no mute button with a mute light"
