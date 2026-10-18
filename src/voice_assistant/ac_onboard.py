"""Teaching TARS the AC's remote, once (temporary: for an AC whose remote nobody has written down yet).

    uv run python -m voice_assistant.ac_onboard check     # the blaster answers, with config.toml's settings
    uv run python -m voice_assistant.ac_onboard capture   # press the remote's buttons, one by one, at the blaster
    uv run python -m voice_assistant.ac_onboard fit       # work out the code, and check it against every press
    uv run python -m voice_assistant.ac_onboard try       # send TARS's own codes to the AC, and say what it did
    uv run python -m voice_assistant.ac_onboard send "cool 22 auto" [--toggle]

`capture` keeps every press, with what the remote's screen showed, in voice_data/ac/captures.json. `fit` reads each
press into bits (Manchester, as Electra's remotes send them, or the pulse-distance most remotes use), finds which bits
are the power toggle, the mode, the temperature, the fan and the swing by comparing presses that differ in one thing
only, and writes voice_data/ac/profile.json only if, from those, it makes every press it heard again bit for bit (a
mode that can't be is left out, and the rest fitted again without it).
`try` is the last word: the AC itself, doing what TARS sends.

Nothing here assumes the layout: one published for Electra's remotes (github.com/barakwei/IRelectra: 34 bits,
Manchester, power as a toggle) is what to expect, and `fit` says whether it's what this remote does."""

import argparse
import json
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .ac import (
    DISTANCE,
    FAN,
    FANS,
    FOLDER,
    MANCHESTER,
    MODE,
    MODES,
    NO_PRESS,
    POWER,
    PRESS,
    PROFILE,
    STATE,
    SWING,
    TEMPERATURE,
    TUYA_KEY,
    Blaster,
    Profile,
    Settings,
    State,
    with_checksum,
)

CAPTURES = "captures.json"
FIELDS = (POWER, TEMPERATURE, FAN, SWING, MODE)  # the order positions are claimed in: mode gets what's left
GAP_UNITS = 8  # in Manchester, a space this many half-bits long is between frames, not in one
MAX_HEADER_UNITS = 8


# Reading a press.


@dataclass
class Decoded:
    scheme: str
    bits: str
    repeats: int
    timing: dict


def decode(pulses: list[int]) -> Decoded | None:
    """A press, as bits: the first scheme that reads it cleanly, every frame the same; None if neither does."""
    if len(pulses) < 10:
        return None
    return _manchester(pulses) or _distance(pulses)


