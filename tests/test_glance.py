"""The house at a glance (a chat's /home): a short line a device, for a person rather than the model."""

import types

from voice_assistant.config import HomeConfig
from voice_assistant.hass import entity_device
from voice_assistant.home import Device, HomeError, HomeTools

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
}


def _state(entity: str) -> dict:
    state, name, unit = STATES[entity]
    return {"state": state, "attributes": {"friendly_name": name, **({"unit_of_measurement": unit} if unit else {})}}


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
