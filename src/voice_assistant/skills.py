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

import json
import re
import sys
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .events import person_key
from .store import Store

DEVICE = "skills"  # the tool's name
HOUSEHOLD = "household"  # a skill's owner when it's everyone's
ALOUD, CHAT = "aloud", "chat"  # where a skill that runs by itself says what it has to say
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TICK_S = 20.0  # how often a clock looks for a skill that's due
TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
MOST_NOTES = 40  # a person's preferences told to the model, newest first
SAVE, DELETE, PAUSE, RESUME, REMEMBER, FORGET, LIST = "save", "delete", "pause", "resume", "remember", "forget", "list"
ACTIONS = (LIST, SAVE, DELETE, PAUSE, RESUME, REMEMBER, FORGET)

SKILLS_TOOL = {
    "type": "function",
    "name": "skills",
    "description": "Teach TARS: save, change, pause or delete a skill (what to do when someone says its name, or at "
    "set times), and remember or forget what someone likes.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "name", "instructions", "when", "at", "days", "say", "household", "about", "guess"],
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": "list: every skill and preference they have. save: a new skill, or a change to one, "
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
            "say": {
                "type": ["string", "null"],
                "enum": [ALOUD, CHAT, None],
                "description": "save, with at: aloud at home, or in their own chat; null: aloud.",
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
    '- Teaching one ("from now on, when I say bedtime, turn off the TV"; "every day at 7:30, tell me the news"): '
    "first say it back in a sentence (the name, what it does, when it runs by itself and where), and save it only "
    "once they say yes. Changing one is saving it again, in full. A skill about the house or the TV that anyone "
    "might ask for is the household's; one about them is theirs. Don't save what they asked for only this once.\n"
    "- Preferences: when they say what they like or don't (in the news, music, the house, anything), remember it, "
    "and say in a few words that you will. When something is clear from what they keep asking for, remember it as "
    "a guess, without making a fuss. Follow their preferences whenever they apply, and never present a guess as "
    'something they said. "What do you know about me?": tell them, guesses as guesses; they can correct or remove '
    "any of it."
)
# A skill's own turn, at its time: written as the request, so the model knows nobody asked, and that what it says is
# heard by whoever is in the room, or read in the chat.
ON_ITS_OWN = (
    "(Nobody said this: it's {time}, when the skill {name} runs by itself{whose}. Do what its instructions say now. "
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
    if skill.paused:
        parts.append("PAUSED")
    return f"- {skill.name} ({'; '.join(parts)}): {skill.instructions}"


def on_its_own(skill: Skill, when: datetime, skip: str) -> str:
    """The request a skill's own turn starts with."""
    return ON_ITS_OWN.format(
        time=f"{when:%H:%M}",
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
        if not person_key(person):
            return "refused: TARS doesn't know who's asking, and only someone it knows can teach it; say so"
        if action not in ACTIONS:
            return f"error: action is one of {', '.join(ACTIONS)}"
        if request is not None and callable(getattr(request, "go_ahead", None)) and not request.go_ahead():
            return "error: the request was withdrawn before anything was done; nothing changed"
        name = _clean(args.get("name")).lower()
        try:
            if action == SAVE:
                return self._save(person, name, args)
            if action in (DELETE, PAUSE, RESUME):
                return self._change(person, name, action)
            if action == REMEMBER:
                return self._remember(person, args)
            return self._forget(person, _clean(args.get("instructions")))
        except ValueError as e:
            return f"error: {e}"

    def _save(self, person: str, name: str, args: dict) -> str:
        instructions = _clean(args.get("instructions"))
        if not name or not instructions:
            raise ValueError("save needs the skill's name and its instructions, in full")
        at, on = times(args.get("at")), days(args.get("days"))
        if on and not at:
            raise ValueError("days are for a skill that runs by itself: it needs at")
        say = args.get("say") or ALOUD
        owner = HOUSEHOLD if args.get("household") else person
        if owner == HOUSEHOLD and say == CHAT:
            raise ValueError("a household skill is said aloud; one posted in a chat is someone's own")
        now = self._clock()
        values = (instructions, _clean(args.get("when")), json.dumps(at), json.dumps(on), say, now, person)
        with self.store.transaction() as db:
            had = self._find(db, owner, name)
            if had:
                db.execute(
                    "UPDATE skills SET instructions=?, purpose=?, at=?, days=?, say=?, updated=?, updated_by=?, "
                    "paused=0 WHERE id=?",
                    (*values, had["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO skills (instructions, purpose, at, days, say, updated, updated_by, owner, name, "
                    "created, paused) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (*values, owner, name, now),
                )
        whose = "the household's" if owner == HOUSEHOLD else f"{person}'s"
        return f"done: {'changed' if had else 'saved'} {whose} skill {name}; say so in a few words"

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
        )


def _pref_line(p: dict) -> str:
    about = f"({p['about']}) " if p["about"] else ""
    return f"- {about}{p['text']}" + (" [a guess]" if p["guessed"] else "")


def _clean(value) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


class Clock:
    """Which skills are due, for the TARS that says them (`say`: ALOUD for the voice's, CHAT for the Telegram
    service's): a thread reads them every `tick_s` and keeps those whose time has come, each once at each of its
    times, until take() hands them over."""

    def __init__(
        self,
        skills: Callable[[], list[Skill]],
        say: str = ALOUD,
        now: Callable[[], datetime] = datetime.now,
        tick_s: float = TICK_S,
    ):
        self._skills, self.say = skills, say
        self._now, self._tick_s = now, tick_s
        self._ran: set = set()
        self._due: list[tuple[Skill, datetime]] = []
        self._lock = threading.Lock()
        self._closing = threading.Event()

    def start(self) -> Clock:
        threading.Thread(target=self._keep, daemon=True, name=f"skill clock ({self.say})").start()
        return self

    def close(self) -> None:
        self._closing.set()

    def is_due(self) -> bool:
        return bool(self._due)

    def take(self) -> list[tuple[Skill, datetime]]:
        """The skills due, each with when it was due, handed over once."""
        with self._lock:
            due, self._due = self._due, []
        return due

    def look(self, when: datetime | None = None) -> None:
        """Keeps the skills due at `when` (now), to the minute, that it hasn't kept for that minute already."""
        when = when or self._now()
        hhmm = when.strftime("%H:%M")
        try:
            skills = self._skills()
        except Exception as e:  # noqa: BLE001 - the table can't be read now: next tick
            print(f"(skills: couldn't read them: {e!r})")
            return
        for skill in skills:
            if skill.paused or skill.say != self.say or hhmm not in skill.at:
                continue
            key = (skill.id or skill.name, when.date(), hhmm)
            if (not skill.days or DAYS[when.weekday()] in skill.days) and key not in self._ran:
                self._ran.add(key)
                with self._lock:
                    self._due.append((skill, when))

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
