"""What a scenario is (a conversation, turn by turn, with what each turn should do), loaded from evals/scenarios/*.toml,
and the checks that say what a turn got wrong. Nothing here asks a model: evals.runner plays the turns."""

import hashlib
import json
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from voice_assistant.llm import FALLBACK, LOOKED_UP, PONDERED, QUICK
from voice_assistant.reminders import ADD, CANCEL, KINDS, SNOOZE, Change, due_at

FOLDER = Path(__file__).parent / "scenarios"

# Where a turn went: Qwen answered it, handed it over (to OpenAI for the web, to the thinking model), OpenAI answered
# because Qwen couldn't, or the reply was a <skip> (not meant for TARS) or an <ack> (a reminder acknowledged).
ROUTE_OF = {QUICK: "quick", LOOKED_UP: "look-up", PONDERED: "ponder", FALLBACK: "fallback"}
SKIPPED, ACKED = "skip", "ack"
ROUTES = (*ROUTE_OF.values(), SKIPPED, ACKED)
# How a turn reaches the brain, as the assistant tags it: after a reply without the wake word, answering TARS's
# "Did you call me?", or answering a reminder TARS just said.
TAGS = ("", "follow-up", "asked", "reminder")
# A failure for the quick model on one turn: its service down, or saying "too many requests". It then sits out its
# cooldown, as in TARS, so later turns can go to OpenAI too.
FAILURES = ("", "down", "limited")

ANY_TAG = re.compile(r"<\s*/?\s*[a-z][\w-]*(\s[^<>]*)?>", re.IGNORECASE)
ESCAPED_TAG = re.compile(r"&lt;\s*/?\s*[a-z][\w-]*", re.IGNORECASE)  # "&lt;look-up&gt;", unrecognized by TARS
LIST_LINE = re.compile(r"^\s*([-*•]|\d+\.)\s", re.MULTILINE)
LINK = re.compile(r"https?://|www\.|\b[\w-]+\.(com|org|net|io|co\.uk)\b", re.IGNORECASE)
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Expected:
    """A reminder change a turn should make (`action`), or one that should be active after it. Times: `in_minutes`
    from when it was asked, or `time` (HH:MM) on `day` (as TARS reads them: today, tomorrow, a weekday, a date), or
    `when_back` (when `for` is next heard). `ref` names it, for a later turn's cancel or snooze; `text`, if given,
    has to appear in what TARS will say (any case)."""

    action: str = ADD
    kind: str | None = None
    in_minutes: float | None = None
    day: str | None = None
    time: str | None = None
    when_back: bool = False
    for_name: str | None = None
    text: str | None = None
    ref: str | None = None
    minutes: float | None = None  # a snooze's


@dataclass
class ExpectedSend:
    kind: tuple[str, ...]
    entries: int | None = None


@dataclass
class Turn:
    say: str
    speaker: str = "alon"  # "unknown": a voice TARS doesn't know
    tag: str = ""
    route: tuple[str, ...] = ()  # any of these; empty = not checked
    reminders: list[Expected] | None = None  # exactly these changes; [] = none; None = not checked
    active: list[Expected] | None = None  # exactly these active after the turn; None = not checked
    active_has: list[Expected] = field(default_factory=list)  # at least these active after it
    sent: list[ExpectedSend] | None = None  # exactly these sent; [] = nothing; None = not checked
    says: list[str] = field(default_factory=list)  # each has to match (a regex, any case)
    never_says: list[str] = field(default_factory=list)
    quick: str = ""  # a failure for the quick model on this turn: FAILURES
    after_minutes: float = 0.0  # quiet before this turn (past [llm] memory_minutes, TARS starts fresh)


@dataclass
class Seed:
    """A reminder set before the conversation starts, from the web page. `said`: TARS has said it and is waiting for
    someone to acknowledge it."""

    ref: str
    kind: str
    text: str
    in_minutes: float
    for_name: str | None = None
    said: bool = False


@dataclass
class Scenario:
    id: str
    about: str
    turns: list[Turn]
    tags: list[str] = field(default_factory=list)
    seeds: list[Seed] = field(default_factory=list)
    file: str = ""
    fingerprint: str = ""  # of its definition: results are comparable only while it's unchanged


def load(folder: Path = FOLDER) -> list[Scenario]:
    """Every scenario in the folder's .toml files, checked: unknown settings, routes or references are errors."""
    scenarios, seen = [], set()
    for path in sorted(folder.glob("*.toml")):
        data = tomllib.loads(path.read_text())
        _only(data, {"scenario"}, path.name)
        for raw in data.get("scenario", []):
            s = _scenario(raw, path.name)
            if s.id in seen:
                raise ValueError(f"{path.name}: scenario {s.id!r} is defined twice")
            seen.add(s.id)
            scenarios.append(s)
    return scenarios


def _only(raw: dict, allowed: set[str], where: str) -> None:
    if unknown := set(raw) - allowed:
        raise ValueError(f"{where}: unknown setting(s) {sorted(unknown)}")


