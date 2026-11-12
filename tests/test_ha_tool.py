"""The home_assistant tool, against a pretend Home Assistant: any entity looked up, read, its past told, and changed,
with the home tool's rules for unlocking, and nothing that administers Home Assistant itself."""

import json
from datetime import datetime, timedelta

import httpx
import pytest

from voice_assistant.config import HomeConfig
from voice_assistant.ha_setup import ACTIONS as SETUP_ACTIONS
from voice_assistant.ha_tool import ACTIONS, HAAccess
from voice_assistant.hass import HomeAssistant
from voice_assistant.home import HomeTools, Request

NOW = datetime(2026, 11, 8, 22, 0).astimezone()


def stamp(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


class FakeHA:
    """Answers the REST calls the tool makes, and keeps every service call."""

    def __init__(self):
        self.states = {
            "switch.boiler": {"state": "off", "attributes": {"friendly_name": "Boiler"}},
            "sensor.ari_temp": {
                "state": "21.5",
                "attributes": {"friendly_name": "Ari's room temperature", "unit_of_measurement": "°C"},
            },
            "lock.front_door": {"state": "locked", "attributes": {"friendly_name": "Front door"}},
            "light.hall": {"state": "off", "attributes": {"friendly_name": "Hall light"}},
            "script.goodnight": {"state": "off", "attributes": {"friendly_name": "Goodnight"}},
            "script.movie": {"state": "off", "attributes": {"friendly_name": "Movie time"}},
            "button.front_door_unlatch": {"state": "unknown", "attributes": {"friendly_name": "Front door Unlatch"}},
            "button.front_door_lock_n_go": {
                "state": "unknown",
                "attributes": {"friendly_name": "Front door Lock 'n' Go"},
            },
            "script.let_in": {"state": "off", "attributes": {"friendly_name": "Let them in"}},
        }
        self.configs = {
            "script/goodnight": {"sequence": [{"action": "lock.lock", "target": {"entity_id": "lock.front_door"}}]},
            "script/movie": {"sequence": [{"action": "light.turn_off", "target": {"entity_id": "light.hall"}}]},
            "script/let_in": {
                "sequence": [{"action": "button.press", "target": {"entity_id": "button.front_door_lock_n_go"}}]
            },
        }
        # The entity registry: the Nuki's buttons are on the lock's own device.
        self.registry = [
            {"entity_id": "lock.front_door", "device_id": "nuki"},
            {"entity_id": "button.front_door_lock_n_go", "device_id": "nuki"},
            {"entity_id": "light.hall", "device_id": "hue"},
        ]
        self.calls, self.queries = [], []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.queries.append((path, dict(request.url.params)))
        if path == "/api/states":
            return httpx.Response(200, json=[{"entity_id": e, **s} for e, s in self.states.items()])
        if path.startswith("/api/states/"):
            entity = path.rsplit("/", 1)[1]
            if entity not in self.states:
                return httpx.Response(404, json={"message": "Entity not found."})
            return httpx.Response(200, json={"entity_id": entity, "last_changed": stamp(2), **self.states[entity]})
        if path == "/api/template":
            return httpx.Response(200, text="sensor.ari_temp\tAri's room\nswitch.boiler\tBathroom\n")
        if path.startswith("/api/history/period/"):
            wanted = request.url.params["filter_entity_id"].split(",")
            rows = []
            if "switch.boiler" in wanted:
                rows.append(
                    [
                        {"entity_id": "switch.boiler", "state": "off", "last_changed": stamp(30)},
                        {"state": "on", "last_changed": stamp(4.5)},
                        {"state": "off", "last_changed": stamp(3.9)},
                        {"state": "on", "last_changed": stamp(1)},
                    ]
                )
            return httpx.Response(200, json=rows)
        if path.startswith("/api/logbook/"):
            return httpx.Response(
                200,
                json=[
                    {
                        "when": stamp(3),
                        "name": "Front door",
                        "message": "was unlocked",
                        "entity_id": "lock.front_door",
                        "context_service": "unlock",
                        "context_domain": "lock",
                    },
                ],
            )
        if path == "/api/services":
            return httpx.Response(
                200,
                json=[
                    {
                        "domain": "light",
                        "services": {"turn_on": {"description": "Turn on", "fields": {"brightness_pct": {}}}},
                    },
                ],
            )
        if path.startswith("/api/config/"):
            key = path.removeprefix("/api/config/").replace("/config/", "/")
            if key not in self.configs:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=self.configs[key])
        if path.startswith("/api/services/"):
            domain, service = path.removeprefix("/api/services/").split("/")
            data = json.loads(request.content or b"{}")
            self.calls.append((f"{domain}.{service}", data))
            changed = []
            for entity in data.get("entity_id", []):
                new = {"turn_on": "on", "turn_off": "off", "unlock": "unlocked", "lock": "locked"}.get(service)
                if entity in self.states and new:
                    self.states[entity]["state"] = new
                    changed.append({"entity_id": entity, "state": new})
            return httpx.Response(200, json=changed)
        return httpx.Response(404, json={"message": f"no {path}"})


