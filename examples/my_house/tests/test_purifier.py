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
    assert on.filter_used == pytest.approx(40.74, abs=0.01)  # its hours-run counter: 1760 of 4320
    assert not parse(OFF_FRAME).on
    assert parse(HELD_FRAME).held
    assert parse(ON_FRAME[:-3] + b"\x00\x00\xee") is None  # a bad checksum
    assert parse(b"\xaa\x03") is None


class Pretend:
    """A purifier that takes commands, or doesn't: `held`; `fails` connections drop first."""

    def __init__(self, held=False, fails=0, ignores=False):
        self.state = {"on": False, "fan": 1, "uv": False, "auto": False, "night": False, "timer": 0, "buzzer": True}
        self.held, self.fails, self.ignores = held, fails, ignores
        self.sent: list[bytes] = []

    def status(self) -> Status:
        s = self.state
        flags = s["on"] | s["uv"] << 1 | bool(s["timer"]) << 2 | s["buzzer"] << 3 | s["auto"] << 5 | s["night"] << 6
        return Status(self.held, FANS[s["fan"]], s["timer"], flags, 2.0, 24.0, 40.0)

    def talk(self, commands, done):
        if self.fails:
            self.fails -= 1
            raise OSError("dropped")
        if self.held:
            return self.status()
        self.sent += commands
        for c in [] if self.ignores else commands:
            key = {1: "on", 2: "fan", 3: "uv", 4: "timer", 5: "auto", 6: "night", 8: "buzzer"}[c[2]]
            self.state[key] = c[4] if key in ("fan", "timer") else bool(c[4])
        return self.status()


def call(device, action, **settings) -> str:
    return device.call(action, dict.fromkeys(SETTINGS) | settings, Request())


def purifier(**kw) -> tuple[Purifier, Pretend]:
    link = Pretend(**kw)
    return Purifier("air purifier", link, sleep=lambda _: None), link


def test_on_off_and_its_status():
    p, link = purifier()
    assert p.status() == "air purifier: off, fan medium, PM2.5 2, 24 °C, filter 60% left"
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


def test_its_buzzer_is_a_press_and_said_only_when_off():
    p, link = purifier()
    assert call(p, "press", button="buzzer off") == "done: air purifier reports buzzer off"
    assert not link.state["buzzer"] and "buzzer off" in p.status()
    assert call(p, "press", button="buzzer on") == "done: air purifier reports buzzer on"
    assert "buzzer" not in p.status()


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
    assert len(told) == 1 and "living room air purifier has gone quiet on Bluetooth" in told[0]
    assert "16 minutes ago" in told[0] and "unplugged" in told[0] and "press its Bluetooth button" in told[0]
    on[0] = True
    watch.look()
    assert told[-1] == "The living room air purifier is back on Bluetooth: I can work it again." and not watch.gone


def test_a_restart_neither_forgets_when_it_was_last_heard_nor_tells_it_gone_again(tmp_path):
    from examples.my_house.purifier import GONE_AFTER_S, Watch

    memory, now, told = tmp_path / "watch.json", [1_000_000.0], []
    heard = [True]

    def watch() -> Watch:
        return Watch("air purifier", lambda s: heard[0], told.append, clock=lambda: now[0], memory=memory)

    watch().look()  # heard: kept in the file
    heard[0] = False  # unplugged
    now[0] += 9 * 3600  # TARS restarts nine hours later: still the morning's last heard
    w = watch()
    w.look()
    assert len(told) == 1 and "about 9 hours ago" in told[0]
    told.clear()
    now[0] += GONE_AFTER_S * 2  # and restarts again: already told
    watch().look()
    assert told == []
    heard[0] = True
    watch().look()
    assert told == ["The air purifier is back on Bluetooth: I can work it again."]


def test_never_heard_at_all_it_says_since_when_it_listened(tmp_path):
    from examples.my_house.purifier import GONE_AFTER_S, Watch

    now, told = [1_000_000.0], []
    w = Watch("air purifier", lambda s: False, told.append, clock=lambda: now[0], memory=tmp_path / "w.json")
    now[0] += GONE_AFTER_S + 60
    w.look()
    assert len(told) == 1 and "since I started listening for it" in told[0]


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


def test_when_this_machine_cant_listen_its_bluetooth_is_restarted_before_anyone_is_told():
    from examples.my_house.purifier import GONE_AFTER_S, Watch

    now, told, restarts = [0.0], [], []
    answers = iter([None, None, True])  # it can't listen twice; restarted, it hears the purifier
    watch = Watch(
        "air purifier", lambda s: next(answers), told.append, clock=lambda: now[0], restart=lambda: restarts.append(1)
    )
    now[0] = GONE_AFTER_S * 2
    watch.look()
    assert restarts == [] and told == []  # one failure: not yet
    watch.look()
    assert restarts == [1] and told == []  # restarted, and that fixed it: nothing to tell
    assert not watch.stuck


