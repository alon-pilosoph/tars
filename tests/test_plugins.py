"""Plugins (plugins.py): declared, granted, scoped, and able to add tools of their own."""

import sys
import textwrap

import pytest

from voice_assistant.check import FAIL, OK, WARN, plugin_result
from voice_assistant.config import HomeConfig
from voice_assistant.home import Context, Request, build
from voice_assistant.plugins import GuardedHass, Manifest, Refused, admit, scoped

PLUGIN = """
from voice_assistant.home import Device
from voice_assistant.plugins import Manifest, PluginTool, Provides

PLUGIN = Manifest(version="2.1.0", needs=("GARDEN_KEY", "home_assistant"))
seen = {}


class Sprinkler(Device):
    actions = ("status", "on")

    def what(self):
        return "the sprinkler"

    def status(self):
        return f"{self.name}: off"


def setup(settings, context):
    seen["key"] = context.secret("GARDEN_KEY")
    seen["context"] = context
    watered = []

    def water(args, request):
        watered.append(args)
        return "done: watered"

    tools = [
        PluginTool("water_garden", "Waters the garden", {"type": "object", "properties": {}}, water, changes=True),
        PluginTool("garden_moisture", "How wet the soil is", {"type": "object", "properties": {}},
                   lambda a, r: "soil: 31%", rules="The garden's soil moisture is garden_moisture's."),
    ]
    seen["watered"] = watered
    return Provides(kinds={"sprinkler": lambda spec: Sprinkler(spec["name"])}, tools=tools)
"""


@pytest.fixture
def garden(tmp_path, monkeypatch):
    (tmp_path / "garden_plugin.py").write_text(textwrap.dedent(PLUGIN))
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    sys.modules.pop("garden_plugin", None)


class FakeHass:
    def __init__(self):
        self.called = []

    def call(self, service, data):
        self.called.append(service)
        return True

    def call_service(self, service, data):
        self.called.append(service)
        return []

    def state(self, entity):
        return {"state": "on"}

    def _send(self, method, path, data=None):
        raise AssertionError("a plugin reached Home Assistant's raw API")


SECRETS = {"GARDEN_KEY": "g4rden", "OPENAI_API_KEY": "sk-not-yours"}


def building(root, grant=None, hass=None):
    settings = {"module": "garden_plugin", **({"grant": grant} if grant is not None else {})}
    cfg = HomeConfig(plugins={"garden": settings}, devices=[{"name": "sprinkler", "kind": "garden.sprinkler"}])
    return build(cfg, Context(root, SECRETS.get, hass=hass, announce=print))


def test_a_plugin_granted_what_it_needs_loads_with_only_that(garden):
    import garden_plugin

    built = building(garden, ["GARDEN_KEY", "home_assistant"], FakeHass())
    assert "sprinkler" in built.tools.devices and built.unavailable == {}
    given = garden_plugin.seen["context"]
    assert garden_plugin.seen["key"] == "g4rden" and given.announce is None and given.devices == {}
    with pytest.raises(Refused, match="OPENAI_API_KEY isn't granted"):
        given.secret("OPENAI_API_KEY")
    assert isinstance(given.hass, GuardedHass)
    assert plugin_result(built.plugins["garden"]).detail == "2.1.0: granted GARDEN_KEY, home_assistant"


def test_a_plugin_that_needs_more_than_its_granted_doesnt_load(garden):
    built = building(garden, ["GARDEN_KEY"])
    assert "home_assistant, which [home.plugins.garden] grant doesn't give it" in built.unavailable["sprinkler"]
    result = plugin_result(built.plugins["garden"])
    assert result.status == FAIL and "didn't load" in result.detail


def test_without_a_grant_it_loads_with_everything_and_check_says_what_to_pin(garden):
    import garden_plugin

    built = building(garden, hass=FakeHass())
    assert "sprinkler" in built.tools.devices and garden_plugin.seen["context"].announce is print
    result = plugin_result(built.plugins["garden"])
    assert result.status == WARN and 'grant = ["GARDEN_KEY", "home_assistant"]' in result.fix


def test_a_plugin_that_declares_nothing_cant_be_granted(tmp_path):
    module = type(sys)("bare")
    with pytest.raises(Refused, match="declares nothing"):
        admit("bare", module, ["home_assistant"], Context(tmp_path, SECRETS.get))
    _, loaded = admit("bare", module, None, Context(tmp_path, SECRETS.get))
    assert loaded.version == "unversioned" and loaded.granted is None


