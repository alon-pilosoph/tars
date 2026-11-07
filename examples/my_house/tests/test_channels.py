"""Live channels on the Fire TV: each step waits for what the app shows, it's on only once the box says the app makes
sound, and "the news" alone asks which."""

import itertools
import threading

import pytest

from examples.my_house import channels
from examples.my_house.channels import (
    ASK_WHICH,
    CATCH_UP,
    GUEST,
    HOME,
    KAN,
    KESHET,
    LIVE,
    LIVE_ROW,
    LIVE_TILE,
    SUBSCRIBERS,
    WATCH,
)
from examples.my_house.direct_play import Catalog, FireTv
from voice_assistant.androidtv import AndroidTV
from voice_assistant.home import HomeError

UIDS = {KAN: 10267, KESHET: 10121}
WELCOME = f"[focused] login @[1,1]\n{GUEST} @[2,2]"
GUEST_FOCUSED = f"login @[1,1]\n[focused] {GUEST} @[2,2]"
APP_HOME = f"[focused] {WATCH} @[1,1]\n{HOME} @[2,2]\n{LIVE} @[3,3]"
MENU = f"[focused] {HOME} @[2,2]\n{LIVE} @[3,3]"
MENU_LIVE = f"{HOME} @[2,2]\n[focused] {LIVE} @[3,3]"
LIVE_PAGE = f"[focused] {WATCH} @[1,1]\n{LIVE_ROW} @[4,4]\n{LIVE} @[3,3]"
TILE_FOCUSED = f"{CATCH_UP} @[1,1]\n{LIVE_TILE}"
BANNER_FOCUSED = f"{CATCH_UP} @[1,1]\n[focused] {WATCH} @[1616,497][1792,553]"
# Kan Box, each press taking it to the next screen; a press from anywhere else does nothing.
KAN_SCREENS = {
    (WELCOME, "left"): GUEST_FOCUSED,
    (GUEST_FOCUSED, "select"): APP_HOME,
    (APP_HOME, "back"): MENU,
    (MENU, "down"): MENU_LIVE,
    (MENU_LIVE, "select"): LIVE_PAGE,
    (LIVE_PAGE, "select"): "",
}


class Box(AndroidTV):
    """A Fire TV with both apps, whose screens follow the remote. `sounds`: the app plays once on its last screen."""

    def __init__(self, first=WELCOME, sounds=True, home_12=TILE_FOCUSED, preview=False):
        super().__init__("10.0.0.8", key_path=None)
        self.first, self.sounds, self.home_12, self.preview = first, sounds, home_12, preview
        self.front, self.screen, self.presses, self.stopped_apps = "", "", [], []
        self.on_press = lambda key: None
        self.after_select = ""  # what 12+ shows once its live tile is chosen
        self.slow_reads = 0  # looks at the screen that run out of time

    def awake(self):
        return True

    def app(self):
        return self.front

    def ui(self):
        if self.slow_reads:
            self.slow_reads -= 1
            raise HomeError("the TV box stopped answering (TcpTimeoutException)")
        return self.screen

    def force_stop(self, package):
        self.stopped_apps.append(package)

    def open(self, package):
        self.front, self.screen = package, self.first if package == KAN else self.home_12

    def press(self, key, times=1):
        self.presses.append(key)
        self.on_press(key)
        if self.front == KESHET:
            if key == "select":
                self.selected = self.screen
                self.screen = self.after_select if LIVE_TILE in self.screen else BANNER_FOCUSED + " playing a promo"
            elif key == "down" and self.screen == BANNER_FOCUSED:
                self.screen = TILE_FOCUSED
        else:
            self.screen = KAN_SCREENS.get((self.screen, key), self.screen)

    def shell(self, command, timeout_s=10.0, repeatable=False):
        if command.startswith("dumpsys package"):
            return f"    userId={UIDS[command.split()[2]]}"
        if command.startswith("dumpsys audio") and self.sounds and self.front and (self.preview or not self.screen):
            uid = UIDS[self.front]
            return f"  AudioPlaybackConfiguration piid:1 u/pid:{uid}/9 state:started attr: usage=USAGE_MEDIA content=X"
        return ""


def tv(box) -> tuple[FireTv, list[str]]:
    said = []
    ticks = itertools.count(step=7.0)  # the clock moves on with every look, so a screen that never comes gives up
    device = FireTv("fire tv", box, None, Catalog(), sleep=lambda s: None, announce=said.append)
    device.tuner._clock = lambda: next(ticks)
    return device, said


