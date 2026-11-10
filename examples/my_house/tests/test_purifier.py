"""The air purifier, against a pretend one: its status read, and a change "done" only when its status shows it."""

import pytest

from examples.my_house.purifier import FANS, Purifier, Status, frame, parse
from voice_assistant.home import SETTINGS, HomeError, Request

ON_FRAME = bytes.fromhex("aa0da021010089018e00f610e0068053ee")  # read from a real one, as are the next two
OFF_FRAME = bytes.fromhex("aa0da021010088017a00f210e006803aee")
HELD_FRAME = bytes.fromhex("aa0da02002008a000000fa10e00680c9ee")


def test_commands_are_framed_as_the_app_frames_them():
    assert frame(0x01, 0, 1).hex() == "aa0301000105ee"
    assert frame(0xAF, 0, 1).hex() == "aa03af0001b3ee"
    assert frame(0xA2, 0, 3).hex() == "aa03a20003a8ee"


def test_a_real_status_is_read_and_anything_else_is_not():
    on = parse(ON_FRAME)
    assert on.on and not on.held and on.fan == "medium" and not on.uv and on.timer_hours == 0
    assert on.pm25 == pytest.approx(14.2) and on.temperature == pytest.approx(24.6)
    assert on.filter_percent == pytest.approx(40.74, abs=0.01)
    assert not parse(OFF_FRAME).on
    assert parse(HELD_FRAME).held
    assert parse(ON_FRAME[:-3] + b"\x00\x00\xee") is None  # a bad checksum
    assert parse(b"\xaa\x03") is None


class Pretend:
    """A purifier that takes commands, or doesn't: `held`; `fails` connections drop first."""

    def __init__(self, held=False, fails=0, ignores=False):
        self.state = {"on": False, "fan": 1, "uv": False, "auto": False, "night": False, "timer": 0}
        self.held, self.fails, self.ignores = held, fails, ignores
        self.sent: list[bytes] = []

    def status(self) -> Status:
        s = self.state
        flags = s["on"] | s["uv"] << 1 | bool(s["timer"]) << 2 | s["auto"] << 5 | s["night"] << 6
        return Status(self.held, FANS[s["fan"]], s["timer"], flags, 2.0, 24.0, 40.0)

    def talk(self, commands, done):
        if self.fails:
            self.fails -= 1
            raise OSError("dropped")
        if self.held:
            return self.status()
        self.sent += commands
        for c in [] if self.ignores else commands:
            key = {1: "on", 2: "fan", 3: "uv", 4: "timer", 5: "auto", 6: "night"}[c[2]]
            self.state[key] = c[4] if key in ("fan", "timer") else bool(c[4])
        return self.status()


def call(device, action, **settings) -> str:
    return device.call(action, dict.fromkeys(SETTINGS) | settings, Request())


def purifier(**kw) -> tuple[Purifier, Pretend]:
    link = Pretend(**kw)
    return Purifier("air purifier", link, sleep=lambda _: None), link


def test_on_off_and_its_status():
    p, link = purifier()
    assert p.status() == "air purifier: off, fan medium, PM2.5 2, 24 °C, filter 40%"
    assert call(p, "on") == "done: air purifier reports on"
    assert call(p, "off") == "done: air purifier reports off"
    assert link.sent == [frame(1, 0, 1), frame(1, 0, 0)]


def test_its_timer_is_two_four_or_eight_hours():
    p, link = purifier()
    assert call(p, "on", minutes=120) == "done: air purifier reports on for 2 hours"
    assert link.state["timer"] == 2
    assert call(p, "on", minutes=30).startswith("error: its timer is 2, 4, 8 hours")


def test_a_fixed_fan_leaves_auto_and_modes_are_one_at_a_time():
    p, link = purifier()
    link.state["auto"] = True
    assert call(p, "set", fan="turbo") == "done: air purifier reports fan turbo"
    assert not link.state["auto"] and link.state["fan"] == 3
    assert call(p, "set", mode="night") == "done: air purifier reports night mode"
    assert link.state["night"] and not link.state["auto"]
    assert call(p, "set", mode="normal") == "done: air purifier reports normal mode"
    assert not link.state["night"]


