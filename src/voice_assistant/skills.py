"""Skills and preferences: what TARS is taught by talking to it, kept in the event database, for the model to carry
out with the tools it has.

A skill is a name and what to do, in plain words: "bedtime: turn the TV off and the AC to 24". Asked for by its name,
or for what it's for (its `when`), the model does it as if it had been asked for all of it. It can also run by itself
at set times of day, said aloud at home (the voice's TARS keeps that time) or posted in its owner's chat (the
Telegram service's): "every day at 19:55, ask whether I want the news on". A skill is its owner's, or the
household's, for anyone in it.

A preference is something TARS knows about a person, in plain words, that it keeps in mind whenever they ask
anything: "skip esports in the gaming news", "likes the AC at 23". It's said by them, or noticed by the model and
marked as a guess.

All of it is taught, changed and removed in conversation, through the skills tool, and every request is told the
skills and preferences of whoever is asking (note()). Nothing here does a step: the tools keep their own rules, so a
skill can't unlock (that needs the words said by a voice trusted with it), and an order still waits for its tap."""

import itertools
import json
import re
import sys
import threading
import time
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from .events import person_key
from .store import Store

DEVICE = "skills"  # the tool's name
HOUSEHOLD = "household"  # a skill's owner when it's everyone's
ALOUD, CHAT = "aloud", "chat"  # where a skill that runs by itself says what it has to say
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TICK_S = 10.0  # how often a clock looks for a skill that's due, or something happening one waits for
DRY_RUN_DAYS = 7  # a device trigger is replayed over this much of the device's history before it's kept
COOLDOWN_S = 600.0  # a skill that runs when something happens runs again no sooner: no loops, no flapping
MOST_RUNS = 20  # told when asked what TARS did
TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
MOST_NOTES = 40  # a person's preferences told to the model, newest first
SAVE, DELETE, PAUSE, RESUME, REMEMBER, FORGET = "save", "delete", "pause", "resume", "remember", "forget"
LIST, RUNS = "list", "runs"
ACTIONS = (LIST, RUNS, SAVE, DELETE, PAUSE, RESUME, REMEMBER, FORGET)
ARRIVES, LEAVES, CHANGES = "arrives", "leaves", "device"  # what a skill can run on, besides the time
UPDATED = "updated"  # and TARS itself being updated: someone added or changed what it can do
SEEN_VERSION = "skill_version:{key}"  # in the meta table: the version a skill that runs on UPDATED last saw
ANYONE, EVERYONE = "anyone", "everyone"

