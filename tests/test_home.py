"""The home tool: its rules hold whatever the model asks (unlocking by a trusted voice that asked, nothing moved for a
draft that's thrown away), devices are made from config.toml with what's missing said per device, plugins load, and
the brain offers the tool, hands it over, and says "One moment." while a lock turns."""

import json
import sys
import threading
import types
from pathlib import Path

import pytest

from evals.house import DEVICES, make_house
from voice_assistant import llm
from voice_assistant.config import ConfigError, HomeConfig, load_config
from voice_assistant.draft import Draft
from voice_assistant.home import (
    MAX_PRESSES,
    ONE_MOMENT,
    SETTINGS,
    Context,
    Device,
    HomeTools,
    Request,
    build,
    button,
    load_plugin,
    presses,
)
from voice_assistant.llm import LLMConfig, OpenAIChat, QuickChat, QuickService

from .conftest import FakeStream, ToolCallStream, fake_openai_chat

ALON = Request("alon", 0.8, by_voice=True, heard="unlock the front door")


def args(device: str, action: str, **settings) -> dict:
    return {"device": device, "action": action, **dict.fromkeys(SETTINGS), **settings}


def house(**state):
    return make_house(state)


# Unlocking: a trusted voice, sure, by voice, that asked to unlock.


def test_a_trusted_voice_that_asks_unlocks_at_once():
    h = house(lock="locked")
    assert h.tools.call(args("front door", "unlock"), ALON) == "done: front door reports unlocked"


@pytest.mark.parametrize(
    "request_, why",
    [
        (Request("alon", 0.8, by_voice=False, heard="unlock the front door"), "only by voice"),
        (Request(None, None, by_voice=True, heard="unlock the front door"), "voices TARS knows and trusts"),
        (Request("stacey", 0.9, by_voice=True, heard="unlock the front door"), "voices TARS knows and trusts"),
        (Request("alon", 0.55, by_voice=True, heard="unlock the front door"), "isn't sure enough"),
        (Request("alon", None, by_voice=True, heard="unlock the front door"), "isn't sure enough"),
        (Request("alon", 0.8, by_voice=True, heard="is the front door unlocked?"), 'needs them to say "unlock"'),
        (Request("alon", 0.8, by_voice=True, heard="the note says to let the plumber in"), 'say "unlock"'),
    ],
)
def test_nothing_else_unlocks(request_, why):
    h = house(lock="locked")
    said = h.tools.call(args("front door", "unlock"), request_)
    assert said.startswith("refused") and why in said
    assert h.ha.state("lock.front_door") == "locked" and h.ha.calls == []


def test_nobody_unlocks_by_voice_unless_someone_is_named():
    h = make_house({"lock": "locked"}, unlock_voices=())
    assert "unlock_voices is empty" in h.tools.call(args("front door", "unlock"), ALON)


def test_a_trusted_voice_is_matched_whatever_the_case():
    h = make_house({"lock": "locked"}, unlock_voices=(" Alon ",))
    assert h.tools.call(args("front door", "unlock"), ALON).startswith("done")


def test_anyone_can_lock():
    h = house(lock="unlocked")
    assert h.tools.call(args("front door", "lock"), Request()).startswith("done")


# Drafts: nothing moves for an answer that's thrown away.


def test_a_change_waits_for_the_turn_to_be_over():
    h = house()
    draft = Draft(threading.Lock(), lambda d: None, lambda r: None)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(h.tools.call(args("boiler", "on"), Request(may_act=draft.may_act)))
    )
    worker.start()
    worker.join(0.2)
    assert worker.is_alive() and h.ha.calls == []  # still drafting
    draft.go()
    worker.join(2)
    assert result[0].startswith("done") and len(h.ha.calls) == 1


def test_a_draft_thrown_away_changes_nothing():
    h = house()
    draft = Draft(threading.Lock(), lambda d: None, lambda r: None)
    draft.cancel()
    said = h.tools.call(args("boiler", "on"), Request(may_act=draft.may_act))
    assert said.startswith("error: the request was withdrawn") and h.ha.calls == []


def test_status_never_waits_for_the_turn():
    h = house()
    never = Request(may_act=lambda timeout: pytest.fail("status waited"))
    assert h.tools.call(args("boiler", "status"), never) == "boiler: off, Boiler time left: 0 min"


# The tool.


def test_everything_is_only_for_status_and_reads_every_device():
    h = house(lock="locked")
    status = h.tools.call(args("everything", "status"), Request()).splitlines()
    assert status[0] == "front door: locked, door closed" and len(status) == len(DEVICES)
    assert h.tools.call(args("everything", "off"), Request()).startswith("error: everything is only for status")