def _scenario(raw: dict, file: str) -> Scenario:
    where = f"{file}: {raw.get('id', '(no id)')}"
    _only(raw, {"id", "about", "tags", "seed", "turn"}, where)
    if not raw.get("id") or not raw.get("about") or not raw.get("turn"):
        raise ValueError(f"{where}: a scenario needs an id, an about and at least one turn")
    seeds = [_seed(s, where) for s in raw.get("seed", [])]
    refs = {s.ref for s in seeds}
    turns = []
    for n, t in enumerate(raw["turn"], 1):
        turn = _turn(t, f"{where}, turn {n}")
        for e in turn.reminders or []:
            if e.action in (CANCEL, SNOOZE) and e.ref not in refs:
                raise ValueError(f"{where}, turn {n}: {e.action} of {e.ref!r}, which isn't set before it")
            if e.action == ADD and e.ref:
                refs.add(e.ref)
        turns.append(turn)
    fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:12]
    return Scenario(raw["id"], raw["about"].strip(), turns, list(raw.get("tags", [])), seeds, file, fingerprint)


def _seed(raw: dict, where: str) -> Seed:
    _only(raw, {"ref", "kind", "text", "in_minutes", "for", "said"}, f"{where}, seed")
    if raw.get("kind") not in KINDS:
        raise ValueError(f"{where}, seed: kind must be one of {KINDS}")
    return Seed(raw["ref"], raw["kind"], raw["text"], float(raw["in_minutes"]), raw.get("for"), raw.get("said", False))


def _turn(raw: dict, where: str) -> Turn:
    keys = {"say", "speaker", "tag", "route", "reminders", "active", "active_has", "sent", "says", "never_says"}
    _only(raw, keys | {"quick", "after_minutes"}, where)
    route = raw.get("route", ())
    route = (route,) if isinstance(route, str) else tuple(route)
    if bad := [r for r in route if r not in ROUTES]:
        raise ValueError(f"{where}: route {bad} isn't one of {ROUTES}")
    if raw.get("tag", "") not in TAGS or raw.get("quick", "") not in FAILURES:
        raise ValueError(f"{where}: tag must be one of {TAGS}, quick one of {FAILURES}")
    for pattern in raw.get("says", []) + raw.get("never_says", []):
        re.compile(pattern)

    def expected(key: str) -> list[Expected] | None:
        return None if key not in raw else [_expected(e, f"{where}, {key}") for e in raw[key]]

    sent = None
    if "sent" in raw:
        sent = []
        for s in raw["sent"]:
            _only(s, {"kind", "entries"}, f"{where}, sent")
            kind = (s["kind"],) if isinstance(s["kind"], str) else tuple(s["kind"])
            sent.append(ExpectedSend(kind, s.get("entries")))
    return Turn(
        say=raw["say"],
        speaker=raw.get("speaker", "alon"),
        tag=raw.get("tag", ""),
        route=route,
        reminders=expected("reminders"),
        active=expected("active"),
        active_has=expected("active_has") or [],
        sent=sent,
        says=list(raw.get("says", [])),
        never_says=list(raw.get("never_says", [])),
        quick=raw.get("quick", ""),
        after_minutes=float(raw.get("after_minutes", 0)),
    )


def _expected(raw: dict, where: str) -> Expected:
    keys = {"action", "kind", "in_minutes", "day", "time", "when_back", "for", "text", "ref", "minutes"}
    _only(raw, keys, where)
    e = Expected(**{("for_name" if k == "for" else k): v for k, v in raw.items()})
    if e.action not in (ADD, CANCEL, SNOOZE):
        raise ValueError(f"{where}: action must be add, cancel or snooze")
    if e.action != ADD and not e.ref:
        raise ValueError(f"{where}: a {e.action} needs the ref of the reminder")
    if e.kind is not None and e.kind not in KINDS:
        raise ValueError(f"{where}: kind must be one of {KINDS}")
    if e.day is not None and e.time is None:
        raise ValueError(f"{where}: a day needs a time")
    return e


# The checks. Each returns what's wrong ("" if nothing), in words for the report.


def check_route(turn: Turn, route: str) -> str:
    return "" if not turn.route or route in turn.route else f"went to {route}, not {' or '.join(turn.route)}"


def check_spoken(said: str, route: str, raw: str = "") -> list[str]:
    """What TARS shouldn't say aloud, whatever the turn: tags, a list, a web address, emoji, a sentence said twice (as
    when a hand-off repeats what Qwen already said), or nothing at all. `said` is what's heard, after the voice's
    cleaning (which drops cited sources, links' addresses and markdown symbols); `raw`, the reply as written."""
    if route == SKIPPED:
        return [] if not said.strip() else [f"said something after <skip>: {said[:60]!r}"]
    wrong = []
    if not said.strip():
        wrong.append("said nothing")
    if m := ANY_TAG.search(said) or ESCAPED_TAG.search(raw):
        wrong.append(f"says a tag aloud: {m.group(0)!r}")
    if m := LIST_LINE.search(raw):
        wrong.append(f"says a list aloud: {raw[m.start() :].strip()[:40]!r}")
    if m := LINK.search(said):
        wrong.append(f"says a web address aloud: {m.group(0)!r}")
    if m := EMOJI.search(said):
        wrong.append(f"says an emoji: {m.group(0)!r}")
    sentences = [_plain(s) for s in SENTENCE_END.split(said.strip())]
    long = [s for s in sentences if len(s.split()) >= 4]
    if dupes := {s for s in long if long.count(s) > 1}:
        wrong.append(f"says this twice: {min(dupes)!r}")
    return wrong