SKILLS_TOOL = {
    "type": "function",
    "name": "skills",
    "description": "Teach TARS: save, change, pause or delete a skill (what to do when someone says its name, or at "
    "set times), and remember or forget what someone likes.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "action",
            "name",
            "instructions",
            "when",
            "at",
            "days",
            "trigger",
            "say",
            "household",
            "about",
            "guess",
        ],
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": "list: every skill and preference they have. runs: what skills did by themselves "
                "lately, and why. save: a new skill, or a change to one, "
                "in full, by its name. delete, pause, resume: a skill, by name. remember: a preference, in "
                "instructions. forget: a preference, by its words in instructions.",
            },
            "name": {"type": ["string", "null"], "description": "save, delete, pause, resume: the skill's name."},
            "instructions": {
                "type": ["string", "null"],
                "description": "save: what to do, in plain words, complete. remember: the preference, as a short "
                "note. forget: the preference's words.",
            },
            "when": {
                "type": ["string", "null"],
                "description": "save: what it's for, so it's found by more than its name ('the kids want something "
                "calm to watch'); null: by name only.",
            },
            "at": {
                "type": ["string", "null"],
                "description": "save: times of day it runs by itself, 24-hour, comma-separated ('07:30, 19:55'); "
                "null: only when asked.",
            },
            "days": {
                "type": ["string", "null"],
                "description": "save, with at: the days, comma-separated ('sun, mon'); null: every day.",
            },
            "trigger": {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["event", "who", "device", "says", "above", "below", "reading"],
                        "properties": {
                            "event": {
                                "type": "string",
                                "enum": [ARRIVES, LEAVES, CHANGES, UPDATED],
                                "description": "arrives or leaves: someone comes home or goes out; device: a device "
                                "changes; updated: TARS itself is updated (something added to or changed in what it "
                                "can do).",
                            },
                            "who": {
                                "type": ["string", "null"],
                                "description": "arrives, leaves: a person, as who's home names them; 'everyone' for "
                                "the last one out, or the first one home; null: anyone.",
                            },
                            "device": {"type": ["string", "null"], "description": "device: as the home tool names it."},
                            "says": {
                                "type": ["string", "null"],
                                "description": "device: the word its status says once it's happened: 'unlocked', "
                                "'open', 'off'.",
                            },
                            "above": {"type": ["number", "null"], "description": "device: a reading going over this."},
                            "below": {"type": ["number", "null"], "description": "device: a reading going under this."},
                            "reading": {
                                "type": ["string", "null"],
                                "description": "device, with above or below: which reading, as its status words it "
                                "('co2', 'pm2.5'); null: its first number.",
                            },
                        },
                    },
                    {"type": "null"},
                ],
                "description": "save: something happening that it runs on, instead of times; null: none.",
            },
            "say": {
                "type": ["string", "null"],
                "enum": [ALOUD, CHAT, None],
                "description": "save, with at or a trigger: aloud at home, or in their own chat; null: where "
                "they asked (a chat's in their chat; a household one aloud).",
            },
            "household": {
                "type": ["boolean", "null"],
                "description": "save: true if it's for everyone at home (the TV, the house), false or null if it's "
                "theirs.",
            },
            "about": {
                "type": ["string", "null"],
                "description": "remember: what it's about, in a word or two ('gaming news', 'the AC', 'music').",
            },
            "guess": {
                "type": ["boolean", "null"],
                "description": "remember: true if you noticed it rather than they said it.",
            },
        },
    },
}
CAN_SKILLS = (
    "be taught by them: skills (named things to do, maybe at set times) and what each person likes, with the skills "
    "tool"
)
NEEDS_SKILLS = "teaching TARS a skill, changing or removing one, or telling it what they like or don't"
RULES = (
    "Skills and preferences: you're taught them by talking, with the skills tool, and each request lists those of "
    "whoever is asking.\n"
    '- Using a skill: when they ask for one by name ("put on cartoons" is the skill cartoons), or what they ask is '
    "what one is for, do what its instructions say, with your tools, as if they'd asked for all of it themselves; "
    "ask only what they leave open.\n"
    '- Something to do later, again, or whenever something happens ("every day at 7:30, tell me the news"; "keep '
    'me updated when Alon teaches you new things"; "when Stacey gets home, tell her what\'s waiting"; "if the CO2 '
    'goes over 1000, turn on the purifier"; "from now on, when I say bedtime, turn off the TV"): just do it. '
    "Save it with the skills tool, giving it a short name yourself, and answer the way a person agreeing would, in "
    "plain words: what you'll do and when (\"Will do: I'll message you whenever Alon adds something new.\"). Never "
    "say skill, trigger or saved, and never ask them to confirm what they just asked for; ask only what you can't "
    "sensibly fill in yourself. Asked in a chat, it's said in their chat; asked aloud, aloud. One about the house or "
    "the TV that anyone might ask for is the household's; one about them is theirs. Changing one is saving it "
    "again, in full. Don't save what they asked for only this once.\n"
    "- Preferences: when they say what they like or don't (in the news, music, the house, anything), remember it, "
    "and say in a few words that you will. When something is clear from what they keep asking for, remember it as "
    "a guess, without making a fuss. Follow their preferences whenever they apply, and never present a guess as "
    'something they said. "What do you know about me?": tell them, guesses as guesses; they can correct or remove '
    'any of it. "What did you do today?": the skills\' runs.'
)
# A skill's own turn, at its time: written as the request, so the model knows nobody asked, and that what it says is
# heard by whoever is in the room, or read in the chat.
ON_ITS_OWN = (
    "(Nobody said this: {why}, when the skill {name} runs by itself{whose}. Do what its instructions say now. "
    "{where} If the instructions say there's nothing to do now, reply with exactly {skip} and nothing else.)\n"
    "Instructions: {instructions}"
)
ALOUD_NOTE = "Whatever you reply is said aloud, unprompted, to whoever is in the room, and you'll hear their answer."
CHAT_NOTE = "Whatever you reply is posted in their chat, unprompted, and they may answer there."


@dataclass(frozen=True)
class Skill:
    name: str
    instructions: str
    at: tuple[str, ...] = ()  # "19:55": when it runs by itself, local time
    days: tuple[str, ...] = ()  # on these days ("mon"...); empty: every day
    when: str = ""  # what it's for, so it's found by more than its name
    owner: str = HOUSEHOLD  # a person's name, as TARS knows them, or HOUSEHOLD
    say: str = ALOUD
    paused: bool = False
    id: int | None = None
    trigger: dict | None = None  # something happening that it runs on (trigger())

    @property
    def household(self) -> bool:
        return self.owner == HOUSEHOLD


def times(raw: str | None) -> tuple[str, ...]:
    """'7:30, 19:55' as ("07:30", "19:55"); ValueError for anything that isn't a time of day."""
    found = []
    for part in [p for p in re.split(r"[,\s]+", raw or "") if p]:
        match = TIME.fullmatch(part)
        if not match:
            raise ValueError(f"{part!r} isn't a time of day, like 19:55")
        found.append(f"{int(match[1]):02d}:{match[2]}")
    return tuple(dict.fromkeys(found))


def days(raw: str | None) -> tuple[str, ...]:
    found = []
    for part in [p for p in re.split(r"[,\s]+", (raw or "").lower()) if p]:
        if part[:3] not in DAYS:
            raise ValueError(f"{part!r} isn't a day of the week")
        found.append(part[:3])
    return tuple(d for d in DAYS if d in found)


