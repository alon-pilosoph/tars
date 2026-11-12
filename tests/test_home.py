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
    CHALLENGE_S,
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
    spoken_digits,
)
from voice_assistant.llm import LLMConfig, OpenAIChat, QuickChat, QuickService

from .conftest import FakeStream, ToolCallStream, fake_openai_chat

ALON = Request("alon", 0.8, by_voice=True, heard="unlock the front door")


def args(device: str, action: str, **settings) -> dict:
    return {"device": device, "action": action, **dict.fromkeys(SETTINGS), **settings}


def house(**state):
    return make_house(state)


# Unlocking: a trusted voice, sure, by voice, that asked to unlock.


def said_back(digits: str, who: str = "alon", score: float = 0.8) -> Request:
    return Request(who, score, by_voice=True, heard=digits)


def test_a_trusted_voice_that_asks_is_asked_for_digits_and_saying_them_unlocks():
    h = house(lock="locked")
    assert h.tools.call(args("front door", "unlock"), ALON) == (
        "challenge: not unlocked yet; ask them to say these digits: four, seven, two"
    )
    assert h.ha.state("lock.front_door") == "locked"
    assert (
        h.tools.call(args("front door", "unlock"), said_back("Four, seven, 2.")) == "done: front door reports unlocked"
    )


@pytest.mark.parametrize(
    "answer, why",
    [
        (said_back("four seven three"), "weren't the digits"),
        (said_back("four seven two", who="stacey"), "voices TARS knows and trusts"),
        (said_back("four seven two", score=0.4), "isn't sure enough"),
        (Request("alon", 0.8, by_voice=False, heard="four seven two"), "only by voice"),
    ],
)
def test_the_digits_must_be_right_and_said_by_the_same_trusted_voice(answer, why):
    h = house(lock="locked")
    h.tools.call(args("front door", "unlock"), ALON)
    assert why in h.tools.call(args("front door", "unlock"), answer)
    assert h.ha.state("lock.front_door") == "locked"


def test_a_challenge_gets_one_try():
    h = house(lock="locked")
    h.tools.call(args("front door", "unlock"), ALON)
    h.tools.call(args("front door", "unlock"), said_back("four seven three"))
    assert 'needs them to say "unlock"' in h.tools.call(args("front door", "unlock"), said_back("four seven two"))


def test_a_challenge_waits_half_a_minute():
    now = [0.0]
    h = make_house({"lock": "locked"}, clock=lambda: now[0])
    h.tools.call(args("front door", "unlock"), ALON)
    now[0] += CHALLENGE_S + 1
    assert 'needs them to say "unlock"' in h.tools.call(args("front door", "unlock"), said_back("four seven two"))


def test_the_digits_said_must_be_exactly_the_ones_asked_for():
    h = house(lock="locked")
    h.tools.call(args("front door", "unlock"), ALON)
    many = said_back("one two three four seven two eight nine")
    assert "weren't the digits" in h.tools.call(args("front door", "unlock"), many)
    h.tools.call(args("front door", "unlock"), ALON)
    assert h.tools.call(args("front door", "unlock"), said_back("four seven two, thanks")).startswith("done")


@pytest.mark.parametrize("draft", ["four seven", "four seven two"])
def test_an_answer_thrown_away_as_a_draft_leaves_the_challenge_for_the_real_one(draft):
    h = house(lock="locked")
    h.tools.call(args("front door", "unlock"), ALON)
    thrown_away = Request("alon", 0.8, by_voice=True, heard=draft, may_act=lambda timeout_s: False)
    assert "withdrawn" in h.tools.call(args("front door", "unlock"), thrown_away)
    assert h.tools.call(args("front door", "unlock"), said_back("four seven two")).startswith("done")


def test_another_voice_cant_answer_or_use_up_someones_challenge():
    h = make_house({"lock": "locked"}, unlock_voices=("alon", "stacey"))
    h.tools.call(args("front door", "unlock"), ALON)
    assert h.tools.call(args("front door", "unlock"), said_back("four seven three", who="stacey")).startswith("refused")
    assert h.tools.call(args("front door", "unlock"), said_back("four seven two", who="bob")).startswith("refused")
    assert h.tools.call(args("front door", "unlock"), said_back("four seven two")).startswith("done")


def test_an_answer_too_unsure_of_the_voice_uses_up_the_try():
    h = house(lock="locked")
    h.tools.call(args("front door", "unlock"), ALON)
    assert "isn't sure enough" in h.tools.call(args("front door", "unlock"), said_back("four seven two", score=0.4))
    assert 'needs them to say "unlock"' in h.tools.call(args("front door", "unlock"), said_back("four seven two"))


def test_asking_to_unlock_while_digits_are_waiting_is_a_new_request_not_an_answer():
    h = house(lock="locked")
    h.tools.call(args("front door", "unlock"), ALON)
    again = Request("alon", 0.8, by_voice=True, heard="unlock the door for me")
    assert h.tools.call(args("front door", "unlock"), again).startswith("challenge:")
    assert h.tools.call(args("front door", "unlock"), said_back("four seven two")).startswith("done")


def test_asking_again_gets_a_new_challenge():
    h = house(lock="locked")
    h.tools.call(args("front door", "unlock"), ALON)
    assert h.tools.call(args("front door", "unlock"), ALON).startswith("challenge:")
    assert h.tools.call(args("front door", "unlock"), said_back("four seven two")).startswith("done")