def _plain(sentence: str) -> str:
    return re.sub(r"[^\w\s]", "", sentence.lower()).strip()


# What TARS says around a reminder's text when it's due ("Alon, a reminder from Stacey: ..."). Copied into the text, it
# would be said twice.
SPOKEN_FRAME = re.compile(r"^\s*(\w+(\s\w+)?,\s+)?(a|your)\s+(reminder|message)\b(\s+from\s+\w+)?\s*:", re.IGNORECASE)


def check_reminder_text(changes: list[Change]) -> list[str]:
    """A reminder's text is what it's about, never TARS's words around it."""
    return [
        f"a reminder's text repeats what TARS says around it: {c.reminder.text!r}"
        for c in changes
        if c.action == ADD and c.reminder and SPOKEN_FRAME.match(c.reminder.text or "")
    ]


def check_text(turn: Turn, said: str) -> list[str]:
    wrong = [f"doesn't say /{p}/" for p in turn.says if not re.search(p, said, re.IGNORECASE)]
    return wrong + [f"says /{p}/" for p in turn.never_says if re.search(p, said, re.IGNORECASE)]


def due_ok(e: Expected, due: float | None, base: float, slack_s: float) -> bool:
    """Whether `due` is when `e` asks for, worked out from `base` (when it was asked)."""
    if e.when_back:
        return due is None
    if due is None:
        return e.in_minutes is None and e.time is None
    if e.in_minutes is not None:
        return abs(due - (base + e.in_minutes * 60)) <= slack_s
    if e.time is not None:
        return abs(due - due_at(e.day, e.time, base)) <= max(slack_s, 60)
    return True


def _same_person(want: str | None, got: str | None) -> bool:
    return want is None or (got or "").lower() == want.lower()


def change_matches(e: Expected, c: Change, refs: dict[str, int]) -> bool:
    if e.action != c.action:
        return False
    if c.action in (CANCEL, SNOOZE):
        return c.id == refs.get(e.ref) and (e.minutes is None or c.minutes == e.minutes)
    r = c.reminder
    return (
        (e.kind is None or r.kind == e.kind)
        and _same_person(e.for_name, r.for_name)
        and (e.text is None or e.text.lower() in (r.text or "").lower())
        and due_ok(e, r.due, c.asked_at, 2.0)
    )


def row_matches(e: Expected, row: dict, base: float, slack_s: float) -> bool:
    return (
        (e.kind is None or row["kind"] == e.kind)
        and _same_person(e.for_name, row["for_name"])
        and (e.text is None or e.text.lower() in (row["text"] or "").lower())
        and due_ok(e, row["next_at"], base, slack_s)
    )


def pair(expected: list, got: list, matches) -> tuple[dict[int, int], list, list]:
    """Pairs each expected with something got that it matches: ({expected index: got index}, the expected left
    unmatched, what was got and not expected)."""
    paired, used = {}, set()
    for i, e in enumerate(expected):
        for j, g in enumerate(got):
            if j not in used and matches(e, g):
                paired[i] = j
                used.add(j)
                break
    return (
        paired,
        [e for i, e in enumerate(expected) if i not in paired],
        [g for j, g in enumerate(got) if j not in used],
    )


def describe_change(c: Change) -> str:
    if c.action == CANCEL:
        return f"cancel #{c.id}"
    if c.action == SNOOZE:
        return f"snooze #{c.id} {c.minutes:g} min"
    r = c.reminder
    at = "when back" if r.due is None else f"in {(r.due - c.asked_at) / 60:.0f} min, {_clock(r.due)}"
    return f"add {r.kind} {r.text!r} for {r.for_name or 'whoever'}, {at}"


def describe_row(row: dict) -> str:
    at = "when back" if row["next_at"] is None else _clock(row["next_at"])
    return f"{row['kind']} {row['text']!r} for {row['for_name'] or 'whoever'}, {at}"


def describe(e: Expected) -> str:
    """What `e` asks for: a change ("cancel pasta"), or a reminder, without the action, for an add or an active one."""
    if e.action != ADD:
        return f"{e.action} {e.ref}" + (f" {e.minutes:g} min" if e.minutes else "")
    if e.when_back:
        at = "when back"
    elif e.in_minutes is not None:
        at = f"in {e.in_minutes:g} min"
    else:
        at = f"{e.day or 'today'} {e.time}" if e.time else "any time"
    bits = [e.kind or "any kind", f"{e.text!r}" if e.text else "", f"for {e.for_name}" if e.for_name else "", at]
    return " ".join(b for b in bits if b)


def _clock(t: float) -> str:
    return time.strftime("%a %H:%M", time.localtime(t))