@pytest.mark.parametrize("needs", [("home assistant",), ("lowercase_key",), ("everything",)])
def test_a_need_is_a_keys_name_or_one_of_the_known_ones(tmp_path, needs):
    module = type(sys)("odd")
    module.PLUGIN = Manifest("1.0", needs)
    with pytest.raises(Refused, match="isn't a key's name"):
        admit("odd", module, list(needs), Context(tmp_path, SECRETS.get))


@pytest.mark.parametrize("service", ["lock.unlock", "lock.open", "alarm_control_panel.alarm_disarm", "LOCK.UNLOCK"])
def test_home_assistant_never_opens_the_house_for_a_plugin_without_the_unlock_grant(tmp_path, service):
    hass = FakeHass()
    given = scoped(Context(tmp_path, SECRETS.get, hass=hass), {"home_assistant"})
    with pytest.raises(Refused, match="needs the unlock grant"):
        given.hass.call(service, {"entity_id": "lock.front_door"})
    with pytest.raises(Refused):
        given.hass.call_service(service, {})
    assert given.hass.call("lock.lock", {}) and given.hass.state("lock.front_door") == {"state": "on"}
    assert hass.called == ["lock.lock"]
    with pytest.raises(AttributeError):
        given.hass._send  # noqa: B018 - the client's own private methods aren't handed over
    opened = scoped(Context(tmp_path, SECRETS.get, hass=hass), {"home_assistant", "unlock"})
    assert opened.hass.call(service, {})


def test_without_the_home_assistant_grant_theres_no_home_assistant(tmp_path):
    assert scoped(Context(tmp_path, SECRETS.get, hass=FakeHass()), set()).hass is None


def test_a_plugins_tools_reach_the_brain_and_one_that_changes_waits_for_the_turn(garden):
    import garden_plugin

    house = building(garden, ["GARDEN_KEY", "home_assistant"], FakeHass()).tools
    names = [t["name"] for t in house.tools]
    assert {"water_garden", "garden_moisture"} <= set(names)
    assert "The garden's soil moisture is garden_moisture's." in house.rules()
    assert house.call_plugin("garden_moisture", {}, Request(read_the_web=True)) == "soil: 31%"
    assert house.call_plugin("water_garden", {}, Request(read_the_web=True)).startswith("refused")
    withdrawn = Request(may_act=lambda timeout: False)
    assert house.call_plugin("water_garden", {}, withdrawn).startswith("error: the request was withdrawn")
    assert garden_plugin.seen["watered"] == []
    assert house.call_plugin("water_garden", {"zone": 1}, Request()) == "done: watered"
    assert garden_plugin.seen["watered"] == [{"zone": 1}]


def test_a_plugin_tool_cant_take_a_built_in_name(garden):
    (garden / "garden_plugin.py").write_text(
        textwrap.dedent(PLUGIN).replace('PluginTool("water_garden"', 'PluginTool("home"')
    )
    built = building(garden, ["GARDEN_KEY", "home_assistant"], FakeHass())
    assert "needs a name of its own" in built.unavailable["sprinkler"]


def test_the_grant_is_checked_in_the_settings():
    from voice_assistant.config import _home_problems

    bad = HomeConfig(plugins={"garden": {"module": "garden_plugin", "grant": "home_assistant"}})
    assert any("grant is a list" in p for p in _home_problems(bad))
    ok = HomeConfig(plugins={"garden": {"module": "garden_plugin", "grant": ["home_assistant"]}})
    assert not any("grant" in p for p in _home_problems(ok))


def test_check_says_ok_for_a_granted_plugin():
    from voice_assistant.plugins import Loaded

    assert plugin_result(Loaded("garden", "2.1.0", ("X",), ("X",))).status == OK


def test_the_brain_calls_a_plugins_tool_and_reads_back_what_it_said(garden):
    import types

    from voice_assistant.llm import LLMConfig, QuickChat, QuickService

    from .conftest import FakeStream, ToolCallStream, fake_openai_chat

    house = building(garden, ["GARDEN_KEY", "home_assistant"], FakeHass()).tools
    rounds = []

    def create(**kw):
        rounds.append(kw)
        if len(rounds) == 1:
            return ToolCallStream([("garden_moisture", "{}")])
        return FakeStream("The soil's at 31 percent.")

    quick = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    brain = QuickChat(
        fake_openai_chat(lambda m: "From OpenAI."),
        LLMConfig(quick_tools=True),
        [QuickService("Cerebras", quick, "qwen")],
        home=house,
    )
    said = "".join(brain.stream_reply("[Speaker: alon] how wet is the garden?", Request("alon", 0.9, by_voice=True)))
    assert said == "The soil's at 31 percent."
    assert "garden_moisture" in [t["function"]["name"] for t in rounds[0]["tools"]]
    assert rounds[1]["messages"][-1]["content"] == "soil: 31%"
