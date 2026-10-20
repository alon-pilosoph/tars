"""The plugin as TARS loads it: named in [home.plugins], its kinds of device made from [[home.devices]], and what's
missing said per device rather than stopping TARS."""

import json

import pytest

from examples.my_house import blaster as blaster_module
from examples.my_house import ir
from examples.my_house.ac import Profile
from examples.my_house.direct_play import FireTv
from examples.my_house.ir import Remote
from examples.my_house.tests.test_ac import electra_profile
from voice_assistant.config import HomeConfig
from voice_assistant.found import Seen
from voice_assistant.home import Context, HomeError, build

BLASTER = {"id": "bf0123456789abcdef", "address": "192.168.1.50", "version": "3.5", "control_type": 2}


def house(tmp_path, devices, blaster=BLASTER, secrets=None, **settings) -> tuple:
    plugin = {"module": "examples.my_house", **({"blaster": blaster} if blaster else {}), **settings}
    cfg = HomeConfig(plugins={"my_house": plugin}, devices=devices)
    keys = {"TUYA_LOCAL_KEY": "0123456789abcdef"} if secrets is None else secrets
    context = Context(tmp_path, keys.get)
    return build(cfg, context), context


def with_profile(tmp_path):
    folder = tmp_path / "voice_data" / "ac"
    folder.mkdir(parents=True)
    (folder / "profile.json").write_text(json.dumps(electra_profile()))


def test_its_kinds_are_made_from_the_devices_config_lists(tmp_path):
    with_profile(tmp_path)
    built, _ = house(
        tmp_path,
        [
            {"name": "AC", "kind": "my_house.ac"},
            {"name": "TV", "kind": "my_house.remote", "codes": "samsung-tv", "awake_from": "fire tv"},
            {"name": "soundbar", "kind": "my_house.remote", "codes": "polk-re9641"},
            {"name": "fire tv", "kind": "my_house.firetv", "address": "192.168.1.60"},
        ],
    )
    assert built.unavailable == {}
    devices = built.tools.devices
    assert list(devices) == ["ac", "tv", "soundbar", "fire tv"]
    assert isinstance(devices["tv"], Remote) and isinstance(devices["fire tv"], FireTv)
    # Where `python -m examples.my_house.ir test TV` keeps what it found: the name in lower case, on any machine.
    assert devices["tv"].tested == ir.tested_path(tmp_path, "TV") == tmp_path / "voice_data" / "ir" / "tv.json"
    assert isinstance(devices["ac"].profile, Profile)
    rules = built.tools.rules()
    assert "is_on or is_off" in rules and "volume and mute are its" in rules and "play with the app" in rules


def test_the_fire_tvs_agent_is_told_how_stremio_lists_streams(tmp_path):
    made = []
    cfg = HomeConfig(
        plugins={"my_house": {"module": "examples.my_house"}},
        devices=[{"name": "fire tv", "kind": "my_house.firetv", "address": "192.168.1.60"}],
    )
    context = Context(tmp_path, {}.get, tv_agent=lambda box, **settings: made.append(settings))
    build(cfg, context)
    assert "first one that plays" in made[0]["extra_rules"]


def test_what_a_device_is_missing_is_said_for_that_device_alone(tmp_path):
    built, _ = house(
        tmp_path,
        [
            {"name": "AC", "kind": "my_house.ac"},  # never taught the remote
            {"name": "TV", "kind": "my_house.remote", "codes": "lg-tv"},
            {"name": "speaker", "kind": "my_house.radio"},
            {"name": "fire tv", "kind": "my_house.firetv", "address": "192.168.1.60"},
        ],
    )
    assert "no AC profile" in built.unavailable["AC"] and "ac_onboard" in built.unavailable["AC"]
    assert "codes must be one of" in built.unavailable["TV"]
    assert "no kind 'radio'" in built.unavailable["speaker"]
    assert list(built.tools.devices) == ["fire tv"]


def test_a_device_keeps_the_name_people_call_it_and_is_found_whatever_the_case(tmp_path):
    with_profile(tmp_path)
    built, _ = house(tmp_path, [{"name": "AC", "kind": "my_house.ac"}])
    assert built.tools.devices["ac"].name == "AC" and built.tools.devices["ac"].status().startswith("AC: off")


def test_without_the_blasters_key_or_settings_the_ir_devices_say_so(tmp_path):
    with_profile(tmp_path)
    devices = [{"name": "AC", "kind": "my_house.ac"}]
    built, _ = house(tmp_path, devices, secrets={})
    assert "no TUYA_LOCAL_KEY" in built.unavailable["AC"]
    built, _ = house(tmp_path, devices, blaster=None)
    assert "set blaster" in built.unavailable["AC"]
    built, _ = house(tmp_path, devices, blaster={"id": "x", "address": "y", "version": "9"})
    assert "version must be one of" in built.unavailable["AC"]


@pytest.mark.parametrize(
    "spec, says",
    [
        ({"name": "AC", "kind": "my_house.ac", "colour": "white"}, "no setting colour"),
        ({"name": "AC", "kind": "my_house.ac", "state": "binary_sensor.ac_light"}, "through Home Assistant"),
    ],
)
def test_a_setting_a_kind_doesnt_have_is_said(tmp_path, spec, says):
    with_profile(tmp_path)
    built, _ = house(tmp_path, [spec])
    assert says in built.unavailable["AC"]


def test_a_setting_the_plugin_doesnt_have_costs_its_devices(tmp_path):
    built, _ = house(tmp_path, [{"name": "fire tv", "kind": "my_house.firetv", "address": "1"}], colour="white")
    assert "didn't load" in built.unavailable["fire tv"] and "colour" in built.unavailable["fire tv"]


def test_a_blaster_the_router_moved_is_found_by_its_id_and_remembered(tmp_path, monkeypatch):
    seen = Seen(tmp_path / "found.json")
    monkeypatch.setattr(blaster_module, "reachable", lambda host, port, timeout_s: host == "192.168.1.77")
    monkeypatch.setattr(blaster_module, "find_by_id", lambda device_id: "192.168.1.77" if device_id == "bf01" else None)
    b = blaster_module.Blaster("bf01", "192.168.1.50", "key", "3.5", 1, seen)
    b._locate()
    assert b._address == "192.168.1.77" and seen.get("tuya:bf01") == {"address": "192.168.1.77"}
    assert blaster_module.Blaster("bf01", "192.168.1.50", "key", "3.5", 1, seen)._address == "192.168.1.77"


def test_a_blaster_thats_nowhere_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(blaster_module, "reachable", lambda host, port, timeout_s: False)
    monkeypatch.setattr(blaster_module, "find_by_id", lambda device_id: None)
    with pytest.raises(HomeError, match="isn't announcing itself elsewhere"):
        blaster_module.Blaster("bf01", "192.168.1.50", "key", seen=Seen(tmp_path / "found.json"))._locate()
