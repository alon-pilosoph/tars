"""Skills: what someone wants done when they say a name, written in plain words in [[home.skills]], for the model to
carry out with the tools it has. "Put on panda" is the skill panda: the model reads its instructions and does them, as
it would anything asked of it, asking whatever it needs to on the way. Nothing here runs a step: the tools keep their
own rules (a skill can't unlock, since unlocking needs the words said and a voice trusted with it).

A skill with `at` also runs by itself at those times of day (`days`, if only some), from the voice's TARS alone: it
starts a turn of its own with the skill's instructions, says what the model replies, and listens for an answer
without the wake word. A chat's TARS runs skills when asked, never on its own."""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TICK_S = 20.0  # how often the clock looks for a skill that's due

RULES = (
    'Skills: when someone asks for one of these by name ("put on cartoons" is the skill cartoons), or what they ask '
    "is what one is for (its when), do what its instructions say, with your tools, as if they'd asked for all of it "
    "themselves. Ask them only what the instructions leave open.\n{skills}"
)
# A skill's own turn, at its time: written as the request, so the model knows nobody asked and that what it says is
# heard by whoever is in the room.
ON_ITS_OWN = (
    "(Nobody said this: it's {time}, when the skill {name} runs by itself. Do what its instructions say now. Whatever "
    "you reply is said aloud, unprompted, to whoever is in the room, and you'll hear their answer. If the "
    "instructions say there's nothing to do now, reply with exactly {skip} and nothing else.)\n"
    "Instructions: {instructions}"
)


@dataclass(frozen=True)
class Skill:
    name: str
    instructions: str
    at: tuple[str, ...] = ()  # "19:55": when it runs by itself, local time
    days: tuple[str, ...] = ()  # on these days ("mon"...); empty: every day
    when: str = ""  # what it's for, so it's found by more than its name: "Ari needs something calm to watch"


def from_config(specs: list[dict]) -> list[Skill]:
    """[[home.skills]], as config.py has checked them."""
    skills = []
    for spec in specs:
        at = spec.get("at", ())
        skills.append(
            Skill(
                spec["name"].strip(),
                " ".join(spec["instructions"].split()),
                (at,) if isinstance(at, str) else tuple(at),
                tuple(d.lower()[:3] for d in spec.get("days", ())),
                " ".join(spec.get("when", "").split()),
            )
        )
    return skills


def rules(skills: list[Skill]) -> str:
    """The skills, for the instructions; empty when there are none."""
    if not skills:
        return ""
    return RULES.format(skills="\n".join(line(s) for s in skills))


def line(skill: Skill) -> str:
    when = f" (when: {skill.when})" if skill.when else ""
    return f"- {skill.name}{when}: {skill.instructions}"


def on_its_own(skill: Skill, when: datetime, skip: str) -> str:
    """The request a skill's own turn starts with."""
    return ON_ITS_OWN.format(time=f"{when:%H:%M}", name=skill.name, instructions=skill.instructions, skip=skip)


class Clock:
    """Which skills are due, for the voice's wake loop: a thread looks every `tick_s` and keeps those whose time has
    come, each once at each of its times, until take() hands them over."""

    def __init__(self, skills: list[Skill], now: Callable[[], datetime] = datetime.now, tick_s: float = TICK_S):
        self.skills = [s for s in skills if s.at]
        self._now, self._tick_s = now, tick_s
        self._ran: set = set()
        self._due: list[tuple[Skill, datetime]] = []
        self._lock = threading.Lock()
        self._closing = threading.Event()

    def start(self) -> Clock:
        threading.Thread(target=self._keep, daemon=True, name="skill clock").start()
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
        for skill in self.skills:
            key = (skill.name, when.date(), hhmm)
            if hhmm in skill.at and (not skill.days or DAYS[when.weekday()] in skill.days) and key not in self._ran:
                self._ran.add(key)
                with self._lock:
                    self._due.append((skill, when))

    def _keep(self) -> None:
        while not self._closing.wait(self._tick_s):
            self.look()