def line(skill: Skill) -> str:
    """A skill as the model reads it."""
    parts = [f"{skill.owner}'s" if not skill.household else "household"]
    if skill.when:
        parts.append(f"for: {skill.when}")
    if skill.at:
        on = f" on {', '.join(skill.days)}" if skill.days else " every day"
        parts.append(
            f"runs by itself at {', '.join(skill.at)}{on}, {'said aloud' if skill.say == ALOUD else 'in their chat'}"
        )
    if skill.trigger:
        where = "said aloud" if skill.say == ALOUD else "in their chat"
        parts.append(f"runs by itself when {describe(skill.trigger)}, {where}")
    if skill.paused:
        parts.append("PAUSED")
    return f"- {skill.name} ({'; '.join(parts)}): {skill.instructions}"


def runs_by_itself(skill: Skill) -> str:
    """When a skill runs by itself, in words for a person: "at 07:30 on mon, sun", "when stacey comes home"; ""
    for only when asked."""
    if skill.at:
        on = f" on {', '.join(d.title() for d in skill.days)}" if skill.days else " every day"
        return f"at {', '.join(skill.at)}{on}"
    return f"when {describe(skill.trigger)}" if skill.trigger else ""


def describe(trigger: dict) -> str:
    """A trigger, in words: "stacey comes home", "the front door says unlocked"."""
    if trigger["event"] == UPDATED:
        return "TARS is updated"
    who = trigger.get("who") or ANYONE
    who = who if who in (ANYONE, EVERYONE) else who.title()
    if trigger["event"] == ARRIVES:
        return "the first one comes home" if who == EVERYONE else f"{who} comes home"
    if trigger["event"] == LEAVES:
        return "the last one goes out" if who == EVERYONE else f"{who} goes out"
    what = trigger.get("reading") or "reading"
    if trigger.get("above") is not None:
        return f"the {trigger['device']}'s {what} goes over {trigger['above']:g}"
    if trigger.get("below") is not None:
        return f"the {trigger['device']}'s {what} goes under {trigger['below']:g}"
    return f"the {trigger['device']} says {trigger['says']}"


def trigger(raw, devices: set[str] | None = None, people: set[str] | None = None) -> dict | None:
    """A trigger as the tool gives it, checked and tidied; None for none; ValueError saying what's wrong. `devices`
    and `people`: the names there are (lower case), when known."""
    if not isinstance(raw, dict):
        return None
    event = raw.get("event")
    if event == UPDATED:
        return {"event": UPDATED}
    if event in (ARRIVES, LEAVES):
        who = _clean(raw.get("who")).lower() or ANYONE
        if people is not None and who not in (ANYONE, EVERYONE) and who not in people:
            raise ValueError(f"who's home doesn't follow {who!r}; it follows: {', '.join(sorted(people)) or 'nobody'}")
        return {"event": event, "who": who}
    if event != CHANGES:
        raise ValueError("a trigger's event is arrives, leaves, device or updated")
    device = _clean(raw.get("device")).lower()
    if not device:
        raise ValueError("a device trigger needs the device")
    if devices is not None and device not in devices:
        raise ValueError(f"there's no device called {device!r}")
    above, below = raw.get("above"), raw.get("below")
    word = _clean(raw.get("says")).lower()
    if above is None and below is None and not word:
        raise ValueError("a device trigger needs what its status says once it's happened, or a reading's limit")
    if above is not None and below is not None:
        raise ValueError("a device trigger takes above or below, not both: two skills for both")
    return {
        "event": CHANGES,
        "device": device,
        "says": word or None,
        "above": above,
        "below": below,
        "reading": _clean(raw.get("reading")).lower() or None,
    }


def reading(status: str, which: str | None) -> float | None:
    """The number a status gives for a reading ("CO2 850 ppm" for "co2"), or its first; None if there's none."""
    status = status.partition(": ")[2] or status  # not the name, which may have digits of its own
    number = r"(-?\d+(?:\.\d+)?)"
    found = re.search(rf"{re.escape(which)}\D{{0,12}}?{number}" if which else number, status, re.IGNORECASE)
    return float(found[1]) if found else None


def says(status: str, word: str) -> bool:
    """Whether a status says a word, as a word: "unlocked" doesn't say "locked"."""
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", status, re.IGNORECASE) is not None


def fired(trigger: dict, before: str, now: str) -> str:
    """Why a device trigger is set off by its device's status going from `before` to `now`; "" if it isn't."""
    device = trigger["device"]
    if trigger.get("says"):
        if says(now, trigger["says"]) and not says(before, trigger["says"]):
            return f"the {device} now says {trigger['says']} ({now})"
        return ""
    was, is_ = reading(before, trigger.get("reading")), reading(now, trigger.get("reading"))
    if was is None or is_ is None:
        return ""
    what = trigger.get("reading") or "reading"
    if trigger.get("above") is not None and was <= trigger["above"] < is_:
        return f"the {device}'s {what} went over {trigger['above']:g}, to {is_:g}"
    if trigger.get("below") is not None and was >= trigger["below"] > is_:
        return f"the {device}'s {what} went under {trigger['below']:g}, to {is_:g}"
    return ""