def test_a_restart_that_doesnt_help_is_told_once_and_never_as_the_purifier_gone():
    from examples.my_house.purifier import GONE_AFTER_S, Watch

    now, told, can_listen = [0.0], [], [False]
    watch = Watch(
        "air purifier",
        lambda s: True if can_listen[0] else None,
        told.append,
        clock=lambda: now[0],
        restart=lambda: True,
    )
    now[0] = GONE_AFTER_S * 3
    for _ in range(4):
        watch.look()
    assert len(told) == 1 and "My Bluetooth has stopped working" in told[0] and not watch.gone
    can_listen[0] = True
    watch.look()
    assert told[-1] == "My Bluetooth works again." and not watch.stuck


class PretendAtom:
    """firmware/atom-lite-bt as TARS meets it, on this machine's loopback, with a purifier beside it (Pretend): its
    key "the-key"; `near` False, it can't connect to the purifier; `heard_ms`, what "heard" says."""

    def __init__(self, purifier: Pretend, near=True, heard_ms=1000):
        import asyncio
        import threading

        self.purifier, self.near, self.heard_ms = purifier, near, heard_ms
        self.sessions, self.links = 0, 0
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.server = self.loop.run_until_complete(asyncio.start_server(self._serve, "127.0.0.1", 0))
            self.port = self.server.sockets[0].getsockname()[1]
            ready.set()
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait()

    def stop(self):
        def close():
            self.server.close()
            self.loop.stop()

        self.loop.call_soon_threadsafe(close)

    def _frame(self) -> bytes:
        s = self.purifier.status()
        body = [0x0D, 0xA0, 0x20 if s.held else 0x21, FANS.index(s.fan), s.timer_hours, s.flags, 0, 20, 0, 240, 0]
        body += [0x40, 0x06, 0]
        return bytes([0xAA, *body, sum(body) & 0xFF, 0xEE])

    async def _serve(self, reader, writer):
        import asyncio
        import hashlib
        import hmac

        self.sessions += 1
        writer.write(b"tars-bt 1 0123abcd\n")
        proof = hmac.new(b"the-key", b"0123abcd", hashlib.sha256).hexdigest()
        if (await reader.readline()).decode().strip() != f"auth {proof}":
            writer.write(b"refused\n")
            writer.close()
            return
        writer.write(b"ok\n")
        live = None

        async def report():
            while True:
                writer.write(b"live " + self._frame().hex().encode() + b"\n")
                await asyncio.sleep(0.05)

        while line := (await reader.readline()).decode().strip():
            if line == "link":
                if not self.near:
                    writer.write(b"failed it didn't answer\n")
                    continue
                self.links += 1
                writer.write(b"linked\n")  # its latest status follows at once, then each it reports
                live = asyncio.ensure_future(report())
            elif line == "unlink":
                if live:
                    live.cancel()
                writer.write(b"unlinked\n")
            elif line == "heard":
                writer.write(f"heard {self.heard_ms}\n".encode())
            elif line.startswith("send "):
                sent = bytes.fromhex(line[5:])
                if sent[2] not in (0xAF, 0xA2):  # its hello and asking for a status change nothing
                    self.purifier.talk([sent], lambda _: True)
                writer.write(b"sent\n")
        if live:
            live.cancel()
        writer.close()


@pytest.fixture
def atom(monkeypatch):
    """An Atom link to a PretendAtom, made with `atom(**kw)`."""
    from examples.my_house import purifier as module

    made = []

    def make(key="the-key", **kw):
        fake = PretendAtom(Pretend(), **kw)
        made.append(fake)
        monkeypatch.setattr(module, "ATOM_PORT", fake.port)
        monkeypatch.setattr(module, "SETTLE_S", 0.5)
        module._found.clear()
        return module.Atom("48:87:2D:1F:C1:5C", "127.0.0.1", key), fake

    yield make
    for fake in made:
        fake.stop()


def test_through_an_atom_it_works_the_purifier_as_from_this_machine(atom):
    link, fake = atom()
    p = Purifier("bedroom purifier", link, sleep=lambda _: None)
    assert p.status().startswith("bedroom purifier: off, fan medium")
    assert call(p, "on") == "done: bedroom purifier reports on"
    assert call(p, "set", fan="turbo") == "done: bedroom purifier reports fan turbo"
    assert fake.purifier.state["on"] and fake.purifier.state["fan"] == 3
    assert (fake.sessions, fake.links) == (3, 3)  # a connection a request, to the Atom and through it
    assert link.heard(1.0) is True