@pytest.fixture
def ha():
    fake = FakeHA()

    def command(message):
        return fake.registry if message["type"] == "config/entity_registry/list" else {}

    hass = HomeAssistant("http://ha.local:8123", "token", fake.transport, command=command)
    tools = HomeTools(HomeConfig(unlock_chats=["Alon"]), {}, hass)
    tools.ha._now = lambda: NOW
    tools.fake = fake
    return tools


def ask(tools: HomeTools, action: str, request: Request | None = None, **args) -> str:
    keys = ("entity_ids", "query", "since", "until", "every", "service", "data", "target")
    return tools.call_ha({"action": action, **dict.fromkeys(keys), **args}, request or Request(by_chat=True))


def test_with_home_assistant_and_no_devices_there_is_only_the_home_assistant_tool(ha):
    assert ha.names == {"home_assistant"} and "home_assistant" in ha.rules()
    assert ha.ha.tool["parameters"]["properties"]["action"]["enum"] == [*ACTIONS, *SETUP_ACTIONS]


def test_find_looks_in_names_rooms_and_ids_and_says_each_ones_state(ha):
    found = ask(ha, "find", query="ari")
    assert found == "sensor.ari_temp: Ari's room temperature (Ari's room): 21.5 °C"
    assert "switch.boiler: Boiler (Bathroom): off" in ask(ha, "find", query="bathroom")
    assert len(ask(ha, "find").splitlines()) == len(ha.fake.states)
    assert "nothing in Home Assistant matches" in ask(ha, "find", query="garage")


def test_how_long_the_boiler_was_on_is_in_its_history(ha):
    said = ask(ha, "history", entity_ids=["switch.boiler"], since="2026-11-08")
    # From midnight to 22:00: off till 17:30, on 36 minutes, off till 21:00, on for the last hour.
    assert "time in each state: off 20 h 24 min, on 1 h 36 min" in said
    assert "11-08 17:30 on" in said and "11-08 21:00 on" in said
    assert "no history in that span" in ask(ha, "history", entity_ids=["light.hall"])


def test_the_logbook_says_what_did_it(ha):
    assert "Front door: was unlocked (by lock.unlock)" in ask(ha, "logbook")


def test_after_a_web_search_the_past_isnt_read_and_nothing_changes(ha):
    searched = Request(by_chat=True, read_the_web=True)
    for action in ("history", "statistics", "logbook"):
        assert ask(ha, action, searched, entity_ids=["switch.boiler"]).startswith("refused: this reply searched")
    assert ask(ha, "state", searched, entity_ids=["switch.boiler"]).startswith("switch.boiler: off")
    said = ask(ha, "call", searched, service="light.turn_on", entity_ids=["light.hall"])
    assert said.startswith("refused") and ha.fake.calls == []


def test_a_change_waits_for_the_turn_and_says_what_it_did(ha):
    turned = ask(ha, "call", service="light.turn_on", entity_ids=["light.hall"], data='{"brightness_pct": 40}')
    assert turned == "done: light.turn_on; now light.hall on"
    assert ha.fake.calls == [("light.turn_on", {"brightness_pct": 40, "entity_id": ["light.hall"]})]
    withdrawn = Request(by_chat=True, may_act=lambda timeout: False)
    assert "withdrawn" in ask(ha, "call", withdrawn, service="light.turn_off", entity_ids=["light.hall"])
    assert len(ha.fake.calls) == 1
    assert "sent:" in ask(ha, "call", service="light.toggle", entity_ids=["light.hall"])


