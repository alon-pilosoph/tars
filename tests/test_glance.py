"""The house at a glance (a chat's /home): a short line a device, for a person rather than the model."""

import types

from voice_assistant.config import HomeConfig, _home_problems
from voice_assistant.hass import entity_device
from voice_assistant.home import Context, Device, HomeError, HomeTools, build

STATES = {
    "lock.door": ("locked", "Home door", ""),
    "sensor.door_battery": ("80", "Home door battery", "%"),
    "switch.boiler": ("off", "Switcher Boiler 14F3", ""),
    "sensor.left": ("00:00:00", "Switcher Boiler 14F3 Remaining time", ""),
    "sensor.power": ("0", "Switcher Boiler 14F3 Power", "W"),
    "sensor.aqi": ("71", "Air quality index", ""),
    "sensor.pm": ("18", "airvisual pro - home PM2.5", "μg/m³"),
    "sensor.humidity": ("67", "airvisual pro - home Humidity", "%"),
    "sensor.gone": ("unavailable", "airvisual pro - home Battery", "%"),
    "sensor.out": ("40", "U.S. Air quality index", ""),
    "sensor.pollutant": ("p2", "U.S. Main pollutant", ""),
}


def _state(entity: str) -> dict:
    state, name, unit = STATES[entity]
    a = {"friendly_name": name, **({"unit_of_measurement": unit} if unit else {})}
    if entity == "sensor.pollutant":  # an enum sensor, as AirVisual's is
        a |= {"device_class": "enum", "options": ["co", "n2", "o3", "p1", "p2", "s2"]}
    return {"state": state, "attributes": a}


HASS = types.SimpleNamespace(state=_state)


def device(name, entity, **spec):
    return entity_device(name, {"entity": entity, **spec}, HASS)


def test_readings_lose_the_words_their_names_share_and_readings_of_nothing():
    boiler = device("boiler", "switch.boiler", details=["sensor.left", "sensor.power"])
    assert boiler.glance() == "off"  # 00:00:00 left and 0 W say nothing
    STATES["sensor.power"] = ("2950", "Switcher Boiler 14F3 Power", "W")
    try:
        assert boiler.glance() == "off · Power 2950 W"  # the boiler's whole name goes
    finally:
        STATES["sensor.power"] = ("0", "Switcher Boiler 14F3 Power", "W")
    # The device's own name shares nothing with them: the words they share among themselves go.
    air = device("air quality", "sensor.aqi", details=["sensor.pm", "sensor.humidity", "sensor.gone"])
    assert air.glance() == "71 · PM2.5 18 μg/m³ · Humidity 67%"
    assert device("front door", "lock.door", battery="sensor.door_battery").glance() == "locked · battery 80%"


class Said(Device):
    def __init__(self, name, said):
        super().__init__(name)
        self.said = said

    def glance(self):
        if isinstance(self.said, Exception):
            raise self.said
        return self.said


def test_airvisuals_pollutant_codes_are_said_in_words():
    outdoor = device("outdoor air", "sensor.out", details=["sensor.pollutant"])
    assert outdoor.glance() == "40 · Main pollutant PM2.5"
    assert "U.S. Main pollutant: PM2.5" in outdoor.status()  # the model reads it in words too


def test_a_label_is_how_its_written_and_the_name_stays_what_its_called(tmp_path):
    cfg = HomeConfig(
        url="http://ha.invalid:8123",
        devices=[
            {"name": "boiler", "entity": "switch.boiler", "label": "Water heater"},
            {"name": "front door", "entity": "lock.door"},
        ],
    )
    assert _home_problems(cfg) == []
    context = Context(tmp_path, lambda key: None)
    context.hass = HASS
    tools = build(cfg, context).tools
    assert list(tools.devices) == ["boiler", "front door"]  # still asked for as the boiler
    assert tools.glance() == "Water heater: off\nFront door: locked"
    bad = HomeConfig(
        url="http://ha.invalid:8123", devices=[{"name": "boiler", "entity": "switch.boiler", "label": " "}]
    )
    assert any("label" in p for p in _home_problems(bad))


def test_a_line_a_device_leaving_out_what_nothing_can_tell():
    tools = HomeTools(
        HomeConfig(),
        {
            "fire tv": Said("fire tv", "on · HBO Max"),
            "TV": Said("TV", None),
            "boiler": Said("boiler", HomeError("Home Assistant didn't answer")),
        },
    )
    assert tools.glance() == "Fire tv: on · HBO Max\nBoiler: can't tell (Home Assistant didn't answer)"
    assert HomeTools(HomeConfig(), {"TV": Said("TV", None)}).glance() == "Nothing in the house to show."


def test_by_default_a_glance_is_its_status_without_its_name():
    class Plain(Device):
        def status(self):
            return f"{self.name}: humming"

    assert Plain("fridge").glance() == "humming"
