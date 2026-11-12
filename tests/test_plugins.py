"""Plugins (plugins.py): declared, granted, scoped, and able to add tools of their own."""

import sys
import textwrap
from typing import ClassVar

import pytest

from voice_assistant.check import FAIL, OK, WARN, plugin_result
from voice_assistant.config import HomeConfig
from voice_assistant.home import LOCK, STATUS, UNLOCK, Context, Device, Request, build
from voice_assistant.plugins import BUILT_IN_TOOLS, GuardedHass, Manifest, Refused, admit, scoped

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


class FakeHouse(FakeHass):
    """Home Assistant with a Nuki (its lock and unlatch button on one device), a script and an automation that
    unlock it, a garage door, blinds and a light."""

    STATES: ClassVar[dict] = {
        "lock.front_door": {},
        "button.front_door_unlatch": {},
        "script.leave_home": {},
        "automation.welcome": {"id": "welcome"},
        "scene.evening": {"id": "evening"},
        "cover.garage": {"device_class": "garage"},
        "cover.blinds": {"device_class": "blind"},
        "light.kitchen": {},
    }
    CONFIGS: ClassVar[dict] = {
        ("script", "leave_home"): {"sequence": [{"action": "lock.unlock", "target": {"entity_id": "lock.front_door"}}]},
        ("automation", "welcome"): {"actions": [{"action": "button.press", "entity_id": "button.front_door_unlatch"}]},
        ("scene", "evening"): {"entities": {"light.kitchen": "on"}},
    }

    def states(self):
        return [{"entity_id": e, "state": "on", "attributes": a} for e, a in self.STATES.items()]

    def state(self, entity):
        return {"state": "on", "attributes": self.STATES.get(entity, {})}

    def entity_entries(self):
        return [{"entity_id": "lock.front_door", "device_id": "nuki"}, {"entity_id": "button.front_door_unlatch",
                "device_id": "nuki"}]  # fmt: skip

    def config_of(self, kind, key):
        return self.CONFIGS[(kind, key)]

    def remove_entry(self, entry_id):
        raise AssertionError("a plugin reached Home Assistant's setup")

    start_setup = answer_setup = update_device = stream_link = remove_entry


def guarded(tmp_path, needs=("home_assistant",)):
    hass = FakeHouse()
    return scoped(Context(tmp_path, SECRETS.get, hass=hass), set(needs)).hass, hass


@pytest.mark.parametrize(
    "service", ["lock.unlock?", "lock.unlock#x", "lock.unlock/x", "lock .unlock", "lock.unlock\nx", "lock", ""]
)
def test_a_service_is_only_ever_a_plain_name(tmp_path, service):
    for needs in (("home_assistant",), ("home_assistant", "unlock")):
        g, hass = guarded(tmp_path, needs)
        with pytest.raises(Refused, match="isn't a service's name"):
            g.call_service(service, {"entity_id": "lock.front_door"})
        assert hass.called == []


