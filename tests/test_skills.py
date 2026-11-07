"""Skills: a name for a list of the home tool's calls, run in order with no model, stopping at the first that fails, and
never able to unlock."""

import threading

import pytest

from voice_assistant import skills
from voice_assistant.config import HomeConfig
from voice_assistant.home import DO, LOCK, ON, PLAY, STATUS, UNLOCK, Context, Device, HomeTools, Request, build
from voice_assistant.skills import Skill, Skills, problem


class Thing(Device):
    actions = (STATUS, ON, PLAY)

    def __init__(self, name, answers=()):
        super().__init__(name)
        self.calls, self.answers = [], list(answers)

    def what(self):
        return "a thing"

    def status(self):
        return f"{self.name}: fine"

    def call(self, action, args, request):
        self.calls.append((action, args.get("title")))
        return self.answers.pop(0) if self.answers else "done: it did"


class Door(Thing):
    actions = (STATUS, LOCK, UNLOCK)


def house(*steps, done="", answers=()):
    tv, said = Thing("tv", answers), []
    device = Skills([Skill("panda", steps, done)], said.append)
    device.tools = HomeTools(HomeConfig(), {"tv": tv, "skills": device})
    return device, tv, said


def run(device, goal="panda"):
    answer = device.call(DO, {"goal": goal}, Request())
    if device._thread:
        device._thread.join(5)
    return answer


TV_ON = {"device": "tv", "action": ON}
PLAY_IT = {"device": "tv", "action": PLAY, "title": "https://youtu.be/x"}


def test_a_skill_runs_its_steps_in_order_and_says_it_worked():
    device, tv, said = house(TV_ON, PLAY_IT, done="Panda is on.")
    assert run(device).startswith("started")
    assert tv.calls == [(ON, None), (PLAY, "https://youtu.be/x")]
    assert said == ["Panda is on."]


def test_with_nothing_to_say_it_uses_its_name():
    device, _, said = house(TV_ON)
    run(device)
    assert said == ["panda is on."]


@pytest.mark.parametrize("failed", ["error: it's broken", "not confirmed: nothing plays", "refused: no"])
def test_a_failed_step_ends_it_there_and_says_which(failed):
    device, tv, said = house(TV_ON, PLAY_IT, answers=[failed])
    run(device)
    assert tv.calls == [(ON, None)]
    assert said == [f"I couldn't finish panda: the tv said {failed}."]


def test_a_name_it_doesnt_know_lists_the_ones_it_does():
    device, tv, said = house(TV_ON)
    assert run(device, "movie night") == "error: there's no skill called 'movie night'; there are: panda"
    assert tv.calls == [] and said == []


def test_its_name_is_matched_whatever_the_case():
    device, tv, _ = house(TV_ON)
    run(device, " Panda ")
    assert len(tv.calls) == 1


def test_stopped_before_a_step_it_goes_no_further_and_says_nothing():
    device, tv, said = house(TV_ON, PLAY_IT)
    release, reached = threading.Event(), threading.Event()
    original = tv.call

    def held(action, args, request):
        reached.set()
        release.wait(5)
        return original(action, args, request)

    tv.call = held
    device.call(DO, {"goal": "panda"}, Request())
    assert reached.wait(5)
    threading.Timer(0.05, release.set).start()
    device.stop()
    assert len(tv.calls) == 1 and said == []


def test_a_skill_cant_unlock_even_on_a_device_that_can():
    devices = {"front door": Door("front door")}
    spec = {"name": "open up", "steps": [{"device": "front door", "action": UNLOCK}]}
    assert problem(spec, devices) == "a skill can't unlock"


def test_a_skill_that_cant_run_is_left_out_saying_why_and_the_others_run():
    devices, unavailable = {"tv": Thing("tv")}, {}
    specs = [
        {"name": "bad", "steps": [{"device": "nothing", "action": ON}]},
        {"name": "odd", "steps": [{"device": "tv", "action": LOCK}]},
        {"name": "panda", "steps": [TV_ON]},
    ]
    made = skills.make(specs, devices, None, unavailable)
    assert list(made.skills) == ["panda"]
    assert unavailable == {"bad": "there's no device called 'nothing'", "odd": "the tv can do: status, on, play"}


def test_build_makes_them_a_device_the_model_can_call(tmp_path):
    cfg = HomeConfig(skills=[{"name": "panda", "steps": [TV_ON]}])
    context = Context(tmp_path, lambda name: None, announce=lambda line: None)
    built = build(cfg, context, extra={"tv": Thing("tv")})
    assert built.unavailable == {}
    assert "skills" in built.tools.devices and built.tools.devices["skills"].tools is built.tools
    assert "panda" in built.tools.rules() and "use skills' do" in built.tools.rules()


def test_no_skills_no_device(tmp_path):
    built = build(HomeConfig(), Context(tmp_path, lambda name: None), extra={"tv": Thing("tv")})
    assert "skills" not in built.tools.devices