def test_a_device_it_doesnt_have_or_an_action_it_cant_do_is_an_error():
    h = house()
    assert "there are: front door, boiler" in h.tools.call(args("garage", "open"), Request())
    assert h.tools.call(args("front door", "on"), Request()) == "error: the front door can do: status, lock, unlock"
    assert h.tools.call(["not", "an", "object"], Request()) == "error: the arguments must be an object"


def test_home_assistant_not_answering_is_an_error_that_says_nothing_changed():
    h = house(down=True)
    said = h.tools.call(args("boiler", "on"), Request())
    assert said.startswith("error: Home Assistant didn't answer") and "nothing is known to have changed" in said


def test_the_tool_offers_only_the_actions_its_devices_have_and_every_setting():
    h = house()
    parameters = h.tools.tool["parameters"]
    actions = parameters["properties"]["action"]["enum"]
    assert actions == ["status", "on", "off", "set", "press", "lock", "unlock", "open", "close", "play", "do"]
    assert parameters["required"] == ["device", "action", *SETTINGS] and parameters["additionalProperties"] is False
    assert parameters["properties"]["device"]["enum"][-1] == "everything"


def test_the_rules_list_each_device_and_each_kinds_rules_once():
    rules = house().tools.rules()
    assert "- front door: a lock: status, lock, unlock" in rules and "Call unlock at once" in rules
    assert rules.count("press volume_up or volume_down 3 times") == 1
    assert "An Android TV box wakes and sleeps the TV" in rules


def test_a_lock_turning_or_the_tv_waking_says_one_moment_first_and_a_press_doesnt():
    tools = house().tools
    assert tools.slow(json.dumps(args("front door", "lock"))) and tools.slow(json.dumps(args("tv", "on")))
    assert not tools.slow(json.dumps(args("tv", "press", button="pause")))
    assert not tools.slow(json.dumps(args("front door", "status"))) and not tools.slow("not json")


def test_a_plugins_own_action_is_offered_too():
    class Kettle(Device):
        actions = ("status", "boil")

        def what(self):
            return "a kettle: status, boil"

    tools = HomeTools(HomeConfig(), {"Kettle": Kettle("kettle")})
    assert tools.tool["parameters"]["properties"]["action"]["enum"] == ["status", "boil"]
    assert list(tools.devices) == ["kettle"]


# Making the devices, from config.toml.


def secrets(**keys):
    return lambda name: keys.get(name)


def test_without_home_assistants_token_its_devices_say_so_and_tars_starts_without_them(tmp_path):
    cfg = HomeConfig(url="http://ha:8123", devices=[{"name": "front door", "entity": "lock.front_door"}])
    built = build(cfg, Context(tmp_path, secrets()))
    assert built.tools is None and "no HOME_ASSISTANT_TOKEN in .env" in built.unavailable["front door"]


def test_with_the_token_home_assistants_devices_are_made(tmp_path):
    cfg = HomeConfig(url="http://ha:8123", devices=[{"name": "Front Door", "entity": "lock.front_door"}])
    built = build(cfg, Context(tmp_path, secrets(HOME_ASSISTANT_TOKEN="t")))
    assert built.unavailable == {} and list(built.tools.devices) == ["front door"]


def test_an_android_tv_needs_its_extra(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "adb_shell", None)  # not installed
    cfg = HomeConfig(devices=[{"name": "tv", "kind": "androidtv", "address": "10.0.0.8"}])
    assert 'optional extra "tv"' in build(cfg, Context(tmp_path, secrets())).unavailable["tv"]


