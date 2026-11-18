"""The purifier bridge (bridge.py), with a pretend purifier for bleak's client: TARS's Atom client works the purifier
through it as through an Atom, over a connection it held ready, and a new one after each request."""

import asyncio
import threading

import pytest

from examples.my_house import purifier as module
from examples.my_house.bridge import Bridge
from examples.my_house.purifier import FANS, LIVE, Atom, Purifier
from voice_assistant.home import SETTINGS, HomeError, Request

from .test_purifier import Pretend


class PretendBleak:
    """bleak's BleakClient, to a pretend purifier: statuses every 50 ms once subscribed; `made` counts connections."""

    purifier: Pretend
    made = 0
    near = True

    def __init__(self, address, timeout):
        self.is_connected = False
        self._reporting = None

    async def connect(self):
        if not PretendBleak.near:
            raise TimeoutError("no answer")
        PretendBleak.made += 1
        self.is_connected = True

    async def start_notify(self, uuid, callback):
        assert uuid == LIVE

        async def report():
            while self.is_connected:
                callback(None, bytearray(_frame(PretendBleak.purifier)))
                await asyncio.sleep(0.05)

        self._reporting = asyncio.ensure_future(report())

    async def write_gatt_char(self, uuid, data, response):
        if data[2] not in (0xAF, 0xA2):  # its hello and asking for a status change nothing
            PretendBleak.purifier.talk([bytes(data)], lambda _: True)

    async def disconnect(self):
        self.is_connected = False


def _frame(purifier: Pretend) -> bytes:
    s = purifier.status()
    body = [0x0D, 0xA0, 0x21, FANS.index(s.fan), s.timer_hours, s.flags, 0, 20, 0, 240, 0, 0x40, 0x06, 0]
    return bytes([0xAA, *body, sum(body) & 0xFF, 0xEE])


@pytest.fixture
def bridge(monkeypatch):
    """A bridge on loopback, holding a pretend purifier ready; the Atom client pointed at it."""
    PretendBleak.purifier, PretendBleak.made, PretendBleak.near = Pretend(), 0, True
    loop = asyncio.new_event_loop()
    ready, made = threading.Event(), {}

    def run():
        asyncio.set_event_loop(loop)
        made["bridge"] = b = Bridge("48:87:2D:1F:FC:9B", "the-key", client_class=PretendBleak)
        made["server"] = loop.run_until_complete(asyncio.start_server(b.serve, "127.0.0.1", 0))
        made["keeping"] = loop.create_task(b.keep())
        ready.set()
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    ready.wait()
    monkeypatch.setattr(module, "ATOM_PORT", made["server"].sockets[0].getsockname()[1])
    monkeypatch.setattr(module, "SETTLE_S", 1.0)
    module._found.clear()
    yield made["bridge"]
    loop.call_soon_threadsafe(loop.stop)


def until_linked(bridge: Bridge) -> None:
    import time

    for _ in range(100):
        if bridge.linked() and not bridge.used:
            return
        time.sleep(0.05)
    raise AssertionError("never linked")


def call(device, action, **settings) -> str:
    return device.call(action, dict.fromkeys(SETTINGS) | settings, Request())


def test_tars_works_the_purifier_through_the_bridge_over_a_connection_held_ready(bridge):
    until_linked(bridge)
    p = Purifier("living room air purifier", Atom("48:87:2D:1F:FC:9B", "127.0.0.1", "the-key"), sleep=lambda _: None)
    assert p.status().startswith("living room air purifier: off, fan medium")
    until_linked(bridge)
    assert call(p, "on") == "done: living room air purifier reports on"
    until_linked(bridge)
    assert call(p, "off") == "done: living room air purifier reports off"
    assert PretendBleak.made >= 3  # each request had its own connection, made ready before it


def test_a_key_that_isnt_the_bridges_is_refused(bridge):
    p = Purifier("living room air purifier", Atom("48:87:2D:1F:FC:9B", "127.0.0.1", "not-it"), sleep=lambda _: None)
    with pytest.raises(HomeError, match="refused the key"):
        p.status()


def test_out_of_reach_it_says_so(bridge):
    until_linked(bridge)
    PretendBleak.near = False
    bridge.client.is_connected = False  # the ready connection goes, and none can be made
    link = Atom("48:87:2D:1F:FC:9B", "127.0.0.1", "the-key")
    with pytest.raises(HomeError, match="couldn't connect to it"):
        Purifier("living room air purifier", link, sleep=lambda _: None).status()
