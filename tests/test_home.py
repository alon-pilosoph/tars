"""The house: the home tool against a fake Home Assistant. Changes are confirmed from what the device reports, the
lock's rules hold whatever the model asks, nothing moves for a draft that's thrown away, and the brain offers the
tool, hands it over and says "One moment." while a lock turns."""

import json
import threading
import types
from pathlib import Path

import httpx
import pytest

from voice_assistant import llm
from voice_assistant.ac import Blaster
from voice_assistant.config import ConfigError, HomeConfig, load_config
from voice_assistant.draft import Draft
from voice_assistant.home import (
    CONFIRM_WITHIN_S,
    ONE_MOMENT,
    HomeAssistant,
    HomeTools,
    Request,
    _left,
)
from voice_assistant.llm import LLMConfig, OpenAIChat, QuickChat, QuickService

from .conftest import FakeStream, ToolCallStream, fake_openai_chat

DEVICES = [
    {"name": "Front door", "kind": "lock", "entity": "lock.front_door", "door": "binary_sensor.front_door_door"},
    {"name": "boiler", "kind": "boiler", "entity": "switch.boiler", "remaining": "sensor.boiler_remaining_time"},
]
ALON = Request("alon", 0.8, by_voice=True)


class FakeHA:
    """Home Assistant's REST API: states, and services that change them, the lock taking `steps` reads to turn."""

    def __init__(self, states=None, steps=2):
        self.states = {
            "lock.front_door": "unlocked",
            "binary_sensor.front_door_door": "off",
            "switch.boiler": "off",
            "sensor.boiler_remaining_time": "00:00:00",
            **(states or {}),
        }
        self.calls, self.steps, self.down, self.jam, self.moving = [], steps, False, False, {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no route to host")
        if request.headers["Authorization"] != "Bearer token":
            return httpx.Response(401)
        path = request.url.path
        if path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if path.startswith("/api/states/"):
            entity = path.removeprefix("/api/states/")
            if entity not in self.states:
                return httpx.Response(404, json={"message": "Entity not found."})
            if entity in self.moving:
                left, final = self.moving[entity]
                if left <= 0:
                    self.states[entity] = final
                    del self.moving[entity]
                else:
                    self.moving[entity] = (left - 1, final)
            return httpx.Response(200, json={"entity_id": entity, "state": self.states[entity], "attributes": {}})
        domain, service = path.removeprefix("/api/services/").split("/")
        data = json.loads(request.content)
        self.calls.append((domain, service, data))
        entity = data["entity_id"]
        if domain == "lock":
            final = "jammed" if self.jam else {"lock": "locked", "unlock": "unlocked"}[service]
            self.states[entity] = f"{service}ing"
            self.moving[entity] = (self.steps, final)
        elif service in ("turn_on", "turn_on_with_timer"):
            self.states[entity] = "on"
        elif service == "turn_off":
            self.states[entity] = "off"
        return httpx.Response(200, json=[])


def home(ha: FakeHA | None = None, unlock_voices=("alon",), devices=DEVICES, **cfg) -> tuple[HomeTools, FakeHA]:
    ha = ha or FakeHA()
    hass = HomeAssistant("http://ha.local:8123", "token", transport=httpx.MockTransport(ha.handle))
    config = HomeConfig(url="http://ha.local:8123", unlock_voices=list(unlock_voices), devices=devices, **cfg)
    clock = types.SimpleNamespace(t=0.0)

    def sleep(s):
        clock.t += s

    tools = HomeTools(config, hass, clock=lambda: clock.t, sleep=sleep)
    tools.clock = clock
    return tools, ha


NO_SETTINGS = dict.fromkeys(("minutes", "mode", "temperature", "fan", "button", "times", "app", "goal"))


def args(device, action, **extra):
    return {"device": device, "action": action, **NO_SETTINGS, "confirmed": False, **extra}


def numbered(request: Request, n: int) -> Request:
    return Request(request.speaker, request.score, request.by_voice, request.may_act, number=n)


# Locking and the boiler.


def test_locking_waits_for_the_lock_to_say_it_is_locked():
    tools, ha = home()
    said = tools.call(args("front door", "lock"), ALON)
    assert said.startswith("done: the front door is locked") and ha.calls == [
        ("lock", "lock", {"entity_id": "lock.front_door"})
    ]


def test_a_lock_that_never_finishes_turning_is_not_said_to_be_locked():
    tools, _ha = home(FakeHA(steps=1000))
    said = tools.call(args("front door", "lock"), ALON)
    assert said.startswith("not confirmed") and "locking" in said


def test_a_jammed_lock_is_said_to_be_jammed():
    tools, ha = home()
    ha.jam = True
    assert tools.call(args("front door", "lock"), ALON) == "error: the front door jammed; someone needs to check it"


def test_an_open_door_is_not_locked():
    tools, ha = home(FakeHA({"binary_sensor.front_door_door": "on"}))
    said = tools.call(args("front door", "lock"), ALON)
    assert said.startswith("error: the door sensor says the door is open") and ha.calls == []


def test_the_boiler_goes_on_for_some_minutes_with_the_switchers_own_timer():
    tools, ha = home()
    said = tools.call(args("boiler", "on", minutes=40), ALON)
    assert said.startswith("done: the boiler is on") and said.endswith("for 40 minutes")
    assert ha.calls == [("switcher_kis", "turn_on_with_timer", {"entity_id": "switch.boiler", "timer_minutes": 40})]


@pytest.mark.parametrize("minutes", [0, 151, "lots"])
def test_a_boiler_timer_it_cant_set_is_an_error(minutes):
    tools, ha = home()
    assert tools.call(args("boiler", "on", minutes=minutes), ALON).startswith("error") and ha.calls == []


def test_status_reads_every_device_and_what_it_says_about_itself():
    tools, _ = home(FakeHA({"switch.boiler": "on", "sensor.boiler_remaining_time": "01:25:00"}))
    said = tools.call(args("everything", "status"), Request())
    assert said == "front door: unlocked, door closed\nboiler: on, heating, 1 hour 25 minutes left"


def test_everything_is_only_for_status():
    tools, _ = home()
    assert tools.call(args("everything", "off"), ALON).startswith("error")


def test_a_device_it_doesnt_have_or_an_action_it_cant_do_is_an_error():
    tools, ha = home()
    assert "there are: front door, boiler" in tools.call(args("garage", "on"), ALON)
    assert tools.call(args("boiler", "lock"), ALON).startswith("error: the boiler can do")
    assert ha.calls == []


def test_home_assistant_not_answering_is_an_error_that_says_nothing_changed():
    tools, ha = home()
    ha.down = True
    said = tools.call(args("boiler", "on"), ALON)
    assert said.startswith("error: Home Assistant didn't answer") and "nothing is known to have changed" in said


def test_a_refused_token_says_where_the_token_goes():
    ha = FakeHA()
    hass = HomeAssistant("http://ha.local:8123", "wrong", transport=httpx.MockTransport(ha.handle))
    tools = HomeTools(HomeConfig(url="http://ha", devices=DEVICES), hass, sleep=lambda s: None)
    assert "HOME_ASSISTANT_TOKEN" in tools.call(args("boiler", "on"), ALON)


@pytest.mark.parametrize(
    ("value", "said"), [("01:25:00", "1 hour 25 minutes"), ("0:40:00", "40 minutes"), ("90", "1 hour 30 minutes")]
)
def test_time_left_is_said_the_way_people_say_it(value, said):
    assert _left(value) == said


# Unlocking: the rules are the tool's, whatever the model asks.


def test_unlocking_asks_first_then_unlocks_on_a_yes_in_the_next_turn():
    tools, ha = home()
    first = tools.call(args("front door", "unlock"), numbered(ALON, 1))
    assert first.startswith('ask first: ask "Unlock the front door?"') and ha.calls == []
    said = tools.call(args("front door", "unlock", confirmed=True), numbered(ALON, 2))
    assert said.startswith("done: the front door is unlocked")
    assert ha.calls == [("lock", "unlock", {"entity_id": "lock.front_door"})]


def test_a_yes_the_model_makes_up_in_the_same_turn_is_not_a_yes():
    tools, ha = home()
    tools.call(args("front door", "unlock"), numbered(ALON, 1))
    assert tools.call(args("front door", "unlock", confirmed=True), numbered(ALON, 1)).startswith("ask first")
    assert ha.calls == []


def test_confirmed_with_nothing_asked_is_not_a_yes():
    tools, ha = home()
    assert tools.call(args("front door", "unlock", confirmed=True), numbered(ALON, 5)).startswith("ask first")
    assert ha.calls == []


def test_a_yes_from_someone_else_is_not_a_yes():
    tools, ha = home(unlock_voices=("alon", "stacey"))
    tools.call(args("front door", "unlock"), numbered(ALON, 1))
    stacey = Request("stacey", 0.9, by_voice=True, number=2)
    assert tools.call(args("front door", "unlock", confirmed=True), stacey).startswith("ask first")
    assert ha.calls == []


def test_a_yes_long_after_the_question_is_not_a_yes():
    tools, _ha = home()
    tools.call(args("front door", "unlock"), numbered(ALON, 1))
    tools.clock.t += CONFIRM_WITHIN_S + 1
    assert tools.call(args("front door", "unlock", confirmed=True), numbered(ALON, 2)).startswith("ask first")


@pytest.mark.parametrize(
    ("request_", "why"),
    [
        (Request(), "only by voice"),
        (Request("stacey", 0.9, by_voice=True), "only for voices"),
        (Request(None, None, by_voice=True), "only for voices"),
        (Request("alon", 0.55, by_voice=True), "isn't sure enough"),
    ],
)
def test_only_a_trusted_voice_recognised_surely_can_unlock(request_, why):
    tools, ha = home()
    for n in (1, 2):
        said = tools.call(args("front door", "unlock", confirmed=n == 2), numbered(request_, n))
        assert said.startswith("refused") and why in said
    assert ha.calls == []


def test_nobody_can_unlock_by_voice_unless_someone_is_named():
    tools, _ha = home(unlock_voices=())
    assert "unlock_voices is empty" in tools.call(args("front door", "unlock"), ALON)


# Drafts: nothing moves for an answer that's thrown away.


def test_a_change_waits_for_the_turn_to_be_over():
    tools, ha = home()
    draft = Draft(threading.Lock(), lambda d: None, lambda r: None)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(tools.call(args("boiler", "on"), Request(may_act=draft.may_act)))
    )
    worker.start()
    worker.join(0.2)
    assert worker.is_alive() and ha.calls == []  # still drafting
    draft.go()
    worker.join(2)
    assert result[0].startswith("done") and len(ha.calls) == 1