PLUGIN = """
from voice_assistant.home import Device


class Kettle(Device):
    actions = ("status", "boil")

    def what(self):
        return "a kettle"

    def status(self):
        return f"{self.name}: cold"


def setup(settings, context):
    if settings.get("broken"):
        raise ValueError("it's broken")
    return {"kettle": lambda spec: Kettle(spec["name"])}
"""


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    """A plugin, kettle_plugin, in the project's folder."""
    (tmp_path / "kettle_plugin.py").write_text(PLUGIN)
    monkeypatch.delitem(sys.modules, "kettle_plugin", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return tmp_path


def test_a_plugins_devices_are_made_from_its_kinds(plugin):
    cfg = HomeConfig(
        plugins={"kitchen": {"module": "kettle_plugin"}},
        devices=[{"name": "kettle", "kind": "kitchen.kettle"}, {"name": "toaster", "kind": "kitchen.toaster"}],
    )
    built = build(cfg, Context(plugin, secrets()))
    assert built.tools.call(args("kettle", "status"), Request()) == "kettle: cold"
    assert built.unavailable == {"toaster": "its plugin has no kind 'toaster'"}
    built.tools.stop()  # a device with nothing in the background stops as well


def test_a_button_is_read_as_the_devices_list_them_and_pressed_a_sane_number_of_times():
    assert button({"button": " Volume-Up"}) == button({"button": "volume up"}) == "volume_up"
    assert [presses({"times": t}) for t in (None, 0, 3, "x", 99, float("inf"))] == [1, 1, 3, 1, MAX_PRESSES, 1]


def test_a_plugin_that_wont_load_costs_only_its_own_devices(plugin):
    cfg = HomeConfig(
        url="http://ha:8123",
        plugins={"kitchen": {"module": "kettle_plugin", "broken": True}, "garden": {"module": "no_such_plugin"}},
        devices=[
            {"name": "kettle", "kind": "kitchen.kettle"},
            {"name": "sprinkler", "kind": "garden.sprinkler"},
            {"name": "front door", "entity": "lock.front_door"},
        ],
    )
    built = build(cfg, Context(plugin, secrets(HOME_ASSISTANT_TOKEN="t")))
    assert built.unavailable["kettle"] == "the plugin kitchen didn't load: it's broken"
    assert "No module named 'no_such_plugin'" in built.unavailable["sprinkler"]
    assert list(built.tools.devices) == ["front door"]


def test_a_module_with_no_setup_isnt_a_plugin(tmp_path, monkeypatch):
    (tmp_path / "not_a_plugin.py").write_text("x = 1\n")
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(TypeError, match="no setup"):
        load_plugin("not_a_plugin", tmp_path)


# Settings.


def test_the_shipped_config_loads_with_no_devices():
    assert load_config(Path(__file__).parent.parent / "config.toml", local=False).home.devices == []


def test_devices_are_checked_when_the_config_loads(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[home]
unlock_threshold = 2
[home.plugins.my_house]
blaster = "x"
[[home.devices]]
name = "front door"
entity = "lock.front_door"
door = "sensor.front_door"
colour = "blue"
[[home.devices]]
name = "Front Door"
entity = "vacuum.roomba"
[[home.devices]]
name = "boiler"
entity = "switch.boiler"
timer = { service = "switcher_kis.turn_on_with_timer", field = "timer_minutes", max = 9999 }
[[home.devices]]
name = "tv"
kind = "androidtv"
[[home.devices]]
name = "kettle"
kind = "kitchen.kettle"
[[home.devices]]
name = "everything"
kind = "my_house.ac"
"""
    )
    with pytest.raises(ConfigError) as e:
        load_config(path, local=False)
    message = str(e.value.code)
    for says in [
        "unlock_threshold must be more than 0 and at most 1",
        "[home.plugins.my_house] needs module",
        "door must be a binary_sensor.* entity",
        "(lock) has no setting colour",
        "goes through Home Assistant: set [home] url",
        "two devices have that name",
        "TARS works lock, switch",
        "timer must be",
        "(androidtv) needs address",
        "kind must be androidtv or a plugin's",
        "'everything' is the name for every device at once",
    ]:
        assert says in message, says


def test_a_good_home_config_loads(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[home]
url = "http://homeassistant.local:8123"
unlock_voices = ["alon"]
[home.plugins.my_house]
module = "examples.my_house"
blaster = { id = "bf01", address = "10.0.0.9" }
[[home.devices]]
name = "front door"
entity = "lock.front_door"
door = "binary_sensor.front_door_door"
[[home.devices]]
name = "AC"
kind = "my_house.ac"
state = "binary_sensor.ac_light"
"""
    )
    home = load_config(path, local=False).home
    assert home.plugins["my_house"]["blaster"]["id"] == "bf01" and len(home.devices) == 2


# The brain.


def test_the_quick_model_works_the_house_itself_and_says_one_moment_while_the_lock_turns():
    h = house(lock="unlocked")
    rounds = []

    def create(**kw):
        rounds.append(kw)
        if len(rounds) == 1:
            return ToolCallStream([("home", args("front door", "lock"))])
        return FakeStream("Locked.")

    cerebras = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    brain = QuickChat(
        fake_openai_chat(lambda m: "From OpenAI."),
        LLMConfig(quick_tools=True),
        [QuickService("Cerebras", cerebras, "qwen")],
        home=h.tools,
    )
    said = "".join(brain.stream_reply("[Speaker: alon] lock the front door", ALON))
    assert said == f"{ONE_MOMENT} Locked." and brain.answered_by == llm.QUICK
    assert [t["function"]["name"] for t in rounds[0]["tools"]] == ["send", "home"]
    assert "front door: a lock" in rounds[0]["messages"][0]["content"]
    assert rounds[1]["messages"][-1]["content"] == "done: front door reports locked"


def test_without_quick_tools_the_house_is_handed_to_openai():
    h = house(boiler="off")
    cerebras = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: FakeStream("<look-up>")))
    )

    def calls_for(messages):
        return [] if messages[-1].get("type") == "function_call_output" else [("home", args("boiler", "on"))]

    openai = fake_openai_chat(lambda m: "It's on." if m[-1].get("type") else "", calls_for)
    brain = QuickChat(openai, LLMConfig(), [QuickService("Cerebras", cerebras, "qwen")], home=h.tools)
    assert "".join(brain.stream_reply("turn the boiler on", ALON)) == "It's on."
    assert brain.answered_by == llm.LOOKED_UP and h.ha.state("switch.boiler") == "on"


