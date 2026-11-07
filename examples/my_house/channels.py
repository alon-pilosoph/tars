"""Live TV on the Fire TV, in fixed steps and with no model working the screen: Channel 12 in Keshet's 12+, Channel 11
in Kan Box. Neither app takes a link to its live channel, so it's closed and opened again, to start from a screen
that's known, and worked by its remote. Each step waits for what the app shows, and it's on only once the box says
the app is making sound: neither app has a media session to ask. It takes up to a minute, so it runs in the
background, and TARS says how it went.

"The news" alone is never a channel: asked to play it, the device has TARS ask which."""

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from voice_assistant.androidtv import POLL_S, STOP_WAIT_S, AndroidTV, open_app
from voice_assistant.home import HomeError

KESHET, KAN = "com.keshet.mako.VODAndroidTV", "com.applicaster.il.ch1"
SCREEN_WAIT_S = 25.0  # a screen of the app coming up; on a Fire TV Stick the first takes 17 s
FOCUS_WAIT_S = 6.0  # the remote's press showing
PLAY_WAIT_S = 45.0  # the live channel starting, an ad before it included
SETTLE_S = 2.0  # a screen that's up taking its first press
# What the apps show, as they say it to accessibility tools.
CATCH_UP = "שודר לאחרונה - Catch Up"  # 12+'s home page, whatever its banner is promoting, with sound of its own
LIVE_TILE = "[focused] (no text) @[1292,685][1644,840]"  # its 12 LIVE tile, which says nothing: only where it is
SUBSCRIBERS = "התוכן למנויים בלבד"  # what 12+ shows behind a VPN
GUEST = "כניסה במצב אורח"  # Kan Box's welcome, at each start without an account
HOME, LIVE = "עמוד הבית", "שידור חי"  # its side menu, which back opens
LIVE_ROW, WATCH = "טלוויזיה בשידור חי", "לצפייה"  # its live page, on Channel 11's current show
ASK_WHICH = "ask them: Channel 11 or Channel 12? Change nothing until they say, then play that channel"
RULES = (
    'For live TV use play with the title "channel 11" (Kan) or "channel 12" (Keshet), on this device; an ad may run '
    'first. "Put the news on", with no channel said, is live TV too: ask which of the two, and play it once they say. '
    "A YouTube link they ask to play or put on: play with the link as it is as the title, and the app youtube."
)


class Stuck(Exception):
    """A step didn't happen: why, to be said."""


@dataclass(frozen=True)
class Channel:
    name: str
    package: str
    steps: Callable[[Tuner, threading.Event], None]
    menu: str  # what shows until the channel does: a menu can make sound of its own