def test_the_service_checked_is_the_one_sent(tmp_path):
    g, hass = guarded(tmp_path)
    g.call(" Light.Turn_On ", {"entity_id": "light.kitchen"})
    assert hass.called == ["light.turn_on"]


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("script.leave_home", {}),  # a script that unlocks, run by its own service
        ("script.turn_on", {"entity_id": "script.leave_home"}),
        ("automation.trigger", {"entity_id": "automation.welcome"}),
        ("button.press", {"entity_id": "button.front_door_unlatch"}),  # on the lock's own device
        ("button.press", {"entity_id": "light.kitchen, button.front_door_unlatch"}),
        ("cover.open_cover", {"entity_id": "cover.garage"}),
        ("cover.toggle", {"target": {"entity_id": ["cover.garage"]}}),
        ("homeassistant.turn_off", {"entity_id": "lock.front_door"}),
        ("homeassistant.restart", {}),
        ("hassio.addon_stop", {}),
        ("alarm_control_panel.alarm_disarm", {"entity_id": "alarm_control_panel.home"}),
        ("light.turn_on", {"area_id": "hall"}),  # aimed wider than the entities it names
        ("light.turn_on", {"entity_id": "all"}),
    ],
)
def test_every_way_home_assistant_opens_the_house_needs_the_unlock_grant(tmp_path, service, data):
    g, hass = guarded(tmp_path)
    with pytest.raises(Refused):
        g.call_service(service, data)
    assert hass.called == []
    opened, hass = guarded(tmp_path, ("home_assistant", "unlock"))
    opened.call_service(service, data)
    assert hass.called == [service]


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("light.turn_on", {"entity_id": "light.kitchen"}),
        ("scene.turn_on", {"entity_id": "scene.evening"}),
        ("cover.open_cover", {"entity_id": "cover.blinds"}),
        ("cover.close_cover", {"entity_id": "cover.garage"}),
        ("lock.lock", {"entity_id": "lock.front_door"}),
    ],
)
def test_what_doesnt_open_the_house_goes_through(tmp_path, service, data):
    g, hass = guarded(tmp_path)
    g.call_service(service, data)
    assert hass.called == [service]


def test_a_plugin_never_holds_home_assistants_client(tmp_path):
    g, hass = guarded(tmp_path, ("home_assistant", "unlock"))
    assert g.state("light.kitchen")["state"] == "on" and len(g.states()) == len(FakeHouse.STATES)
    for name in dir(g):
        if not name.startswith("__"):
            assert getattr(getattr(g, name), "__self__", None) is not hass, name
    for admin in ("remove_entry", "start_setup", "answer_setup", "update_device", "stream_link", "_send", "_hass"):
        assert not hasattr(g, admin), admin


class Door(Device):
    actions = (STATUS, LOCK, UNLOCK)

    def __init__(self, name):
        super().__init__(name)
        self.done = []

    def what(self):
        return "a lock"

    def status(self):
        return f"{self.name}: locked"

    def call(self, action, args, request):
        self.done.append(action)
        return f"done: {action}"


def granted_devices(tmp_path, needs):
    context = Context(tmp_path, SECRETS.get, maintainers=["alon"])
    door, own = Door("front door"), Door("shed")
    context.devices |= {"front door": door, "shed": own}
    context.plugin_of["shed"] = "garden"
    return context, scoped(context, set(needs), "garden").devices, door, own


def test_a_plugin_sees_the_other_devices_but_cant_unlock_one_without_the_grant(tmp_path):
    context, devices, door, own = granted_devices(tmp_path, {"devices"})
    view = devices["front door"]
    assert view is not door and devices["shed"] is own  # its own devices, as they are
    assert (
        view.status() == "front door: locked"
        and view.actions == door.actions
        and set(devices) == {"front door", "shed"}
    )
    view.actions = ("explode",)  # a Lock unlocks for any action but lock: only its own are passed on
    for action in (UNLOCK, STATUS, "explode"):
        with pytest.raises(Refused):
            view.call(action, {}, Request())
    assert view.call(LOCK, {}, Request()) == "done: lock" and door.done == [LOCK]
    assert not hasattr(view, "_device") and getattr(view.status, "__self__", None) is not door
    with pytest.raises(TypeError):
        devices["front door"] = own
    context.devices["back door"] = Door("back door")  # made after the plugin's setup: seen when used
    assert devices.get("back door") is not None


def test_with_the_unlock_grant_a_plugin_unlocks_only_as_the_home_tool_would(tmp_path):
    context, devices, door, _ = granted_devices(tmp_path, {"devices", "unlock"})
    context.may_unlock = lambda request: None if request.speaker == "alon" else "refused: not a voice TARS trusts"
    assert devices["front door"].call(UNLOCK, {}, Request("stranger")) == "refused: not a voice TARS trusts"
    with pytest.raises(Refused):
        devices["front door"].call(UNLOCK, {}, None)
    assert devices["front door"].call(UNLOCK, {}, Request("alon")) == "done: unlock" and door.done == [UNLOCK]