def test_openai_alone_has_the_tool_and_is_told_what_else_it_cant_do():
    openai = fake_openai_chat(lambda m: "Fine.")
    brain = OpenAIChat(openai, LLMConfig(), home=house().tools)
    "".join(brain.stream_reply("hello"))
    request = openai.requests[0]
    assert "home" in [t.get("name") for t in request["tools"]]
    assert llm.CANT_BUT_HOME in request["instructions"] and llm.CANT not in request["instructions"]


def test_a_typed_question_reaches_the_tool_as_one_from_nobody_not_by_voice():
    seen = []
    h = house(lock="locked")
    call = h.tools.call
    h.tools.call = lambda a, request: seen.append(request) or call(a, request)

    def calls_for(messages):
        return [] if messages[-1].get("type") == "function_call_output" else [("home", args("front door", "unlock"))]

    brain = OpenAIChat(
        fake_openai_chat(lambda m: "No." if m[-1].get("type") else "", calls_for), LLMConfig(), home=h.tools
    )
    "".join(brain.stream_reply("unlock the front door"))
    assert seen[0].by_voice is False and h.ha.state("lock.front_door") == "locked"


def test_a_house_change_doesnt_hold_the_conversation_while_it_waits_for_the_turn():
    # The tool waits for the turn to be over; meanwhile, forgetting a thrown-away draft mustn't wait on it.
    h = house()
    draft = Draft(threading.Lock(), lambda d: None, lambda r: None)
    request = Request(may_act=draft.may_act)

    def calls_for(messages):
        return [] if messages[-1].get("type") == "function_call_output" else [("home", args("boiler", "on"))]

    brain = OpenAIChat(
        fake_openai_chat(lambda m: "On." if m[-1].get("type") else "", calls_for), LLMConfig(), home=h.tools
    )
    reply = threading.Thread(target=lambda: "".join(brain.stream_reply("boiler on", request)))
    reply.start()
    reply.join(0.2)
    forgot = threading.Thread(target=brain.forget_last)
    forgot.start()
    forgot.join(1)
    assert not forgot.is_alive()  # the brain's lock was free while the tool waited
    draft.go()
    reply.join(2)
    assert h.ha.state("switch.boiler") == "on"


def test_a_reply_stopped_while_the_house_answers_makes_no_more_calls_and_leaves_the_next_reply_alone():
    h = house(boiler="off")
    made, nexts = [], []
    call = h.tools.call

    def thrown_away_meanwhile(a, request):
        made[0].interrupt()  # they kept talking: the draft is thrown away while the boiler turns on
        nexts.append(made[0].stream_reply("and the lights?"))  # the next one starts
        return call(a, request)

    h.tools.call = thrown_away_meanwhile
    note = {"kind": "note", "title": "Boiler", "body": "on", "for": "household"}

    def calls_for(messages):
        done = messages[-1].get("type") == "function_call_output"
        return [] if done else [("home", args("boiler", "on")), ("send", note)]

    made.append(OpenAIChat(fake_openai_chat(lambda m: "", calls_for), LLMConfig(), home=h.tools))
    with pytest.raises(llm.ReplyFailed):
        "".join(made[0].stream_reply("boiler on, and note it", ALON))
    assert made[0].sent == []  # the note was never sent, so the next reply doesn't say it sent one