class Tuner:
    """Puts a channel on, from a thread of its own. `announce`: how TARS says how it went."""

    def __init__(
        self,
        box: AndroidTV,
        announce: Callable[[str], None],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        ready: Callable[[threading.Event], object] = lambda stopped: None,
    ):
        """`ready(stopped)`: the picture on, first; raises HomeError if it can't be."""
        self.box, self._announce, self._sleep, self._clock, self._ready = box, announce, sleep, clock, ready
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, channel: Channel) -> str:
        self.stop()
        self._stopped = stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(channel, stopped), daemon=True, name="tv-channel")
        self._thread.start()
        return "started: TARS will say when it's on, so say only that you're on it"

    def stop(self) -> None:
        """As the TV agent's: a person asking for something else takes over."""
        self._stopped.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_WAIT_S)

    def _run(self, channel: Channel, stopped: threading.Event) -> None:
        try:
            self._tune(channel, stopped)
            say = f"{channel.name} is on."
        except (Stuck, HomeError) as e:
            say = f"I couldn't put {channel.name} on: {e}."
        except Exception as e:  # noqa: BLE001 - said, never raised: nobody is waiting on this thread
            print(f"(tv: {channel.name} failed: {e!r})")
            say = f"I couldn't put {channel.name} on."
        if stopped.is_set():
            print(f"(tv: {channel.name} stopped)")
            return
        print(f"(tv: {say})")
        self._announce(say)

    def _tune(self, channel: Channel, stopped: threading.Event) -> None:
        self._ready(stopped)
        self.box.force_stop(channel.package)
        opened, _, _ = open_app(self.box, channel.package, self._sleep, stopped)
        if not opened:
            raise Stuck("its app didn't open")
        channel.steps(self, stopped)
        walled = []

        def over() -> bool:
            ui = self.box.ui()
            if self.sounding(channel.package) and channel.menu not in ui:
                return True
            walled.extend([True] * (SUBSCRIBERS in ui))
            return bool(walled)

        if not self._until(over, PLAY_WAIT_S, stopped):
            raise Stuck("nothing started playing")
        if walled:
            raise Stuck("it says it's for subscribers, which it does when the VPN is on")

    def _until(self, happened: Callable[[], bool], seconds: float, stopped: threading.Event) -> bool:
        """By the clock, not by looks: reading the screen takes seconds of its own on a Fire TV Stick, and a read that
        runs out of time, as one can while an app starts, only means it hasn't been seen yet."""
        deadline = self._clock() + seconds
        while not stopped.is_set():
            try:
                if happened():
                    return True
            except HomeError as e:
                print(f"(tv: couldn't look: {e})")
            if self._clock() >= deadline:
                return False
            self._sleep(POLL_S)
        return False

    def _see(self, words: str, what: str, stopped: threading.Event, seconds: float = SCREEN_WAIT_S) -> None:
        if not self._until(lambda: words in self.box.ui(), seconds, stopped):
            raise Stuck(f"{what} never came up")

    def _focus(self, words: str, what: str, stopped: threading.Event) -> None:
        self._see(f"[focused] {words} @", what, stopped, FOCUS_WAIT_S)

    def sounding(self, package: str) -> bool:
        """Whether the app is playing sound, as the box's audio service says."""
        found = re.search(
            r"userId=(\d+)", self.box.shell(f"dumpsys package {package} | grep -m1 userId=", repeatable=True)
        )
        players = self.box.shell("dumpsys audio | grep state:started", repeatable=True)
        return bool(found and re.search(rf"u/pid:{found[1]}/\d+ state:started .*USAGE_MEDIA", players))

    def twelve(self, stopped: threading.Event) -> None:
        self._see(CATCH_UP, "the app's home page", stopped)
        self._sleep(SETTLE_S)
        if LIVE_TILE not in self.box.ui():  # the banner's own button has it: the tile is the row below
            self.box.press("down")
            self._see(LIVE_TILE, "the live tile", stopped, FOCUS_WAIT_S)
        self.box.press("select")

    def eleven(self, stopped: threading.Event) -> None:
        if not self._until(lambda: GUEST in (ui := self.box.ui()) or LIVE in ui, SCREEN_WAIT_S, stopped):
            raise Stuck("the app's first page never came up")
        if GUEST in self.box.ui():
            self.box.press("left")
            self._focus(GUEST, "the guest button", stopped)
            self.box.press("select")
            if not self._until(lambda: GUEST not in (ui := self.box.ui()) and LIVE in ui, SCREEN_WAIT_S, stopped):
                raise Stuck("the app's home page never came up")
        self._sleep(SETTLE_S)
        self.box.press("back")
        self._focus(HOME, "the menu", stopped)
        self.box.press("down")
        self._focus(LIVE, "the menu's live page", stopped)
        self.box.press("select")
        self._see(LIVE_ROW, "the live page", stopped)
        self._focus(WATCH, "the watch button", stopped)
        self.box.press("select")


CHANNEL_11 = Channel("Channel 11", KAN, Tuner.eleven, LIVE_ROW)
CHANNEL_12 = Channel("Channel 12", KESHET, Tuner.twelve, CATCH_UP)
NAMES = {
    **dict.fromkeys(("channel 11", "kan 11", "kan", "ערוץ 11", "כאן 11", "כאן"), CHANNEL_11),
    **dict.fromkeys(("channel 12", "keshet 12", "keshet", "12+", "ערוץ 12", "קשת 12", "קשת"), CHANNEL_12),
}
NEWS = ("news", "the news", "חדשות", "החדשות")


def channel(title: str) -> Channel | None:
    return NAMES.get(title.strip().lower())


def only_news(title: str, app: str) -> bool:
    """The news with no channel said: not on an app they named, like YouTube."""
    return title.strip().lower() in NEWS and not app