def _unit(durations: list[int]) -> float:
    """The shortest duration there's a lot of: Manchester's half-bit."""
    ordered = sorted(durations)
    base = ordered[len(ordered) // 20]
    return statistics.median(d for d in ordered if d <= base * 1.5)


def _manchester(pulses: list[int]) -> Decoded | None:
    unit = _unit(pulses)
    segments, gaps, levels = [], [], ""
    for i, p in enumerate(pulses):
        n = max(1, round(p / unit))
        if i % 2 and n > GAP_UNITS:  # a long space: the frames on either side are separate
            segments.append(levels)
            gaps.append(p)
            levels = ""
        else:
            levels += ("0" if i % 2 else "1") * n
    segments.append(levels)
    frames, shape = [], None
    for segment in segments:
        found = _frames(segment)
        if not found:
            return None
        bits, repeats, header, trailer, one = found
        if shape not in (None, (header, trailer, one)):
            return None
        shape = (header, trailer, one)
        frames += [bits] * repeats
    if len(set(frames)) != 1:
        return None
    header, trailer, one = shape
    gap = round(statistics.median(gaps)) if gaps else 0
    timing = {"unit": round(unit, 1), "header": list(header), "trailer": trailer, "one": one, "gap": gap}
    return Decoded(MANCHESTER, frames[0], len(frames), timing)


def _frames(levels: str) -> tuple[str, int, tuple[int, int], int, str] | None:
    """`levels` (half-bits, 1 = mark) as (bits, repeats, (header mark, header space), trailer, one): repeats of a
    header and Manchester data, then a trailer mark, the way it's built again by ac.frame_pulses."""
    best = None
    for head_mark in range(2, MAX_HEADER_UNITS + 1):
        if not levels.startswith("1" * head_mark):
            continue
        for head_space in range(2, MAX_HEADER_UNITS + 1):
            for trailer in range(1, MAX_HEADER_UNITS + 1):
                for repeats in range(1, 7):
                    data = len(levels) - trailer - repeats * (head_mark + head_space)
                    if data <= 0 or data % (2 * repeats):
                        continue
                    half = data // repeats
                    start = head_mark + head_space
                    pairs = levels[start : start + half]
                    if any(pairs[i : i + 2] not in ("01", "10") for i in range(0, half, 2)):
                        continue
                    frame = "1" * head_mark + "0" * head_space + pairs
                    if frame * repeats + "1" * trailer != levels:
                        continue
                    bits = "".join("1" if pairs[i : i + 2] == "01" else "0" for i in range(0, half, 2))
                    found = (bits, repeats, (head_mark, head_space), trailer, "01")
                    if best is None or repeats > best[1]:
                        best = found
    return best


def _distance(pulses: list[int]) -> Decoded | None:
    """Every bit a short mark, then a short space (0) or a long one (1), after a long header mark; frames end with a
    mark and, between them, a long gap."""
    marks = pulses[0::2]
    short = statistics.median(marks)
    heads = [i for i in range(0, len(pulses), 2) if pulses[i] > 2.5 * short]
    if not heads:
        return None
    frames, timing_seen = [], {"header": [], "mark": [], "spaces": [], "trailer": [], "gap": []}
    for n, h in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(pulses)
        body = pulses[h:end]
        if len(body) < 4:
            return None
        timing_seen["header"].append(body[:2])
        rest = body[2:]
        if len(rest) % 2 == 0:  # a gap after the trailer mark
            timing_seen["gap"].append(rest[-1])
            rest = rest[:-1]
        *pairs, trailer = rest
        timing_seen["trailer"].append(trailer)
        spaces = pairs[1::2]
        if not spaces:
            return None
        timing_seen["mark"] += pairs[0::2]
        timing_seen["spaces"] += spaces
        frames.append(spaces)
    every = sorted(timing_seen["spaces"])
    threshold = (every[0] + every[-1]) / 2
    if every[-1] < 1.6 * every[0]:
        return None  # one length of space: not pulse-distance
    bit_frames = ["".join("1" if s > threshold else "0" for s in f) for f in frames]
    if len(set(bit_frames)) != 1:
        return None
    zeros = [s for s in every if s <= threshold]
    ones = [s for s in every if s > threshold]
    timing = {
        "header": [
            round(statistics.median(h[0] for h in timing_seen["header"])),
            round(statistics.median(h[1] for h in timing_seen["header"])),
        ],
        "mark": round(statistics.median(timing_seen["mark"])),
        "zero": round(statistics.median(zeros)),
        "one": round(statistics.median(ones)),
        "trailer": round(statistics.median(timing_seen["trailer"])),
        "gap": round(statistics.median(timing_seen["gap"])) if timing_seen["gap"] else 40000,
    }
    return Decoded(DISTANCE, bit_frames[0], len(bit_frames), timing)


# What each bit means.


@dataclass
class Capture:
    """One press: what the remote's screen showed after it, whether it was the power button, and what was sent."""

    settings: Settings
    power: bool
    pulses: list[int]
    step: str = ""

    def labels(self) -> dict[str, str]:
        s = self.settings
        return {
            POWER: PRESS if self.power else NO_PRESS,
            MODE: s.mode,
            TEMPERATURE: str(s.temperature),
            FAN: s.fan,
            SWING: "on" if s.swing else "off",
        }


@dataclass
class Fit:
    profile: dict | None  # None: nothing that could be trusted
    report: list[str]


def fit(captures: list[Capture]) -> Fit:
    report: list[str] = []
    decoded = [(c, decode(c.pulses)) for c in captures]
    unread = [c.step or str(i) for i, (c, d) in enumerate(decoded, 1) if d is None]
    if unread:
        report.append(f"couldn't read {len(unread)} press(es): {', '.join(unread)}; capture them again")
    read = [(c, d) for c, d in decoded if d]
    if len(read) < 4:
        return Fit(None, [*report, "too few presses read to work anything out"])
    shapes = {(d.scheme, len(d.bits)) for _, d in read}
    scheme, width = max(shapes, key=lambda shape: sum((d.scheme, len(d.bits)) == shape for _, d in read))
    if len(shapes) > 1:
        report.append(f"presses read as different shapes {sorted(shapes)}; using {scheme}, {width} bits")
    read = [(c, d) for c, d in read if (d.scheme, len(d.bits)) == (scheme, width)]
    report.append(f"read {len(read)} presses: {scheme}, {width} bits, sent {max(d.repeats for _, d in read)} times")
    labels = [c.labels() for c, _ in read]
    bits = [d.bits for _, d in read]

    # Same screen, same code: the press itself adds nothing (a clock, a counter) the profile can't make.
    seen: dict[tuple, str] = {}
    for label, b in zip(labels, bits, strict=True):
        key = tuple(sorted(label.items()))
        if seen.setdefault(key, b) != b:
            return Fit(None, [*report, f"the same screen ({_said(label)}) sent different codes: a clock or counter?"])

    # A last byte that's the sum of the others is made, not learned: its bits change with every setting.
    checksum = _checksum(bits, width)
    if checksum:
        report.append(f"the last byte is a checksum (the sum of the others, {checksum['order']} first)")
    settings_width = width - 8 if checksum else width
    varying = {i for i in range(settings_width) if len({b[i] for b in bits}) > 1}
    claimed: dict[int, str] = {}
    positions: dict[str, list[int]] = {}
    for name in FIELDS:
        changed: set[int] = set()
        for a in range(len(bits)):
            for b in range(a + 1, len(bits)):
                differ = [k for k in FIELDS if labels[a][k] != labels[b][k]]
                if differ == [name]:
                    changed |= {i for i in range(settings_width) if bits[a][i] != bits[b][i]}
        mine = sorted(i for i in changed if i not in claimed)
        if not mine:
            if len({label[name] for label in labels}) > 1:
                report.append(f"{name}: no two presses differ in {name} alone, so its bits aren't known")
            continue
        positions[name] = mine
        claimed |= dict.fromkeys(mine, name)
    if unexplained := sorted(varying - set(claimed)):
        report.append(f"bits {unexplained} change but follow no setting (a checksum TARS doesn't know?)")
        return Fit(None, report)

    fields: dict[str, dict] = {}
    for name, at in positions.items():
        values: dict[str, str] = {}
        for label, b in zip(labels, bits, strict=True):
            code = "".join(b[i] for i in at)
            if values.setdefault(label[name], code) != code:
                report.append(f"{name} {label[name]!r} came out as two codes; it depends on something else too")
        if len(set(values.values())) != len(values):
            report.append(f"{name}: two values share a code: {values}")
        if name == TEMPERATURE:
            values = _temperatures(values, report)
        fields[name] = {"at": at, "values": values}
        report.append(f"{name}: bits {at[0]}..{at[-1]}, {_values(name, values)}")
    if POWER in fields and set(fields[POWER]["values"]) != {PRESS, NO_PRESS}:
        report.append("power: needs presses with and without the power button")
        del fields[POWER]

    timing = _timing([d for _, d in read], scheme)
    template = bits[0]
    profile = {
        "scheme": scheme,
        "timing": timing,
        "repeats": max(d.repeats for _, d in read),
        "bits": template,
        "fields": fields,
        "checksum": checksum,
        "checked": False,
    }
    # The test that counts: every press made again from its screen, bit for bit.
    wrong = []
    for (c, d), label in zip(read, labels, strict=True):
        try:
            made = _bits(profile, label)
        except KeyError:
            made = None
        if made != d.bits:
            wrong.append((c, label))
    report.append(f"made {len(read) - len(wrong)} of {len(read)} presses again, bit for bit")
    if wrong and MODE not in fields:
        return Fit(None, [*report, *(f"press {c.step or _said(label)} isn't made again exactly" for c, label in wrong)])
    if wrong:  # a mode that writes some setting its own way: left out, rather than sent wrong, and fitted again
        left_out = {label[MODE] for _, label in wrong}
        for c, label in wrong:
            report.append(f"press {c.step or _said(label)} isn't made again exactly, so {label[MODE]} mode is left out")
        kept = [c for (c, _), label in zip(read, labels, strict=True) if label[MODE] not in left_out]
        again = fit(kept)
        return Fit(again.profile, [*report, "fitted again without those modes:", *again.report])
    if POWER not in fields:
        return Fit(None, [*report, "no power bit found: TARS could set the AC but never turn it on or off"])
    if not fields.get(MODE, {"values": {None: ""}})["values"]:
        return Fit(None, [*report, "no mode is made again exactly"])
    return Fit(profile, report)


def _bits(profile: dict, label: dict[str, str]) -> str:
    bits = list(profile["bits"])
    for name, f in profile["fields"].items():
        for at, bit in zip(f["at"], f["values"][label[name]], strict=True):
            bits[at] = bit
    frame = "".join(bits)
    return with_checksum(frame, profile["checksum"]) if profile["checksum"] else frame


def _checksum(bits: list[str], width: int) -> dict | None:
    """sum8, if every press's last byte is the sum of the others (each byte read lowest bit first, or highest) and
    it isn't the same in all of them."""
    if width % 8 or width < 16 or len({b[-8:] for b in bits}) < 2:
        return None
    for order in ("lsb", "msb"):
        if all(with_checksum(b, {"order": order}) == b for b in bits):
            return {"kind": "sum8", "order": order}
    return None


def _temperatures(values: dict[str, str], report: list[str]) -> dict[str, str]:
    """Every temperature from the lowest heard to the highest, from the ones heard: their codes are a count, up or
    down, read either way round."""
    heard = {int(t): code for t, code in values.items()}
    width = len(next(iter(heard.values())))
    for order in (1, -1):
        for sign in (1, -1):
            read = {t: int(code[::order], 2) for t, code in heard.items()}
            offsets = {read[t] - sign * t for t in read}
            if len(offsets) != 1:
                continue
            offset = offsets.pop()
            low, high = min(heard), max(heard)
            made = {}
            for t in range(low, high + 1):
                n = sign * t + offset
                if not 0 <= n < 2**width:
                    break
                made[str(t)] = format(n, f"0{width}b")[::order]
            else:
                if len(heard) < len(made):
                    report.append(f"temperature: heard {len(heard)}, the rest from {low} to {high} counted from them")
                return made
    report.append("temperature: the codes don't count up or down; only the temperatures heard can be set")
    return values


def _timing(decoded: list[Decoded], scheme: str) -> dict:
    """The timing every press agrees on: the middle value of each."""
    first = decoded[0].timing
    timing = {}
    for key, value in first.items():
        if isinstance(value, list):
            timing[key] = [round(statistics.median(d.timing[key][i] for d in decoded)) for i in range(len(value))]
        elif isinstance(value, str):
            timing[key] = value
        else:
            middle = statistics.median(d.timing[key] for d in decoded)
            timing[key] = round(middle, 1) if key == "unit" else round(middle)
    return timing


def _values(name: str, values: dict[str, str]) -> str:
    if name == TEMPERATURE:
        temps = sorted(int(t) for t in values)
        return f"{temps[0]} to {temps[-1]} degrees" if temps else "none"
    return ", ".join(f"{v}={code}" for v, code in values.items())


def _said(label: dict[str, str]) -> str:
    power = ", power pressed" if label[POWER] == PRESS else ""
    swing = ", swing" if label[SWING] == "on" else ""
    return f"{label[MODE]} {label[TEMPERATURE]} {label[FAN]}{swing}{power}"


# What the remote's screen shows.


def parse_screen(text: str, before: Settings) -> Settings:
    """ "heat 24 auto", "cool 22 high swing", "fan - low": mode, temperature (- if it isn't shown: unchanged), fan,
    and "swing" or "noswing"; anything left out is as `before`. ValueError says what's wrong."""
    words = text.lower().replace(",", " ").split()
    s = Settings(**vars(before))
    fans = {"1": "low", "2": "medium", "3": "high", "a": "auto", "med": "medium", "mid": "medium"}
    if words and words[-1] in ("swing", "noswing"):
        s.swing = words.pop() == "swing"
    if len(words) > 3:
        raise ValueError("too many words: mode, temperature, fan, then swing or noswing")
    if words:
        if words[0] not in MODES:
            raise ValueError(f"the mode is one of {', '.join(MODES)}, not {words[0]!r}")
        s.mode = words[0]
    if len(words) > 1 and words[1] != "-":
        if not words[1].isdigit() or not 10 <= int(words[1]) <= 35:
            raise ValueError(f"the temperature is a number of degrees, not {words[1]!r}")
        s.temperature = int(words[1])
    if len(words) > 2:
        fan = fans.get(words[2], words[2])
        if fan not in FANS:
            raise ValueError(f"the fan is one of {', '.join(FANS)}, not {words[2]!r}")
        s.fan = fan
    return s


@dataclass
class Step:
    """One press to capture: what to set up first (not captured), the screen after setting it up (`first`), the
    button, and the screen expected after pressing it."""

    name: str
    setup: str
    button: str
    expect: Callable[[Settings], Settings]
    power: bool = False
    first: Callable[[Settings], Settings] = lambda s: s


def _with(**changes) -> Callable[[Settings], Settings]:
    return lambda s: Settings(**{**vars(s), **changes})


def _next(values: tuple[str, ...], key: str) -> Callable[[Settings], Settings]:
    def go(s: Settings) -> Settings:
        now = getattr(s, key)
        return Settings(**{**vars(s), key: values[(values.index(now) + 1) % len(values)]})

    return go


COOL_23 = {"mode": "cool", "temperature": 23, "fan": "auto", "swing": False}
STEPS = [
    Step(
        "temp up",
        "Set the remote to COOL, 23 degrees, fan AUTO, swing off (aim it away from the blaster).",
        "TEMP UP",
        _with(temperature=24),
        first=_with(**COOL_23),
    ),
    Step("temp up again", "", "TEMP UP", _with(temperature=25)),
    Step("temp down", "", "TEMP DOWN", _with(temperature=24)),
    Step("coldest", "Set it to 17 degrees (aimed away).", "TEMP DOWN", _with(temperature=16)),
    Step("warmest", "Set it to 29 degrees (aimed away).", "TEMP UP", _with(temperature=30)),
    Step("middle", "Set it to 20 degrees (aimed away).", "TEMP UP", _with(temperature=21)),
    Step("fan 1", "Set it back to 24 degrees (aimed away).", "FAN", _next(FANS, "fan"), first=_with(temperature=24)),
    Step("fan 2", "", "FAN", _next(FANS, "fan")),
    Step("fan 3", "", "FAN", _next(FANS, "fan")),
    Step("fan 4", "", "FAN", _next(FANS, "fan")),
    Step("mode 1", "", "MODE", _next(MODES, "mode")),
    Step("mode 2", "", "MODE", _next(MODES, "mode")),
    Step("mode 3", "", "MODE", _next(MODES, "mode")),
    Step("mode 4", "", "MODE", _next(MODES, "mode")),
    Step("mode 5", "", "MODE", _next(MODES, "mode")),
    Step(
        "swing on",
        "If it isn't on COOL 24, fan AUTO now, set it so (aimed away).",
        "SWING",
        _with(swing=True),
        first=_with(**{**COOL_23, "temperature": 24}),
    ),
    Step("swing off", "", "SWING", _with(swing=False)),
    Step("power on", "Now the AC should turn ON with this press.", "POWER", lambda s: s, power=True),
    Step("power off", "And OFF with this one.", "POWER", lambda s: s, power=True),
]


# The commands.


def _load_captures(folder: Path) -> list[Capture]:
    path = folder / CAPTURES
    if not path.exists():
        return []
    return [
        Capture(Settings(**c["settings"]), c["power"], c["pulses"], c.get("step", ""))
        for c in json.loads(path.read_text())
    ]


def _save_captures(folder: Path, captures: list[Capture]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    rows = [{"step": c.step, "settings": vars(c.settings), "power": c.power, "pulses": c.pulses} for c in captures]
    (folder / CAPTURES).write_text(json.dumps(rows, indent=1))


def blaster_from_config(config: Path) -> Blaster:
    from dotenv import dotenv_values

    from .config import load_config

    blaster = load_config(config).home.blaster
    if not blaster:
        raise SystemExit("config.toml has no [home] blaster yet: see docs/home.md")
    key = dotenv_values(config.resolve().parent / ".env").get(TUYA_KEY)
    if not key:
        raise SystemExit(f"Put the blaster's local key in .env as {TUYA_KEY} (docs/home.md says how to get it)")
    return Blaster(blaster["id"], blaster["address"], key, blaster.get("version", "3.3"))


def ask(prompt: str, default: str = "") -> str:
    answer = input(f"{prompt}{f' [{default}]' if default else ''} ").strip()
    return answer or default


def yes(prompt: str) -> bool:
    return ask(f"{prompt} (y/n)").lower().startswith("y")


def capture(blaster: Blaster, folder: Path, again: str | None) -> None:
    captures = _load_captures(folder)
    done = {c.step for c in captures}
    todo = [s for s in STEPS if s.name == again] if again else [s for s in STEPS if s.name not in done]
    if again and not todo:
        raise SystemExit(f"no step called {again!r}: {', '.join(s.name for s in STEPS)}")
    if not todo:
        print("Every step is captured. Next: fit (or capture --again STEP to redo one).")
        return
    print(
        "Start with the AC OFF. Point the remote at the blaster for each press, from about 30 cm.\n"
        "After each press, type what the remote's screen shows: mode, temperature, fan, and 'swing' if it's on,\n"
        "like 'cool 24 auto' or 'heat 26 high swing'; just Enter if it's what's in brackets.\n"
        "Buttons other than POWER shouldn't turn the AC on. If one does, say so at the end.\n"
    )
    screen = captures[-1].settings if captures and not again else Settings("cool", 23, "auto", False)
    for step in todo:
        if again:
            screen = next((c.settings for c in captures if c.step == step.name), screen)
        if step.setup:
            input(f"{step.setup}\n  Enter when ready. ")
        expected = step.expect(step.first(screen))
        for attempt in range(3):
            print(f"  Press {step.button} now ({step.name})...", flush=True)
            pulses = blaster.learn()
            if pulses and decode(pulses):
                break
            print("  That didn't come through whole." + (" Press it again." if attempt < 2 else ""))
        else:
            print(f"  Skipped {step.name}: capture it later with: capture --again {step.name!r}")
            continue
        while True:
            try:
                screen = parse_screen(ask("  The screen shows:", _plain(expected)), expected)
                break
            except ValueError as e:
                print(f"  {e}")
        captures = [c for c in captures if c.step != step.name] + [Capture(screen, step.power, pulses, step.name)]
        _save_captures(folder, captures)
    if not again and yes("Did the AC turn on or off at any press but POWER?"):
        print("Then its power may not be a toggle: run fit, and send me what it says.")
    print(f"Saved {len(captures)} presses in {folder / CAPTURES}. Next: fit")


def _plain(s: Settings) -> str:
    return f"{s.mode} {s.temperature} {s.fan}{' swing' if s.swing else ''}"


def run_fit(folder: Path) -> int:
    captures = _load_captures(folder)
    if not captures:
        raise SystemExit("Nothing captured yet: run capture first.")
    result = fit(captures)
    for line in result.report:
        print(f"- {line}")
    if result.profile is None:
        print("\nNo profile written: nothing could be trusted. Send voice_data/ac/captures.json and this output.")
        return 1
    (folder / PROFILE).write_text(json.dumps(result.profile, indent=1))
    print(f"\nWrote {folder / PROFILE}. Next: try (with the AC in view).")
    return 0


def run_try(blaster: Blaster, folder: Path) -> int:
    profile = Profile.load(folder / PROFILE)
    modes, fans = profile.values(MODE), profile.values(FAN)
    temps = [int(t) for t in profile.values(TEMPERATURE)]
    if len(temps) < 2:
        print("The profile knows fewer than two temperatures: capture more of them, and fit again.")
        return 1
    mode = "cool" if "cool" in modes or not modes else modes[0]
    fan = "auto" if "auto" in fans or not fans else fans[0]
    start = min(temps, key=lambda t: abs(t - 24))
    other = min((t for t in temps if t != start), key=lambda t: abs(t - start - 2))
    cool, warmer = Settings(mode, start, fan), Settings(mode, other, fan)
    said = f"{mode.upper()} {start}"
    print("Turn the AC OFF with its own remote, and watch it. Each step sends one code from TARS.\n")
    input("Enter when it's off. ")
    blaster.send(profile.pulses(cool, toggle=False))
    if not yes(f"1. Sent {said} without the power bit. It should beep (maybe) and stay OFF. Did it stay off?"):
        print("Then the settings code turns it on: the power bit isn't a toggle. Send me what fit said.")
        return 1
    blaster.send(profile.pulses(cool, toggle=True))
    if not yes(f"2. Sent {said} with the power bit. Did it turn ON?"):
        print("Check the blaster can see the AC (try the Smart Life app from where it stands), then try again.")
        return 1
    blaster.send(profile.pulses(warmer, toggle=False))
    if not yes(f"3. Sent {mode.upper()} {other} without the power bit. Is it still ON, and set to {other}?"):
        print("Then a settings change doesn't work as the remote's does. Send me what fit said.")
        return 1
    blaster.send(profile.pulses(warmer, toggle=True))
    if not yes("4. Sent the power bit again. Did it turn OFF?"):
        print("It may have missed it: try again from a clearer spot.")
        return 1
    raw = json.loads((folder / PROFILE).read_text())
    raw["checked"] = True
    (folder / PROFILE).write_text(json.dumps(raw, indent=1))
    State(on=False, settings=warmer, how="told").save(folder / STATE)
    print("\nThe AC does what TARS sends. The profile is marked checked, and TARS knows the AC is off.")
    return 0


def run_send(blaster: Blaster, folder: Path, screen: str, toggle: bool) -> None:
    profile = Profile.load(folder / PROFILE)
    settings = parse_screen(screen, Settings())
    blaster.send(profile.pulses(settings, toggle))
    print(f"Sent {_plain(settings)}{' with the power bit' if toggle else ''}.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m voice_assistant.ac_onboard", description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["check", "capture", "fit", "try", "send"])
    parser.add_argument("screen", nargs="?", help='send: the settings, like "cool 22 auto"')
    parser.add_argument("--toggle", action="store_true", help="send: with the power bit")
    parser.add_argument("--again", metavar="STEP", help="capture: redo this step")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()
    folder = args.config.resolve().parent / FOLDER
    if args.command == "fit":
        raise SystemExit(run_fit(folder))
    blaster = blaster_from_config(args.config)
    if args.command == "check":
        try:
            blaster.connect()
        except Exception as e:  # noqa: BLE001 - what's wrong is the answer
            raise SystemExit(f"The blaster didn't answer: {e}") from None
        print("The blaster answers. Next: capture")
    elif args.command == "capture":
        capture(blaster, folder, args.again)
    elif args.command == "try":
        raise SystemExit(run_try(blaster, folder))
    elif args.command == "send":
        if not args.screen:
            raise SystemExit('send needs the settings, like: send "cool 22 auto"')
        run_send(blaster, folder, args.screen, args.toggle)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped; what was captured is kept.", file=sys.stderr)