def dry_run(trigger: dict, past: list[tuple[float, str]]) -> list[float]:
    """When a device trigger would have run over its device's past statuses ((when, status), in order): each change
    it's set off by, no sooner than COOLDOWN_S after the last, as the clock runs it. The clock looks every TICK_S, so
    a status gone again sooner than that is taken as never seen."""
    seen: list[tuple[float, str]] = []
    for i, (at, status) in enumerate(past):
        if i + 1 < len(past) and past[i + 1][0] - at < TICK_S:
            continue
        if not seen or seen[-1][1] != status:
            seen.append((at, status))
    ran: list[float] = []
    for (_, before), (at, now) in itertools.pairwise(seen):
        if fired(trigger, before, now) and (not ran or at - ran[-1] >= COOLDOWN_S):
            ran.append(at)
    return ran


def dry_run_note(trigger: dict, ran: list[float], days: int = DRY_RUN_DAYS) -> str:
    """What the model is told a dry run found: how often, and when, with when to bring it up."""
    if not ran:
        times = "it never would have"
    else:
        times = f"it would have run {len(ran)} time{'s' * (len(ran) != 1)}"
        if len(ran) >= 3:
            hours = Counter(datetime.fromtimestamp(t).astimezone().hour for t in ran)
            times += f" (most often around {hours.most_common(1)[0][0]:02d}:00)"
    return (
        f"checked against the last {days} days of the {trigger['device']}'s history: {times}. Say so only if that's "
        "clearly more often than they'd want, or never, and offer to change it; otherwise don't mention it"
    )


def on_its_own(skill: Skill, when: datetime, skip: str, why: str = "") -> str:
    """The request a skill's own turn starts with. `why`: what happened, for one that runs on something happening."""
    return ON_ITS_OWN.format(
        why=f"it's {when:%H:%M} and {why}" if why else f"it's {when:%H:%M}",
        name=skill.name,
        whose="" if skill.household else f", for {skill.owner}",
        where=ALOUD_NOTE if skill.say == ALOUD else CHAT_NOTE,
        instructions=skill.instructions,
        skip=skip,
    )