def put_on(device: FireTv, title: str) -> str:
    answer = device.play(title, "")
    device.tuner._thread.join(5)
    return answer


@pytest.mark.parametrize("title", ["channel 12", "Channel 12", "keshet", "ערוץ 12", "12+"])
def test_channel_12_opens_afresh_and_takes_one_press(title):
    box = Box()
    device, said = tv(box)
    assert put_on(device, title).startswith("started")
    assert box.stopped_apps == [KESHET] and box.presses == ["select"]
    assert said == ["Channel 12 is on."]


@pytest.mark.parametrize("title", ["channel 11", "kan", "כאן 11"])
def test_channel_11_goes_in_as_a_guest_and_through_the_menu(title):
    box = Box()
    device, said = tv(box)
    put_on(device, title)
    assert box.stopped_apps == [KAN]
    assert box.presses == ["left", "select", "back", "down", "select", "select"]
    assert said == ["Channel 11 is on."]


def test_channel_11_with_no_welcome_goes_straight_to_the_menu():
    box = Box(first=APP_HOME)
    device, said = tv(box)
    put_on(device, "channel 11")
    assert box.presses == ["back", "down", "select", "select"]
    assert said == ["Channel 11 is on."]


def test_a_screen_that_never_comes_stops_there_and_says_so():
    box = Box(first="something new @[1,1]")
    device, said = tv(box)
    put_on(device, "channel 11")
    assert box.presses == []
    assert said == ["I couldn't put Channel 11 on: the app's first page never came up."]


def test_it_isnt_on_until_the_box_says_the_app_makes_sound():
    box = Box(sounds=False)
    device, said = tv(box)
    put_on(device, "channel 12")
    assert said == ["I couldn't put Channel 12 on: nothing started playing."]


def test_behind_a_vpn_12_plus_says_subscribers_only_and_tars_says_why():
    box = Box(sounds=False)
    box.after_select = f"{SUBSCRIBERS} @[1,1]"
    device, said = tv(box)
    put_on(device, "channel 12")
    assert said == ["I couldn't put Channel 12 on: it says it's for subscribers, which it does when the VPN is on."]


def test_a_look_at_the_screen_that_runs_out_of_time_only_means_not_yet():
    box = Box()
    box.slow_reads = 2  # as an app starting on a Fire TV Stick can make it
    device, said = tv(box)
    put_on(device, "channel 12")
    assert said == ["Channel 12 is on."]


def test_with_the_banner_promoting_a_show_it_goes_down_to_the_live_tile_first():
    box = Box(home_12=BANNER_FOCUSED)
    device, said = tv(box)
    put_on(device, "channel 12")
    assert box.presses == ["down", "select"] and LIVE_TILE in box.selected
    assert said == ["Channel 12 is on."]


def test_a_preview_making_sound_on_the_home_page_isnt_the_channel():
    box = Box(sounds=True, preview=True)
    box.after_select = TILE_FOCUSED  # select did nothing: still the home page, the preview still sounding
    device, said = tv(box)
    put_on(device, "channel 12")
    assert said == ["I couldn't put Channel 12 on: nothing started playing."]


def test_someone_asking_for_something_else_takes_over_quietly():
    box = Box()
    device, said = tv(box)
    pressed, go_on = threading.Event(), threading.Event()
    box.on_press = lambda key: (pressed.set(), go_on.wait(5))
    device.play("channel 11", "")
    assert pressed.wait(5)
    threading.Timer(0.05, go_on.set).start()
    device.stop()
    assert said == [] and box.presses == ["left"]


@pytest.mark.parametrize("title", ["news", "The news", "החדשות"])
def test_the_news_alone_asks_which_and_touches_nothing(title):
    box = Box()
    device, said = tv(box)
    assert device.play(title, "") == ASK_WHICH
    assert box.presses == [] and box.stopped_apps == [] and said == []


def test_the_news_on_an_app_they_named_isnt_a_channel():
    assert not channels.only_news("the news", "youtube")
    assert channels.channel("the news") is None


def test_only_what_takes_time_says_one_moment_first():
    device, _ = tv(Box())
    assert not device.slow("play", {"title": "channel 12"})
    assert not device.slow("play", {"title": "the news"})
    assert device.slow("play", {"title": "Ratatouille (2007)"})


def test_the_model_is_told_how_to_ask_for_them():
    device, _ = tv(Box())
    assert '"channel 11"' in device.rules() and '"channel 12"' in device.rules() and "news" in device.rules()