def test_home_assistant_itself_isnt_administered(ha):
    for service in ("homeassistant.restart", "recorder.purge", "update.install", "hassio.host_reboot"):
        assert ask(ha, "call", service=service).startswith(f"refused: {service.split('.')[0]} administers")
    assert ha.fake.calls == []
    assert "error: data must be a JSON object" in ask(ha, "call", service="light.turn_on", data="[1]")
    for aimed in ('{"entity_id": "lock.front_door"}', '{"device_id": "nuki"}', '{"area_id": "hall"}'):
        assert "data can't aim it" in ask(ha, "call", service="button.press", data=aimed)


def test_unlocking_keeps_the_home_tools_rules_however_its_asked(ha):
    stranger = Request(by_chat=True, speaker="Stacey", heard="unlock the door")
    alon = Request(by_chat=True, speaker="Alon", heard="unlock the door please")
    assert ask(ha, "call", stranger, service="lock.unlock", entity_ids=["lock.front_door"]).startswith("refused")
    # Not around the rules by another service on the lock, or a script that touches it.
    assert ask(ha, "call", stranger, service="lock.open", entity_ids=["lock.front_door"]).startswith("refused")
    assert ask(ha, "call", stranger, service="homeassistant.turn_on", entity_ids=["lock.front_door"]).startswith(
        "refused"
    )
    assert "it can unlock: script.goodnight" in ask(ha, "call", stranger, service="script.goodnight")
    assert ha.fake.calls == []
    # A script whose setup can't be read is treated as if it could.
    assert "it can unlock: script.mystery" in ask(ha, "call", stranger, service="script.mystery")
    assert ask(ha, "call", stranger, service="script.movie").startswith("done")  # no lock in it
    assert ask(ha, "call", alon, service="lock.unlock", entity_ids=["lock.front_door"]) == (
        "done: lock.unlock; now lock.front_door unlocked"
    )
    # Locking is never held back.
    assert ask(ha, "call", stranger, service="lock.lock", entity_ids=["lock.front_door"]).startswith("done")


def test_services_lists_a_domains_with_their_fields(ha):
    assert ask(ha, "services", query="light") == "light.turn_on: Turn on fields: brightness_pct"
    assert "no domain 'garage'" in ask(ha, "services", query="garage")


def test_the_brain_gets_both_tools_and_routes_each_call(ha):
    from voice_assistant.config import LLMConfig
    from voice_assistant.llm import OpenAIChat

    from .conftest import fake_openai_chat

    asked = {"action": "state", "entity_ids": ["switch.boiler"]}
    asked |= dict.fromkeys(("query", "since", "until", "every", "service", "data"))
    first = lambda m: len(m) <= 2  # the question alone: the first round
    client = fake_openai_chat(
        lambda m: "" if first(m) else "The boiler's off.", lambda m: [("home_assistant", asked)] * first(m)
    )
    brain = OpenAIChat(client, LLMConfig(), home=ha)
    assert "".join(brain.stream_reply("is the boiler on?")) == "The boiler's off."
    opening = client.requests[0]
    assert {t["name"] for t in opening["tools"] if "name" in t} >= {"home_assistant"}
    assert "all of Home Assistant" in opening["instructions"]
    output = next(i for i in client.requests[1]["input"] if i.get("type") == "function_call_output")
    assert output["output"].startswith("switch.boiler: off")


def test_without_home_assistant_theres_no_such_tool():
    tools = HomeTools(HomeConfig(), {})
    assert tools.ha is None and tools.tools == []
    assert HAAccess  # the module loads on its own


def test_a_door_opened_by_a_button_on_the_lock_or_a_script_pressing_one_keeps_the_unlock_rules(ha):
    # A Nuki's own buttons ("unlatch", "Lock 'n' Go") open the door without its lock entity: the hole found on the Pi.
    stacey = Request(by_chat=True, speaker="Stacey", heard="press the button")
    for button in ("button.front_door_unlatch", "button.front_door_lock_n_go"):  # by its name; by the lock's device
        said = ask(ha, "call", stacey, service="button.press", entity_ids=[button])
        assert said.startswith("refused") and f"{button} can open a door" in said
    assert "it can unlock: script.let_in" in ask(ha, "call", stacey, service="script.let_in")
    assert ha.fake.calls == []
    assert ask(ha, "call", stacey, service="light.turn_on", entity_ids=["light.hall"]).startswith("done")


