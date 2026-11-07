"""The TV, its soundbar and the Fire TV's input as one thing, for whatever needs the picture. Waking the Fire TV doesn't
turn this TV on, so TARS does what the Fire TV's own remote does: the TV by IR, its input by IR, and the soundbar's
power button. The TV says whether it's on, over the home network, with no pairing; the soundbar can't say anything,
and its one button switches it either way, so it's pressed only when the TV was certainly off, and goes off with it."""

import threading
import time
from collections.abc import Callable

import httpx

from voice_assistant.androidtv import POLL_S, AndroidTV
from voice_assistant.home import HomeError

from .ir import Remote

STATE_URL = "http://{address}:8001/api/v2/"  # Samsung's: answers on or standby, and nothing at all once it's fully off
STATE_TIMEOUT_S = 1.5
ON, STANDBY = "on", "standby"
POWER = "power"  # the soundbar's button: its code set has no on or off, so nothing names it as the power one
TV_WAIT_S = 25.0  # the TV coming on
SETTLE_S = 3.0  # a TV that's just on taking its input
OFF_WAIT_S = 8.0  # the TV saying it's gone off


class Screen:
    def __init__(
        self,
        box: AndroidTV,
        tv: Callable[[], Remote | None],
        soundbar: Callable[[], Remote | None],
        address: str,
        source: str,
        get: Callable[..., httpx.Response] = httpx.get,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        """`tv()`, `soundbar()`: those devices, read when used, since they may be made after this. `address`: the
        TV's. `source`: its button for the Fire TV's input, like hdmi4."""
        self.box, self._tv, self._soundbar, self.source = box, tv, soundbar, source
        self._url, self._get, self._sleep, self._clock = STATE_URL.format(address=address), get, sleep, clock

    def state(self) -> str:
        """What the TV says it is; empty if it doesn't answer."""
        try:
            return str(self._get(self._url, timeout=STATE_TIMEOUT_S).json()["device"]["PowerState"])
        except httpx.HTTPError, ValueError, KeyError, TypeError:
            return ""

    def on(self, stopped: threading.Event | None = None) -> bool:
        """The TV on, showing the Fire TV, with the soundbar on if the TV was off. Whether it turned the TV on. A TV
        that doesn't answer with the Fire TV awake may be on and only unreachable, so the soundbar is left alone."""
        tv, was = self._need_tv(), self.state()
        came_on = was != ON
        if came_on:
            certainly_off = was == STANDBY or not self.box.awake()
            tv.send(tv.codes.on)
            if not self._until(lambda: self.state() == ON, TV_WAIT_S, stopped):
                raise HomeError("the TV didn't come on")
            if certainly_off and (soundbar := self._soundbar()):
                soundbar.send(POWER)
        if not self.box.awake():
            self.box.wake()
        if came_on:
            self._sleep(SETTLE_S)
        tv.send(self.source)
        return came_on

    def off(self) -> bool:
        """The TV, the soundbar and the Fire TV off. Whether the TV was on, to turn off."""
        tv, was_on = self._need_tv(), self.state() == ON
        if was_on:
            tv.send(tv.codes.off)
            if soundbar := self._soundbar():
                soundbar.send(POWER)
            if not self._until(lambda: self.state() != ON, OFF_WAIT_S):
                raise HomeError("the TV says it's still on")
        if self.box.awake():
            self.box.sleep()
        return was_on

    def _need_tv(self) -> Remote:
        tv = self._tv()
        if tv is None:
            raise HomeError("there's no TV in config.toml to turn on")
        return tv

    def _until(self, happened: Callable[[], bool], seconds: float, stopped: threading.Event | None = None) -> bool:
        deadline = self._clock() + seconds
        while not (stopped and stopped.is_set()):
            if happened():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(POLL_S)
        return False