def test_a_plugins_context_shares_nothing_it_could_change(tmp_path):
    context = Context(tmp_path, SECRETS.get, maintainers=["alon"])
    given = scoped(context, set(), "garden")
    with pytest.raises(AttributeError):
        given.maintainers.append("mallory")
    assert context.maintainers == ["alon"] and given.maintainers == ("alon",)
    assert given.plugin_of == {} and given.plugin_of is not context.plugin_of and given.may_unlock is None
    with pytest.raises(TypeError):
        given.devices["front door"] = Door("front door")


def test_two_plugins_cant_add_tools_of_the_same_name(garden):
    settings = {"module": "garden_plugin", "grant": ["GARDEN_KEY", "home_assistant"]}
    cfg = HomeConfig(plugins={"garden": settings, "garden_too": settings})
    built = build(cfg, Context(garden, SECRETS.get, hass=FakeHass()))
    assert not built.plugins["garden"].error and "needs a name of its own" in built.plugins["garden_too"].error
    assert {"water_garden", "garden_moisture"} <= built.tools.names


def test_every_tool_the_brain_has_of_its_own_is_kept_from_plugins(garden):
    import types

    from voice_assistant import feeds, myself, skills
    from voice_assistant.browser import errands
    from voice_assistant.llm import LLMConfig, OpenAIChat
    from voice_assistant.telegram import CHAT_REMIND_TOOLS

    from .conftest import fake_openai_chat

    def part(tool):
        return types.SimpleNamespace(tool=tool, can="", rules="")

    house = building(garden, ["GARDEN_KEY", "home_assistant"], FakeHass()).tools
    own = {"water_garden", "garden_moisture"}
    for remind_tools in (None, CHAT_REMIND_TOOLS):
        brain = OpenAIChat(
            fake_openai_chat(lambda m: ""),
            LLMConfig(web_search=True, send=True),
            reminders=types.SimpleNamespace(),
            remind_tools=remind_tools,
            home=house,
            browser=part(errands.BROWSE_TOOL),
            feeds=part(feeds.FEEDS_TOOL),
            skills=part(skills.SKILLS_TOOL),
            myself=part(myself.TOOL),
        )
        names = {t.get("name") or t["type"] for t in brain._tools} - own
        assert names <= BUILT_IN_TOOLS and len(names) == len(BUILT_IN_TOOLS)


def test_a_grant_names_only_what_can_be_granted():
    from voice_assistant.config import _home_problems

    bad = HomeConfig(plugins={"garden": {"module": "garden_plugin", "grant": ["home_assistant", "everything"]}})
    assert any("everything is neither a key's name" in p for p in _home_problems(bad))


def test_check_warns_when_a_plugin_is_granted_one_of_tarss_own_keys():
    from voice_assistant.plugins import Loaded

    result = plugin_result(Loaded("garden", "2.1.0", ("OPENAI_API_KEY",), ("OPENAI_API_KEY",)))
    assert result.status == WARN and "OPENAI_API_KEY is TARS's own" in result.detail


@pytest.mark.parametrize(
    ("plugin", "why"),
    [
        (Manifest("1.0", "home_assistant"), "its needs are a tuple"),
        (Manifest("1.0", ("home_assistant", 3)), "its needs are a tuple"),
        (Manifest("", ()), "its version"),
        (Manifest(None, ()), "its version"),
        ({"version": "1.0", "needs": ()}, "isn't a Manifest"),
    ],
)
def test_a_manifest_is_checked(tmp_path, plugin, why):
    module = type(sys)("odd")
    module.PLUGIN = plugin
    for grant in (None, ["home_assistant"]):
        with pytest.raises(Refused, match=why):
            admit("odd", module, grant, Context(tmp_path, SECRETS.get))