@pytest.mark.parametrize(
    ("service", "ids", "data"),
    [
        # A scene applied or made on the spot, the lock in its entities: as a state, or a dict with one.
        ("scene.apply", None, {"entities": {"lock.front_door": "unlocked"}}),
        ("scene.apply", None, {"entities": {"lock.front_door": {"state": "unlocked"}}}),
        ("scene.apply", None, {"entities": {"group.doors": "unlocked"}}),  # a group with the lock in it
        ("scene.apply", None, {"entities": {"button.front_door_unlatch": "pressed"}}),
        ("scene.create", None, {"scene_id": "in", "entities": {"lock.front_door": {"state": "unlocked"}}}),
        ("scene.create", None, {"scene_id": "in", "snapshot_entities": ["lock.front_door"]}),
        # ...then turned on: one with the lock among its entities, one with no setup to read.
        ("scene.turn_on", ["scene.made"], None),
        ("scene.turn_on", ["scene.on_the_spot"], None),
        # A script that runs one that opens the door; a lock or its device handed to one; a template for its target.
        ("script.turn_on", ["script.bedtime"], None),
        ("automation.trigger", ["automation.chain"], None),
        ("script.turn_on", ["script.movie"], {"variables": {"door": "lock.front_door"}}),
        ("script.turn_on", ["script.movie"], {"variables": {"device_id": "nuki"}}),
        ("script.turn_on", ["script.movie"], {"variables": {"target": {"area_id": "hall"}}}),
        ("script.lights_for", None, {"which": "light.hall"}),
        ("button.press", ["all"], None),  # every button: the unlatch one too
        # What can't be seen into: a request to another server, a command sent straight to a device.
        ("rest_command.open_sesame", None, None),
        ("mqtt.publish", None, {"topic": "zigbee2mqtt/front_door/set", "payload": '{"state": "UNLOCK"}'}),
    ],
)
def test_a_door_reached_through_a_calls_data_keeps_the_unlock_rules(ha, service, ids, data):
    fake = ha.fake
    fake.states |= {
        "group.doors": {"state": "locked", "attributes": {"entity_id": ["lock.front_door", "light.hall"]}},
        "scene.made": {"state": "unknown", "attributes": {"entity_id": ["light.hall", "lock.front_door"]}},
        "scene.on_the_spot": {"state": "unknown", "attributes": {}},
        "script.bedtime": {"state": "off", "attributes": {}},
        "script.lights_for": {"state": "off", "attributes": {}},
        "automation.chain": {"state": "on", "attributes": {"id": "chain"}},
    }
    fake.configs |= {
        "script/bedtime": {"sequence": [{"action": "light.turn_off"}, {"action": "script.let_in"}]},
        "script/lights_for": {"sequence": [{"action": "light.turn_on", "target": {"entity_id": "{{ which }}"}}]},
        "automation/chain": {"actions": [{"action": "script.turn_on", "target": {"entity_id": "script.bedtime"}}]},
    }
    stacey = Request(by_chat=True, speaker="Stacey", heard="set the scene")
    alon = Request(by_chat=True, speaker="Alon", heard="unlock the door please")
    asked = {"service": service, "entity_ids": ids, "data": json.dumps(data) if data else None}
    assert ask(ha, "call", stacey, **asked).startswith("refused") and fake.calls == []
    assert not ask(ha, "call", alon, **asked).startswith(("refused", "error")) and len(fake.calls) == 1


def test_a_call_whose_data_reaches_no_door_goes_ahead(ha):
    stacey = Request(by_chat=True, speaker="Stacey", heard="evening, please")
    for service, ids, data in [
        ("scene.apply", None, {"entities": {"light.hall": {"state": "on", "brightness": 80}}}),
        ("light.turn_off", ["all"], None),  # every light: none opens a door
        ("script.turn_on", ["script.movie"], {"variables": {"device_id": "hue"}}),
    ]:
        said = ask(ha, "call", stacey, service=service, entity_ids=ids, data=json.dumps(data) if data else None)
        assert said.startswith(("done", "sent")), said
    assert len(ha.fake.calls) == 3


def test_only_maintainers_may_change_home_assistants_setup():
    fake = FakeHA()
    hass = HomeAssistant("http://ha.local:8123", "token", fake.transport, command=lambda m: {})
    assert HomeTools(HomeConfig(), {}, hass, None, ["Alon"]).ha._setup._people == {"alon"}
    assert HomeTools(HomeConfig(), {}, hass).ha._setup._people == set()  # none named: nobody