class Skills:
    """The skills and preferences in the event database, and the brain's skills tool over them: tool, rules, can and
    needs, as the browser's errands give them (llm.py), and note() for each request."""

    tool, rules, can, needs = SKILLS_TOOL, RULES, CAN_SKILLS, NEEDS_SKILLS

    def __init__(self, store: Store, clock: Callable[[], float] = time.time):
        self.store, self._clock = store, clock
        # The devices and who's home's people there are, when known, to check a trigger against: set once they're
        # made, as functions so they're read when a skill is saved.
        self.devices: Callable[[], set[str] | None] = lambda: None
        self.people: Callable[[], set[str] | None] = lambda: None
        self.status: Callable[[str], str] | None = None  # a device's status, to check a reading against
        # A device's past statuses, (when, status) in order, from start to end: a device trigger's dry run.
        self.past: Callable[[str, float, float], list[tuple[float, str]]] | None = None

    def seen_version(self, key: str) -> str | None:
        """The version of TARS a skill that runs on its being updated last saw; None before it first looked."""
        row = self.store.row("SELECT value FROM meta WHERE key=?", (SEEN_VERSION.format(key=key),))
        return row["value"] if row else None

    def see_version(self, key: str, version: str) -> None:
        self.store.write(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SEEN_VERSION.format(key=key), version),
        )

    def ran(self, skill: Skill, why: str, said: str) -> None:
        """A skill ran by itself: kept, so "what did you do today?" can be answered."""
        self.store.write(
            "INSERT INTO skill_runs (skill_id, name, owner, at, why, said) VALUES (?, ?, ?, ?, ?, ?)",
            (skill.id, skill.name, skill.owner, self._clock(), why, said),
        )

    def runs(self, person: str | None) -> str:
        mine = person_key(person)
        rows = [
            r
            for r in self.store.rows("SELECT * FROM skill_runs ORDER BY id DESC LIMIT ?", (MOST_RUNS * 3,))
            if r["owner"] == HOUSEHOLD or (mine and person_key(r["owner"]) == mine)
        ][:MOST_RUNS]
        if not rows:
            return "No skill has run by itself lately."
        lines = [
            f"- {time.strftime('%a %H:%M', time.localtime(r['at']))}, {r['name']} ({r['why']}): "
            f"{r['said'] or 'nothing to do'}"
            for r in rows
        ]
        return "What skills did by themselves lately, newest first:\n" + "\n".join(lines)

    # Reading.

    def all(self) -> list[Skill]:
        return [self._skill(r) for r in self.store.rows("SELECT * FROM skills ORDER BY owner != ?, name", (HOUSEHOLD,))]

    def visible(self, person: str | None) -> list[Skill]:
        """The household's skills, and the person's own: theirs first, so their "bedtime" is the one meant."""
        mine = person_key(person)
        own = [s for s in self.all() if mine and person_key(s.owner) == mine]
        names = {s.name for s in own}
        return own + [s for s in self.all() if s.household and s.name not in names]

    def preferences(self, person: str | None) -> list[dict]:
        if not person_key(person):
            return []
        return self.store.rows(
            "SELECT * FROM preferences WHERE person=? ORDER BY id DESC LIMIT ?", (person_key(person), MOST_NOTES)
        )

    def note(self, person: str | None) -> str:
        """For the model, on each request: the skills and preferences of whoever is asking."""
        parts = []
        if skills := self.visible(person):
            whose = f"{person}'s and the household's" if person else "the household's (TARS isn't sure who's asking)"
            parts.append(f"Skills ({whose}):\n" + "\n".join(line(s) for s in skills))
        if prefs := self.preferences(person):
            parts.append(f"What {person} likes and doesn't:\n" + "\n".join(_pref_line(p) for p in prefs))
        return "\n\n".join(parts)

    # The tool.

    def call(self, args: dict, request=None) -> str:
        args = args if isinstance(args, dict) else {}
        action, person = args.get("action"), getattr(request, "speaker", None)
        if action == LIST:
            return self.note(person) or "Nothing taught yet: no skills, and no preferences."
        if action == RUNS:
            return self.runs(person)
        if not person_key(person):
            return "refused: TARS doesn't know who's asking, and only someone it knows can teach it; say so"
        if action not in ACTIONS:
            return f"error: action is one of {', '.join(ACTIONS)}"
        if request is not None and callable(getattr(request, "go_ahead", None)) and not request.go_ahead():
            return "error: the request was withdrawn before anything was done; nothing changed"
        name = _clean(args.get("name")).lower()
        try:
            if action == SAVE:
                return self._save(person, name, args, chat=bool(getattr(request, "by_chat", False)))
            if action in (DELETE, PAUSE, RESUME):
                return self._change(person, name, action)
            if action == REMEMBER:
                return self._remember(person, args)
            return self._forget(person, _clean(args.get("instructions")))
        except ValueError as e:
            return f"error: {e}"

    def _save(self, person: str, name: str, args: dict, chat: bool = False) -> str:
        """`chat`: it was asked for in a chat."""
        instructions = _clean(args.get("instructions"))
        if not name or not instructions:
            raise ValueError("save needs the skill's name and its instructions, in full")
        at, on = times(args.get("at")), days(args.get("days"))
        on_event = trigger(args.get("trigger"), self.devices(), self.people())
        if on and not at:
            raise ValueError("days are for a skill that runs by itself: it needs at")
        if at and on_event:
            raise ValueError("a skill runs at set times or on something happening, not both: two skills for both")
        if on_event and on_event["event"] == CHANGES and self.status:
            self._check_reading(on_event)
        tried = self._dry_run(on_event) if on_event and on_event["event"] == CHANGES else ""
        owner = HOUSEHOLD if args.get("household") else person
        # Where it says what it has to: as asked, or where it was asked for (a chat's, in that chat), but a
        # household's is said aloud.
        say = args.get("say") or (CHAT if chat and owner != HOUSEHOLD else ALOUD)
        if owner == HOUSEHOLD and say == CHAT:
            raise ValueError("a household skill is said aloud; one posted in a chat is someone's own")
        now = self._clock()
        happens = json.dumps(on_event) if on_event else None
        values = (instructions, _clean(args.get("when")), json.dumps(at), json.dumps(on), happens, say, now, person)
        with self.store.transaction() as db:
            had = self._find(db, owner, name)
            if had:
                db.execute(
                    "UPDATE skills SET instructions=?, purpose=?, at=?, days=?, trigger=?, say=?, updated=?, "
                    "updated_by=?, paused=0 WHERE id=?",
                    (*values, had["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO skills (instructions, purpose, at, days, trigger, say, updated, updated_by, owner, "
                    "name, created, paused) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (*values, owner, name, now),
                )
        runs = f", said {'in their chat' if say == CHAT else 'aloud at home'} when it runs" if at or on_event else ""
        return (
            f"done: {'changed' if had else 'kept'}{runs}; tell them in plain words what you'll do and when, as "
            f"agreeing to it, not that you saved anything{f'. Dry run: {tried}' if tried else ''}"
        )

    def _dry_run(self, on_event: dict) -> str:
        """A device trigger replayed over the device's last DRY_RUN_DAYS (self.past), as a note; "" when its history
        can't be read (a device Home Assistant doesn't keep, or one that doesn't answer)."""
        if not self.past:
            return ""
        end = self._clock()
        try:
            past = self.past(on_event["device"], end - DRY_RUN_DAYS * 86400, end)
        except Exception as e:  # noqa: BLE001 - kept without one
            print(f"(skills: no dry run for the {on_event['device']}: {e!r})")
            return ""
        return dry_run_note(on_event, dry_run(on_event, past)) if past else ""

    # The web UI's Skills page: everything, everyone's, and changed by id (it has no one asking).

    def page(self) -> dict:
        """Every skill, everyone's preferences and the latest runs, for the web UI (types.ts SkillsInfo)."""
        skills = [
            {
                "id": s.id,
                "name": s.name,
                "instructions": s.instructions,
                "when": s.when,
                "owner": s.owner,
                "household": s.household,
                "runs_by_itself": runs_by_itself(s),
                "say": s.say,
                "paused": s.paused,
            }
            for s in self.all()
        ]
        prefs = self.store.rows("SELECT id, person, about, text, guessed, at FROM preferences ORDER BY person, id DESC")
        runs = self.store.rows("SELECT id, name, owner, at, why, said FROM skill_runs ORDER BY id DESC LIMIT 50")
        return {
            "skills": skills,
            "preferences": [{**p, "guessed": bool(p["guessed"])} for p in prefs],
            "runs": runs,
        }

    def set_paused(self, skill_id: int, paused: bool) -> bool:
        return self._by_id("UPDATE skills SET paused=?, updated=? WHERE id=?", (paused, self._clock(), skill_id))

    def delete(self, skill_id: int) -> bool:
        return self._by_id("DELETE FROM skills WHERE id=?", (skill_id,))

    def forget(self, preference_id: int) -> bool:
        return self._by_id("DELETE FROM preferences WHERE id=?", (preference_id,))

    def _by_id(self, sql: str, args: tuple) -> bool:
        with self.store.transaction() as db:
            return db.execute(sql, args).rowcount > 0

    def _check_reading(self, on_event: dict) -> None:
        """A reading's limit only works if the device's status has that reading, in its own words ("Carbon
        dioxide", not "CO2"): checked against what it says now, which the model is shown to try again with."""
        if on_event["above"] is None and on_event["below"] is None:
            return
        try:
            now = self.status(on_event["device"])
        except Exception:  # noqa: BLE001 - can't be read now: kept as asked
            return
        if reading(now, on_event["reading"]) is None:
            which = f"{on_event['reading']!r} reading" if on_event["reading"] else "number"
            raise ValueError(f"the {on_event['device']}'s status has no {which}; it says: {now}")

    def _change(self, person: str, name: str, action: str) -> str:
        with self.store.transaction() as db:
            had = self._find(db, person, name) or self._find(db, HOUSEHOLD, name)
            if had is None:
                return f"error: there's no skill called {name!r}; list shows theirs"
            if action == DELETE:
                db.execute("DELETE FROM skills WHERE id=?", (had["id"],))
            else:
                db.execute(
                    "UPDATE skills SET paused=?, updated=? WHERE id=?", (action == PAUSE, self._clock(), had["id"])
                )
        return f"done: {name} {'deleted' if action == DELETE else action + 'd'}"

    def _remember(self, person: str, args: dict) -> str:
        text = _clean(args.get("instructions"))
        if not text:
            raise ValueError("remember needs the preference, in instructions")
        about = _clean(args.get("about")).lower()
        with self.store.transaction() as db:
            same = db.execute(
                "SELECT id FROM preferences WHERE person=? AND lower(text)=lower(?)", (person_key(person), text)
            ).fetchone()
            if same:  # said again: no longer a guess, if it was one
                db.execute(
                    "UPDATE preferences SET guessed=?, at=? WHERE id=?",
                    (bool(args.get("guess")), self._clock(), same["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO preferences (person, about, text, guessed, at) VALUES (?, ?, ?, ?, ?)",
                    (person_key(person), about, text, bool(args.get("guess")), self._clock()),
                )
        return "done: remembered" + (" (as a guess)" if args.get("guess") else "")

    def _forget(self, person: str, words: str) -> str:
        prefs = self.preferences(person)
        gone = [
            p for p in prefs if words and (words.lower() in p["text"].lower() or p["text"].lower() in words.lower())
        ]
        if not gone:
            return "error: nothing they like or don't has those words; list shows what TARS knows"
        with self.store.transaction() as db:
            db.executemany("DELETE FROM preferences WHERE id=?", [(p["id"],) for p in gone])
        return f"done: forgot {'; '.join(p['text'] for p in gone)}"

    def _find(self, db, owner: str, name: str):
        rows = db.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchall()
        return next((r for r in rows if person_key(r["owner"]) == person_key(owner)), None)

    @staticmethod
    def _skill(row: dict) -> Skill:
        return Skill(
            row["name"],
            row["instructions"],
            tuple(json.loads(row["at"] or "[]")),
            tuple(json.loads(row["days"] or "[]")),
            row["purpose"] or "",
            row["owner"],
            row["say"],
            bool(row["paused"]),
            row["id"],
            json.loads(row["trigger"]) if row["trigger"] else None,
        )


def _pref_line(p: dict) -> str:
    about = f"({p['about']}) " if p["about"] else ""
    return f"- {about}{p['text']}" + (" [a guess]" if p["guessed"] else "")


def _clean(value) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


class Due(NamedTuple):
    skill: Skill
    when: datetime
    why: str = ""  # what happened, for one that runs on something happening; "" for its time


class Clock:
    """Which skills are due, for the TARS that says them (`say`: ALOUD for the voice's, CHAT for the Telegram
    service's). A thread looks every `tick_s`: at the time, for the skills whose time it is, each once at each of its
    times; and at who's home (`people()`: each person's name and whether they're home, None not known) and the
    devices triggers name (`status(device)`: its status), for the skills waiting for what changed since. Those are
    kept until take() hands them over. A skill that runs on something happening runs again no sooner than
    COOLDOWN_S, so one can't set itself off, or flap with a reading around its limit. What changed while TARS
    started up is only seen, never acted on. But TARS being updated is: `version()` is the version running, and each
    skill that runs on it keeps the one it last saw (`seen`, `see`), so one deployed while TARS was down, which
    restarts it, is still told, once, with what changed between the two (`between(old, new)`)."""

    def __init__(
        self,
        skills: Callable[[], list[Skill]],
        say: str = ALOUD,
        now: Callable[[], datetime] = datetime.now,
        tick_s: float = TICK_S,
        people: Callable[[], dict[str, bool | None]] | None = None,
        status: Callable[[str], str] | None = None,
        record: Callable[[Skill, str, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        version: Callable[[], str] | None = None,
        between: Callable[[str, str], str] | None = None,
        seen: Callable[[str], str | None] | None = None,
        see: Callable[[str, str], None] | None = None,
    ):
        self._skills, self.say = skills, say
        self._version, self._between, self._seen, self._see = version, between, seen, see
        self._now, self._tick_s = now, tick_s
        self._people, self._status, self._record, self._clock = people, status, record, clock
        self._ran: set = set()
        self._fired: dict = {}  # skill: when it last ran on something happening (self._clock)
        self._home: dict[str, bool | None] | None = None  # who was home last time; None before the first look
        self._statuses: dict[str, str] = {}  # each device's, last time
        self._reading: dict[str, str | None] = {}  # each device's, this time
        self._due: list[Due] = []
        self._lock = threading.Lock()
        self._closing = threading.Event()

    def start(self) -> Clock:
        threading.Thread(target=self._keep, daemon=True, name=f"skill clock ({self.say})").start()
        return self

    def close(self) -> None:
        self._closing.set()

    def is_due(self) -> bool:
        return bool(self._due)

    def take(self) -> list[Due]:
        """The skills due, each with when, and why if something happened, handed over once."""
        with self._lock:
            due, self._due = self._due, []
        return due

    def record(self, due: Due, said: str) -> None:
        """How a skill that ran by itself went: what it said, or "" for nothing to do."""
        if self._record:
            try:
                self._record(due.skill, due.why or f"at {due.when:%H:%M}", said)
            except Exception as e:  # noqa: BLE001 - the log of runs is no reason to stop
                print(f"(skills: couldn't keep the run of {due.skill.name}: {e!r})")

    def look(self, when: datetime | None = None) -> None:
        """Keeps what's due at `when` (now): skills whose time it is, and those waiting for what's changed since."""
        when = when or self._now()
        try:
            skills = [s for s in self._skills() if not s.paused and s.say == self.say]
        except Exception as e:  # noqa: BLE001 - the table can't be read now: next tick
            print(f"(skills: couldn't read them: {e!r})")
            return
        found = self._timed(skills, when) + self._happened([s for s in skills if s.trigger], when)
        if found:
            with self._lock:
                self._due += found

    def _timed(self, skills: list[Skill], when: datetime) -> list[Due]:
        hhmm, found = when.strftime("%H:%M"), []
        for skill in skills:
            key = (skill.id or skill.name, when.date(), hhmm)
            if hhmm in skill.at and (not skill.days or DAYS[when.weekday()] in skill.days) and key not in self._ran:
                self._ran.add(key)
                found.append(Due(skill, when))
        return found

    def _happened(self, skills: list[Skill], when: datetime) -> list[Due]:
        """The skills waiting for something that's happened since the last look."""
        events: list[tuple[dict, str]] = []  # (what happened, as a trigger matches it; in words)
        if self._people and any(s.trigger["event"] in (ARRIVES, LEAVES) for s in skills):
            events += self._comings_and_goings()
        found = []
        for skill in skills:
            event = skill.trigger["event"]
            if event == UPDATED:
                why = self._updated(skill)
            else:
                why = self._changed(skill) if event == CHANGES else self._matches(skill.trigger, events)
            if not why:
                continue
            key = skill.id or skill.name
            if self._clock() - self._fired.get(key, -COOLDOWN_S) < COOLDOWN_S:
                print(f"(skill {skill.name}: {why}, but it ran less than {COOLDOWN_S / 60:.0f} minutes ago)")
                continue
            self._fired[key] = self._clock()
            found.append(Due(skill, when, why))
        self._remember_statuses(skills)
        return found

    def _comings_and_goings(self) -> list[tuple[dict, str]]:
        try:
            now = self._people()
        except Exception as e:  # noqa: BLE001 - who's home can't be read now: next tick
            print(f"(skills: couldn't read who's home: {e!r})")
            return []
        before, self._home = self._home, now
        if before is None:
            return []
        events = []
        for person, home in now.items():
            was = before.get(person)
            if home is None or was is None or home == was:
                continue
            nobody_before = not any(before.get(p) for p in now)
            nobody_now = not any(now.values())
            event = {
                "event": ARRIVES if home else LEAVES,
                "who": person.lower(),
                "all": nobody_before if home else nobody_now,
            }
            events.append((event, f"{person} just {'came home' if home else 'went out'}"))
        return events

    @staticmethod
    def _matches(trigger: dict, events: list[tuple[dict, str]]) -> str:
        """What happened that a who's-home trigger waits for, in words; "" if nothing did."""
        who = trigger.get("who") or ANYONE
        for event, words in events:
            if event["event"] == trigger["event"] and (
                who in (ANYONE, event["who"]) or (who == EVERYONE and event["all"])
            ):
                if who == EVERYONE:
                    return words + (" (nobody was home)" if event["event"] == ARRIVES else " (nobody's home now)")
                return words
        return ""

    def _updated(self, skill: Skill) -> str:
        """Why a skill that runs on TARS being updated is set off now, with what changed; "" if it isn't. A skill
        just saved only notes the version it starts from."""
        if not (self._version and self._seen and self._see):
            return ""
        try:
            now, key = self._version(), str(skill.id or skill.name)
            if not now or (last := self._seen(key)) == now:
                return ""
            self._see(key, now)
            if last is None:
                return ""
            changed = self._between(last, now) if self._between else ""
        except Exception as e:  # noqa: BLE001 - the version or the table can't be read now: next tick
            print(f"(skills: couldn't tell whether TARS was updated: {e!r})")
            return ""
        return f"TARS was just updated ({last} to {now}). What changed, newest first, a title each:\n{changed}"

    def _changed(self, skill: Skill) -> str:
        """Why a device trigger is set off now, from its status before and now; "" if it isn't."""
        device = skill.trigger["device"]
        now, before = self._read(device), self._statuses.get(device)
        return fired(skill.trigger, before, now) if now is not None and before is not None else ""

    def _read(self, device: str) -> str | None:
        if device not in self._reading:
            try:
                self._reading[device] = self._status(device) if self._status else None
            except Exception as e:  # noqa: BLE001 - a device that doesn't answer now: next tick
                print(f"(skills: couldn't read the {device}: {e!r})")
                self._reading[device] = None
        return self._reading[device]

    def _remember_statuses(self, skills: list[Skill]) -> None:
        """The statuses read this look become the ones to compare the next look with; each device is read once a
        look, whatever number of skills wait on it."""
        for skill in skills:
            if skill.trigger["event"] == CHANGES:
                self._read(skill.trigger["device"])
        for device, status in self._reading.items():
            if status is not None:
                self._statuses[device] = status
        self._reading = {}

    def _keep(self) -> None:
        while not self._closing.wait(self._tick_s):
            self.look()


def make(events) -> Skills | None:
    """The skills, in the event database; None without one ([learning] log_events off)."""
    if events is None:
        print("(skills: off, since they're kept in the event database and [learning] log_events is off)")
        return None
    return Skills(events.store)


def import_config(config: Path, skills: Skills) -> list[str]:
    """[[home.skills]] from a config file, as the household's skills: what skills were in config.toml before they
    were taught by talking. The names imported; one already taught is left as it is."""
    raw = tomllib.loads(config.read_text()) if config.exists() else {}
    done = []
    for spec in raw.get("home", {}).get("skills", []):
        name = str(spec.get("name", "")).strip().lower()
        if not name or not spec.get("instructions") or name in {s.name for s in skills.all() if s.household}:
            continue
        args = {
            "action": SAVE,
            "name": name,
            "instructions": spec["instructions"],
            "when": spec.get("when"),
            "at": ", ".join([spec["at"]] if isinstance(spec.get("at"), str) else spec.get("at", [])) or None,
            "days": ", ".join(spec.get("days", [])) or None,
            "household": True,
        }
        said = skills._save("config", name, args)
        if said.startswith("done"):
            done.append(name)
    return done


def main() -> None:
    """`python -m voice_assistant.skills import`: config.local.toml's [[home.skills]] into the event database, once,
    after which they're taught, changed and removed by talking to TARS (and are deleted from the file)."""
    from .config import load_config
    from .events import EventLog

    if sys.argv[1:] != ["import"]:
        raise SystemExit("usage: python -m voice_assistant.skills import")
    root = Path.cwd()
    cfg = load_config(root / "config.toml", local=False)
    skills = Skills(EventLog(root / cfg.learning.folder).store)
    done = import_config(root / "config.local.toml", skills)
    print(f"Imported: {', '.join(done) or 'nothing'}. Now delete [[home.skills]] from config.local.toml.")


if __name__ == "__main__":
    main()