def test_an_atom_that_cant_reach_it_or_cant_be_reached_is_said(atom):
    link, _ = atom(near=False, heard_ms=-1)
    p = Purifier("bedroom purifier", link, sleep=lambda _: None)
    with pytest.raises(HomeError, match="the Atom couldn't connect to it"):
        p.status()
    assert link.heard(1.0) is False  # the Atom answers, and hasn't heard it
    link, fake = atom()
    fake.stop()
    with pytest.raises(HomeError, match="the Atom at 127.0.0.1 didn't answer"):
        Purifier("bedroom purifier", link, sleep=lambda _: None).status()
    assert link.heard(1.0) is None  # nothing known about the purifier


def test_an_atom_with_another_key_is_refused(atom):
    link, _ = atom(key="not-the-key")
    with pytest.raises(HomeError, match="refused the key"):
        Purifier("bedroom purifier", link, sleep=lambda _: None).status()


def test_an_atom_gone_quiet_on_wi_fi_is_told_as_the_atom_not_the_purifier():
    from examples.my_house.purifier import GONE_AFTER_S, Watch

    now, told, reachable = [0.0], [], [False]
    watch = Watch(
        "bedroom purifier",
        lambda s: True if reachable[0] else None,
        told.append,
        clock=lambda: now[0],
        via="tars-bt-bedroom.local",
    )
    now[0] = GONE_AFTER_S * 3
    for _ in range(4):
        watch.look()
    assert len(told) == 1 and "The Atom I reach the bedroom purifier through" in told[0] and not watch.gone
    reachable[0] = True
    watch.look()
    assert told[-1] == "The Atom for the bedroom purifier answers again."


def test_an_atom_purifier_needs_its_key(tmp_path):
    import examples.my_house as plugin
    from examples.my_house.purifier import Atom
    from voice_assistant.home import Context

    spec = {"name": "bedroom purifier", "address": "48:87:2D:1F:C1:5C", "atom": "tars-bt-bedroom.local"}
    with pytest.raises(ValueError, match="no PURIFIER_ATOM_KEY in .env"):
        plugin.setup({}, Context(tmp_path, lambda name: None))["purifier"](spec)
    made = plugin.setup({}, Context(tmp_path, {"PURIFIER_ATOM_KEY": "k"}.get))["purifier"](spec)
    assert isinstance(made._link, Atom) and made._link.host == "tars-bt-bedroom.local"
    with pytest.raises(ValueError, match="its Bluetooth address, like"):
        plugin.setup({}, Context(tmp_path, lambda name: None))["purifier"]({**spec, "address": "bedroom"})


def test_a_bridge_that_doesnt_answer_is_restarted_before_anyone_is_told():
    from examples.my_house.purifier import GONE_AFTER_S, Watch

    now, told, restarts = [0.0], [], []
    answers = iter([None, None, True])  # the bridge doesn't answer twice; restarted, it does
    watch = Watch(
        "air purifier",
        lambda s: next(answers),
        told.append,
        clock=lambda: now[0],
        restart=lambda: restarts.append(1) or True,
        via="127.0.0.1",
    )
    now[0] = GONE_AFTER_S * 2
    watch.look()
    watch.look()
    assert restarts == [1] and told == [] and not watch.stuck


def test_a_bridge_restart_that_doesnt_help_is_told_once_with_what_was_tried():
    from examples.my_house.purifier import GONE_AFTER_S, Watch

    now, told = [0.0], []
    watch = Watch(
        "air purifier", lambda s: None, told.append, clock=lambda: now[0], restart=lambda: False, via="127.0.0.1"
    )
    now[0] = GONE_AFTER_S * 2
    for _ in range(4):
        watch.look()
    (said,) = told
    assert "purifier bridge on this machine isn't answering" in said and "I restarted it and it still doesn't" in said


def test_the_watch_restarts_what_it_can_reach(monkeypatch):
    import examples.my_house as plugin
    from examples.my_house.purifier import Atom, Bluetooth, restart_bridge

    bluetooth = Bluetooth("48:87:2D:1F:FC:9B")
    assert plugin._restarter(bluetooth, None) == bluetooth.restart
    assert plugin._restarter(Atom("x", "127.0.0.1", "k"), "127.0.0.1") is restart_bridge
    assert plugin._restarter(Atom("x", "atom-bedroom.local", "k"), "atom-bedroom.local") is None  # out of reach


def test_restarting_the_bridge_waits_until_it_takes_connections(monkeypatch):
    import socket
    import subprocess

    from examples.my_house import purifier as module

    ran = []
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, b"", b"")
    )
    server = socket.create_server(("127.0.0.1", 0))
    monkeypatch.setattr(module, "ATOM_PORT", server.getsockname()[1])
    try:
        assert module.restart_bridge(wait_s=2) is True
    finally:
        server.close()
    assert ran == [["systemctl", "--user", "restart", "tars-purifier-bridge"]]
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 5, b"", b"Unit not found.")
    )
    assert module.restart_bridge(wait_s=0.1) is False