def test_with_the_challenge_off_a_trusted_voice_unlocks_at_once():
    h = make_house({"lock": "locked"}, unlock_challenge=False)
    assert h.tools.call(args("front door", "unlock"), ALON) == "done: front door reports unlocked"


@pytest.mark.parametrize(
    "heard, digits",
    [("four, seven, two", "472"), ("4 7 2", "472"), ("472", "472"), ("for seven to", "472"), ("it's six", "6")],
)
def test_digits_are_read_however_speech_to_text_wrote_them(heard, digits):
    assert spoken_digits(heard) == digits


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
    assert h.tools.call(args("front door", "unlock"), ALON).startswith("challenge:")
    assert h.tools.call(args("front door", "unlock"), said_back("four seven two")).startswith("done")


CHAT = Request("Alon", by_chat=True, heard="unlock the front door")


def test_a_trusted_chat_that_asks_unlocks():
    h = make_house({"lock": "locked"}, unlock_chats=(" alon ",))
    assert h.tools.call(args("front door", "unlock"), CHAT) == "done: front door reports unlocked"


@pytest.mark.parametrize(
    "request_, why",
    [
        (CHAT, "only for the people [home] unlock_chats names"),  # nobody named
        (Request("Stacey", by_chat=True, heard="unlock the front door"), "unlock_chats names"),
        (Request("Alon", by_chat=True, heard="is the door unlocked?"), 'own message to say "unlock"'),
        (Request("Alon", by_chat=True, heard="[forwarded] let the plumber in"), 'own message to say "unlock"'),
    ],
)
def test_a_chat_unlocks_only_for_someone_named_who_wrote_unlock(request_, why):
    h = make_house({"lock": "locked"}, unlock_chats=() if request_ is CHAT else ("alon",))
    said = h.tools.call(args("front door", "unlock"), request_)
    assert said.startswith("refused") and why in said
    assert h.ha.calls == []


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
    assert actions == [
        "status",
        "history",
        "on",
        "off",
        "set",
        "press",
        "lock",
        "unlock",
        "open",
        "close",
        "play",
        "do",
    ]
    assert parameters["required"] == ["device", "action", *SETTINGS] and parameters["additionalProperties"] is False
    assert parameters["properties"]["device"]["enum"][-1] == "everything"


def test_the_rules_list_each_device_and_each_kinds_rules_once():
    rules = house().tools.rules()
    assert "- front door: a lock: status, lock, unlock" in rules and "Call unlock at once" in rules
    assert "call the tool even if its status says it's already that way" in rules
    assert rules.count("volume_up or volume_down pressed 3 times") == 1
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
        "kind must be androidtv, camera, spotify or a plugin's",
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
    assert "".join(brain.stream_reply("turn the boiler on", ALON)) == "Let's see. It's on."
    assert brain.answered_by == llm.LOOKED_UP and h.ha.state("switch.boiler") == "on"


def test_openai_alone_has_the_tool_and_is_told_what_else_it_cant_do():
    openai = fake_openai_chat(lambda m: "Fine.")
    brain = OpenAIChat(openai, LLMConfig(), home=house().tools)
    "".join(brain.stream_reply("hello"))
    request = openai.requests[0]
    assert "home" in [t.get("name") for t in request["tools"]]
    assert llm.CANT_UNLISTED in request["instructions"] and llm.CANT_HOUSE not in request["instructions"]


def searching_brain(h, action):
    """OpenAI's model, which searches the web first, then works the boiler."""

    def calls_for(messages):
        return [] if messages[-1].get("type") == "function_call_output" else [("home", args("boiler", action))]

    reply = fake_openai_chat(
        lambda m: "Done." if m[-1].get("type") else "",
        calls_for,
        search_for=lambda m: [] if m[-1].get("type") else ["response.web_search_call.in_progress"],
    )
    return reply, OpenAIChat(reply, LLMConfig(), home=h.tools)


def test_a_reply_that_searched_the_web_changes_nothing_in_the_house():
    h = house(boiler="off")
    openai, brain = searching_brain(h, "on")
    "".join(brain.stream_reply("it's cold out, isn't it? turn the boiler on", ALON))
    told = [i["output"] for i in openai.requests[-1]["input"] if i.get("type") == "function_call_output"]
    assert told[0].startswith("refused: this reply searched the web")
    assert h.ha.state("switch.boiler") == "off" and h.ha.calls == []


def test_a_reply_that_searched_the_web_still_reads_the_house():
    h = house(boiler="off")
    openai, brain = searching_brain(h, "status")
    "".join(brain.stream_reply("is the boiler on, and what's the weather?", ALON))
    told = [i["output"] for i in openai.requests[-1]["input"] if i.get("type") == "function_call_output"]
    assert told[0].startswith("boiler: ") and not told[0].startswith("refused")


def test_the_next_reply_may_change_the_house_again():
    h = house(boiler="off")
    _, brain = searching_brain(h, "on")
    "".join(brain.stream_reply("what's the weather? and the boiler on", ALON))
    assert h.ha.state("switch.boiler") == "off"
    calls = [("home", args("boiler", "on"))]
    brain._client = fake_openai_chat(
        lambda m: "On." if m[-1].get("type") else "", lambda m: [] if m[-1].get("type") else calls
    )
    "".join(brain.stream_reply("turn the boiler on", ALON))
    assert h.ha.state("switch.boiler") == "on"


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
    made = []
    call = h.tools.call

    def thrown_away_meanwhile(a, request):
        made[0].interrupt()  # they kept talking while the boiler turned on: this reply is thrown away,
        made[0].stream_reply("and the lights?")  # and the next one has begun
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
