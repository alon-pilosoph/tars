"""The TV, soundbar and input as one: on when something needs the picture, the soundbar pressed only when the TV was
certainly off, and nothing said to work that the TV doesn't say worked."""

import itertools
import threading
import types

import httpx
import pytest

from examples.my_house.direct_play import Catalog, FireTv
from examples.my_house.ir import CODE_SETS, Remote
from examples.my_house.screen import Screen
from voice_assistant.home import HomeError, Request


class House:
    """The TV and stick, as they answer: `tv` is on, standby, or None for not answering; IR changes it."""

    def __init__(self, tv="standby", stick=False, takes_ir=True):
        self.tv, self.stick, self.takes_ir, self.sent = tv, stick, takes_ir, []
        self.box = types.SimpleNamespace(
            awake=lambda: self.stick, wake=lambda: self._stick(True), sleep=lambda: self._stick(False)
        )
        self.remote = Remote("TV", CODE_SETS["samsung-tv"], lambda pulses: None)
        self.bar = Remote("soundbar", CODE_SETS["polk-re9641"], lambda pulses: None)
        self.remote.send = self._tv_button
        self.bar.send = lambda pressed: self.sent.append(f"bar {pressed}")
        ticks = itertools.count(step=3.0)
        self.screen = Screen(
            self.box,
            lambda: self.remote,
            lambda: self.bar,
            "10.0.0.9",
            "hdmi4",
            self._get,
            lambda s: None,
            lambda: next(ticks),
        )

    def _stick(self, awake):
        self.stick = awake

    def _tv_button(self, pressed):
        self.sent.append(f"tv {pressed}")
        if self.takes_ir and pressed in ("on", "off"):
            self.tv = "on" if pressed == "on" else "standby"

    def _get(self, url, timeout):
        if self.tv is None:
            raise httpx.ConnectError("no answer")
        return httpx.Response(200, json={"device": {"PowerState": self.tv}})


def test_a_tv_thats_off_comes_on_with_the_soundbar_and_its_input():
    house = House()
    assert house.screen.on() is True
    assert house.sent == ["tv on", "bar power", "tv hdmi4"] and house.stick


def test_a_tv_fully_off_answers_nothing_and_counts_as_off_with_the_stick_asleep():
    house = House(tv=None)
    house.screen.on()
    assert "bar power" in house.sent


def test_a_tv_already_on_only_gets_its_input_and_the_soundbar_is_left_alone():
    house = House(tv="on", stick=True)
    assert house.screen.on() is False
    assert house.sent == ["tv hdmi4"]


def test_a_tv_not_answering_with_the_stick_awake_may_be_on_so_the_soundbar_is_left_alone():
    house = House(tv=None, stick=True)
    house.screen.on()
    assert house.sent == ["tv on", "tv hdmi4"]


def test_a_tv_that_never_comes_on_is_said_and_the_soundbar_stays_as_it_was():
    house = House(takes_ir=False)
    with pytest.raises(HomeError, match="didn't come on"):
        house.screen.on()
    assert house.sent == ["tv on", "tv on", "tv on"]


def test_ir_that_misses_is_sent_again():
    house = House(takes_ir=False)
    send = house._tv_button

    def second_time_lucky(pressed):
        send(pressed)
        house.takes_ir = True

    house.remote.send = second_time_lucky
    assert house.screen.on() is True
    assert house.sent == ["tv on", "tv on", "bar power", "tv hdmi4"]


def test_off_takes_the_soundbar_and_stick_with_it():
    house = House(tv="on", stick=True)
    assert house.screen.off() is True
    assert house.sent == ["tv off", "bar power"] and not house.stick


def test_off_when_its_off_presses_nothing():
    house = House(stick=False)
    assert house.screen.off() is False
    assert house.sent == []


def test_someone_stopping_it_ends_the_wait():
    house = House(takes_ir=False)
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(HomeError):
        house.screen.on(stopped)
    assert house.sent == ["tv on"]


def fire_tv(house: House, opened: list) -> FireTv:
    box = types.SimpleNamespace(
        awake=house.box.awake, wake=house.box.wake, sleep=house.box.sleep, app=lambda: "", find_app=lambda name: None
    )
    device = FireTv("fire tv", box, None, Catalog(), sleep=lambda s: None, announce=opened.append, screen=house.screen)
    return device


def request() -> Request:
    return Request(speaker="alon")


def test_turning_the_tv_on_says_what_the_tv_says():
    house = House()
    device = fire_tv(house, [])
    assert device.power(True) == "done: the TV is on (it says so itself)"
    assert device.power(True) == "done: the TV was already on"


def test_a_tv_that_wont_come_on_isnt_said_to_be_on():
    device = fire_tv(House(takes_ir=False), [])
    assert device.power(True) == "not confirmed: the TV didn't come on"


def test_without_a_screen_power_is_left_to_the_tvs_own_codes():
    house = House()
    device = FireTv("fire tv", house.box, None, Catalog(), sleep=lambda s: None)
    assert device.power(True) is None


def test_the_tv_remotes_own_on_goes_through_the_screen():
    said = []
    remote = Remote("TV", CODE_SETS["samsung-tv"], said.append, power_by=lambda on: f"by the screen: {on}")
    assert remote.call("on", {}) == "by the screen: True"
    assert said == []


def test_the_news_question_doesnt_turn_anything_on():
    house = House()
    device = fire_tv(house, [])
    assert "Channel 11 or Channel 12" in device.call("play", {"title": "the news"}, request())
    assert house.sent == []


def test_a_channel_fails_out_loud_when_the_tv_wont_come_on():
    said = []
    device = fire_tv(House(takes_ir=False), said)
    device.call("play", {"title": "channel 12"}, request())
    device.tuner._thread.join(5)
    assert said == ["I couldn't put Channel 12 on: the TV didn't come on."]