def test_a_draft_thrown_away_changes_nothing():
    tools, ha = home()
    draft = Draft(threading.Lock(), lambda d: None, lambda r: None)
    draft.cancel()
    said = tools.call(args("boiler", "on"), Request(may_act=draft.may_act))
    assert said.startswith("error: the request was withdrawn") and ha.calls == []


def test_status_never_waits_for_the_turn():
    tools, _ha = home()
    never = Request(may_act=lambda timeout: pytest.fail("status waited"))
    assert tools.call(args("boiler", "status"), never) == "boiler: off"


# Settings.


def test_devices_are_checked_when_the_config_loads(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[home]
url = "http://homeassistant.local:8123"
[[home.devices]]
name = "front door"
kind = "lock"
entity = "switch.front_door"
[[home.devices]]
name = "Front Door"
kind = "garage"
[[home.devices]]
name = "ac"
kind = "ac"
blaster = "abc"
colour = "blue"
"""
    )
    with pytest.raises(ConfigError) as e:
        load_config(path)
    message = str(e.value.code)
    assert "entity must be a lock.* entity" in message
    assert "two devices have that name" in message and "kind must be lock, boiler, switch, ac" in message
    assert "(ac) has no setting colour" in message and "goes through the IR blaster: set [home] blaster" in message


def test_a_blaster_that_wont_say_its_protocol_is_told_it(tmp_path):
    path = tmp_path / "config.toml"
    blaster = '[home]\nblaster = {{ id = "bf41", address = "10.0.0.9", version = "3.5"{} }}\n'
    path.write_text(blaster.format(", control_type = 1"))
    assert Blaster.from_spec(load_config(path).home.blaster, "key")._control_type == 1
    path.write_text(blaster.format(", control_type = 3"))
    with pytest.raises(ConfigError, match="control_type must be 1"):
        load_config(path)
    path.write_text(blaster.format(""))
    assert Blaster.from_spec(load_config(path).home.blaster, "key")._control_type == 0


def test_the_shipped_config_loads_with_no_devices():
    assert load_config(Path(__file__).parent.parent / "config.toml").home.devices == []


# The brain.


def test_the_quick_model_works_the_house_itself_and_says_one_moment_while_the_lock_turns():
    tools, _ha = home()
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
        home=tools,
    )
    said = "".join(brain.stream_reply("[Speaker: alon] lock the front door", ALON))
    assert said == f"{ONE_MOMENT} Locked." and brain.answered_by == llm.QUICK
    assert [t["function"]["name"] for t in rounds[0]["tools"]] == ["send", "home"]
    assert "front door: a lock" in rounds[0]["messages"][0]["content"]
    assert rounds[1]["messages"][-1]["content"].startswith("done: the front door is locked")


def test_without_quick_tools_the_house_is_handed_to_openai():
    tools, ha = home()
    cerebras = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: FakeStream("<look-up>")))
    )

    def calls_for(messages):
        return [] if messages[-1].get("type") == "function_call_output" else [("home", args("boiler", "on"))]

    openai = fake_openai_chat(lambda m: "It's on." if m[-1].get("type") else "", calls_for)
    brain = QuickChat(openai, LLMConfig(), [QuickService("Cerebras", cerebras, "qwen")], home=tools)
    assert "".join(brain.stream_reply("turn the boiler on", ALON)) == "It's on."
    assert brain.answered_by == llm.LOOKED_UP and ha.states["switch.boiler"] == "on"


def test_openai_alone_has_the_tool_and_is_told_what_else_it_cant_do():
    tools, _ = home()
    openai = fake_openai_chat(lambda m: "Fine.")
    brain = OpenAIChat(openai, LLMConfig(), home=tools)
    "".join(brain.stream_reply("hello"))
    request = openai.requests[0]
    assert "home" in [t.get("name") for t in request["tools"]]
    assert llm.CANT_BUT_HOME in request["instructions"] and llm.CANT not in request["instructions"]


def test_each_reply_gets_its_own_request_number():
    tools, _ = home()
    brain = OpenAIChat(fake_openai_chat(lambda m: "Fine."), LLMConfig(), home=tools)
    first, second = Request(), Request()
    "".join(brain.stream_reply("one", first))
    "".join(brain.stream_reply("two", second))
    assert 0 < first.number < second.number


def test_devices_tars_works_itself_are_listed_with_what_they_can_do_and_wait_for_the_turn():
    from voice_assistant.ir import CODE_SETS, Remote

    sent = []
    devices = [*DEVICES, {"name": "soundbar", "kind": "ir", "codes": "polk-re9641"}, {"name": "AC", "kind": "ac"}]
    config = HomeConfig(url="http://ha", devices=devices, blaster={"id": "x", "address": "10.0.0.9"})
    soundbar = Remote("soundbar", CODE_SETS["polk-re9641"], sent.append, sleep=lambda s: None)
    tools = HomeTools(config, None, {"soundbar": soundbar})
    assert list(tools.devices) == ["soundbar"]  # no Home Assistant, and no AC profile: only what can be worked
    assert "- soundbar: a Polk soundbar, by IR: press a button (power, mute, volume_up" in tools.rules()
    assert "use times for how many presses" in tools.rules()
    withdrawn = Request(may_act=lambda timeout: False)
    assert tools.call(args("soundbar", "press", button="mute"), withdrawn).startswith(
        "error: the request was withdrawn"
    )
    assert sent == []
    assert tools.call(args("soundbar", "press", button="mute"), Request()).startswith("sent: mute")


def test_only_an_unlock_that_turns_the_lock_says_one_moment_first():
    tools, _ = home()
    assert tools.slow(json.dumps(args("front door", "lock")))
    assert tools.slow(json.dumps(args("front door", "unlock", confirmed=True)))
    assert not tools.slow(json.dumps(args("front door", "unlock")))


def test_waking_the_fire_tv_says_one_moment_first():
    tools, _ = home(devices=[*DEVICES, {"name": "fire tv", "kind": "firetv", "address": "10.0.0.8"}])
    tools.devices["fire tv"] = types.SimpleNamespace(kind="firetv")
    assert tools.slow(json.dumps(args("fire tv", "on")))
    assert not tools.slow(json.dumps(args("fire tv", "press", button="pause")))