def test_what_it_doesn_t_have_changes_nothing():
    p, link = purifier()
    assert call(p, "set", fan="ludicrous").startswith("error: its fan is low")
    assert call(p, "set", mode="party").startswith("error: its mode is auto")
    assert call(p, "set").startswith("error: say a fan or a mode")
    assert call(p, "press", button="boost").startswith("error: its buttons")
    assert link.sent == []


def test_uv_is_a_press():
    p, link = purifier()
    assert call(p, "press", button="uv on") == "done: air purifier reports UV on"
    assert link.state["uv"]


def test_a_purifier_that_doesn_t_change_is_not_said_to_have():
    p, _ = purifier(ignores=True)
    assert call(p, "on").startswith("not confirmed: asked for on, and it reports off")


def test_held_after_a_long_unplug_it_says_to_use_the_panel_and_sends_nothing():
    p, link = purifier(held=True)
    said = call(p, "on")
    assert said.startswith("not confirmed") and "at its panel" in said and link.sent == []
    assert "at its panel" in p.status() and p.check()[0].status == "warn"


def test_a_dropped_connection_is_tried_again_then_said():
    p, _ = purifier(fails=2)
    assert call(p, "on") == "done: air purifier reports on"
    p, _ = purifier(fails=3)
    with pytest.raises(HomeError, match="didn't answer over Bluetooth"):
        call(p, "on")
    p, _ = purifier(fails=3)
    assert p.check()[0].status == "warn"


def test_its_watch_tells_the_maintainers_once_when_it_goes_quiet_and_once_when_its_back():
    from examples.my_house.purifier import GONE_AFTER_S, Watch

    now, on, told = [0.0], [True], []
    watch = Watch("living room air purifier", lambda seconds: on[0], told.append, clock=lambda: now[0])
    watch.look()
    on[0] = False  # its Bluetooth switched off at its panel
    now[0] = GONE_AFTER_S - 1
    watch.look()
    assert told == []  # not yet: a quarter of an hour unheard first
    now[0] = GONE_AFTER_S + 60
    watch.look()
    watch.look()
    assert len(told) == 1 and "living room air purifier's Bluetooth has gone quiet" in told[0]
    assert "press its Bluetooth button" in told[0]
    on[0] = True
    watch.look()
    assert told[-1] == "The living room air purifier is back on Bluetooth: I can work it again." and not watch.gone


def test_a_request_it_answers_counts_as_heard():
    from examples.my_house.purifier import GONE_AFTER_S, Watch

    now, told = [0.0], []
    watch = Watch("air purifier", lambda seconds: False, told.append, clock=lambda: now[0])
    link = Pretend()
    p = Purifier("air purifier", link, sleep=lambda _: None, answered=watch.answered)
    now[0] = GONE_AFTER_S - 10
    p.status()  # it answered: on, whatever the listening missed
    now[0] = GONE_AFTER_S + 30
    watch.look()
    assert told == []


def test_only_the_voice_assistants_house_watches_it(tmp_path, monkeypatch):
    pytest.importorskip("bleak")
    import examples.my_house as plugin
    from voice_assistant.home import Context

    started = []
    monkeypatch.setattr(plugin.Watch, "start", lambda self, *a: started.append(self.name) or self)
    spec = {"name": "air purifier", "address": "48:87:2D:1F:FC:9B"}
    chat_side = Context(tmp_path, lambda name: None)
    plugin.setup({}, chat_side)["purifier"](spec)
    assert started == []  # the Telegram service's house, or --check's: no second watch
    voice_side = Context(tmp_path, lambda name: None, tell=print, background=True)
    plugin.setup({}, voice_side)["purifier"](spec)
    assert started == ["air purifier"]
