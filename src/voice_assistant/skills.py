"""Skills: a name for a list of things to do in the house, kept in [[home.skills]] and run with no model working them,
the same way every time. To the model a skill is one more device: it only has to recognize the name. The steps are the
home tool's own calls, checked by it as any call is, from a thread of their own, since a step can take a minute; TARS
says how it went. A failed step ends the skill there, and says which it was.

A short phrase mustn't open the door, so a skill can't unlock: one that tries is left out, and says why."""

import threading
from collections.abc import Callable
from dataclasses import dataclass

from .home import DO, STATUS, UNLOCK, Device, HomeTools, Request

DEVICE = "skills"
NOT_DONE = ("error", "not confirmed", "refused")  # how the home tool's answers say a step didn't work
STOP_WAIT_S = 5.0


@dataclass(frozen=True)
class Skill:
    name: str
    steps: tuple[dict, ...]
    done: str  # what TARS says when it's worked


def problem(spec: dict, devices: dict[str, Device]) -> str | None:
    """What's wrong with a [[home.skills]] entry, given the devices there are; None if it can run."""
    for step in spec["steps"]:
        name, action = str(step["device"]).strip().lower(), step["action"]
        device = devices.get(name)
        if device is None:
            return f"there's no device called {name!r}"
        if action == UNLOCK:
            return "a skill can't unlock"
        if action not in device.actions:
            return f"the {name} can do: {', '.join(device.actions)}"
    return None


class Skills(Device):
    actions = (STATUS, DO)

    def __init__(self, skills: list[Skill], announce: Callable[[str], None]):
        super().__init__(DEVICE)
        self.skills = {s.name.strip().lower(): s for s in skills}
        self._announce = announce
        self.tools: HomeTools | None = None  # set once the tools are made: a skill's steps go through them
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def what(self) -> str:
        return f"named routines, run with do and the goal as the name alone: {', '.join(self.skills)}"

    def rules(self) -> str:
        return (
            'When they ask for a skill by its name ("put on panda" is the skill panda), use skills\' do with its name '
            "as the goal, never its steps yourself. It works in the background and TARS says when it's done: say only "
            "that you're on it, in a few words."
        )

    def status(self) -> str:
        return f"skills: {', '.join(self.skills)}"

    def call(self, action: str, args: dict, request: Request) -> str:
        name = str(args.get("goal") or "").strip().lower()
        skill = self.skills.get(name)
        if skill is None:
            return f"error: there's no skill called {name!r}; there are: {', '.join(self.skills)}"
        self.stop()
        self._stopped = stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(skill, stopped), daemon=True, name="skill")
        self._thread.start()
        return "started: TARS will say when it's done, so say only that you're on it"

    def stop(self) -> None:
        """Before the next step: a step under way finishes, since the device it's on is one call at a time."""
        self._stopped.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_WAIT_S)

    def _run(self, skill: Skill, stopped: threading.Event) -> None:
        try:
            say = self._steps(skill, stopped)
        except Exception as e:  # noqa: BLE001 - said, never raised: nobody is waiting on this thread
            print(f"(skill {skill.name}: {e!r})")
            say = f"I couldn't finish {skill.name}."
        if stopped.is_set():
            print(f"(skill {skill.name} stopped)")
            return
        print(f"(skill {skill.name}: {say})")
        self._announce(say)

    def _steps(self, skill: Skill, stopped: threading.Event) -> str:
        for step in skill.steps:
            if stopped.is_set():
                break
            said = self.tools.call(dict(step), Request())
            if said.startswith(NOT_DONE):
                return f"I couldn't finish {skill.name}: the {step['device']} said {said}."
        return skill.done or f"{skill.name} is on."


def make(specs: list[dict], devices: dict[str, Device], announce, unavailable: dict[str, str]) -> Skills | None:
    """The skills that can run, with why in `unavailable` for those that can't; None if there are none."""
    skills = []
    for spec in specs:
        if why := problem(spec, devices):
            unavailable[spec["name"]] = why
        else:
            steps = tuple(dict(s) for s in spec["steps"])
            skills.append(Skill(spec["name"], steps, spec.get("done", "")))
    return Skills(skills, announce or print) if skills else None
