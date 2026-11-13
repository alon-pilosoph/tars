"""TARS's browser on a Fly Sprite: the control service that runs there (deploy/browser-host/control.py), and this
machine's side of the link (browser/sprite.py), without a Sprite: the control service runs here with a stand-in for
Chrome, and ssh is replaced by a process that forwards nothing."""

import importlib.util
import json
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import HTTPServer
from pathlib import Path

import pytest

from voice_assistant.browser import sprite
from voice_assistant.browser.sprite import Control, SpriteUnreachable, ssh_command
from voice_assistant.check import FAIL, OK, sprite_browser
from voice_assistant.config import _problems, load_config

_spec = importlib.util.spec_from_file_location("control", Path(__file__).parents[1] / "deploy/browser-host/control.py")
control = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(control)
KEY = "k" * 64
REPO_CONFIG = Path(__file__).parents[1] / "config.toml"


class FakeChrome:
    def __init__(self, dies: bool = False):
        self.dies, self.signals = dies, []

    def poll(self):
        return 1 if self.dies or self.signals else None

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.signals.append("kill")


@pytest.fixture
def host(tmp_path):
    launched = []

    def launch(command):
        launched.append(command)
        return FakeChrome()

    made = control.Host(launch=launch, ready=lambda: True, profiles=tmp_path)
    made.launched = launched
    return made


@pytest.fixture
def served(host):
    server = HTTPServer(("127.0.0.1", 0), control.handler(host, KEY.encode()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], host
    server.shutdown()
    server.server_close()


def test_chrome_is_started_with_the_profile_and_the_way_out(host, tmp_path):
    host.open("profile-0a1b2c3d4e5f")
    command = host.launched[0]
    assert f"--user-data-dir={tmp_path / 'profile-0a1b2c3d4e5f'}" in command
    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--proxy-server=socks5://127.0.0.1:1080" in command
    assert "--proxy-bypass-list=<-loopback>" in command  # the Sprite's own localhost goes to the way out too
    assert "--disable-quic" in command
    assert not any("headless" in flag for flag in command)


@pytest.mark.parametrize("name", ["", "../etc", "a/b", ".hidden", "Profile", "x" * 65, "p p", "profile\n"])
def test_a_profile_is_a_folder_name_and_nothing_else(host, name):
    with pytest.raises(ValueError):
        host.open(name)
    assert host.launched == []


def test_one_chrome_at_a_time(host):
    host.open("profile")
    first = host.process
    host.open("profile-0a1b2c3d4e5f")
    assert first.signals  # the first was shut before the second started
    assert host.running()
    host.close()
    assert not host.running()


def test_a_chrome_that_never_answers_is_shut_and_said(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "START_S", 0.3)
    host = control.Host(launch=lambda command: FakeChrome(), ready=lambda: False, profiles=tmp_path)
    with pytest.raises(RuntimeError):
        host.open("profile")
    assert not host.running()


def test_the_control_service_over_http(served):
    port, _ = served
    asked = Control(port, KEY)
    assert asked.health() == {"ok": True, "chrome": False}
    asked.open("profile")
    assert asked.health()["chrome"] is True
    asked.close()
    assert asked.health()["chrome"] is False


@pytest.mark.parametrize("key", ["", "wrong", KEY[:-1]])
def test_the_control_service_wants_its_key(served, key):
    port, host = served
    with pytest.raises(urllib.error.HTTPError) as refused:
        Control(port, key).ask("/open", {"profile": "profile"})
    assert refused.value.code == 403
    assert host.launched == []


def test_a_bad_profile_name_over_http_is_a_400(served):
    port, _ = served
    request = urllib.request.Request(f"http://127.0.0.1:{port}/open", data=json.dumps({"profile": "../x"}).encode(),
                                     headers={"X-Key": KEY})  # fmt: skip
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=5)
    assert refused.value.code == 400


# This machine's side


def test_the_link_forwards_three_ports_and_runs_nothing():
    command = ssh_command("tars-browser", 41000, 41001, 41002, "/usr/local/bin/sprite")
    joined = " ".join(command)
    assert command[:2] == ["ssh", "-N"]  # no command on the Sprite
    assert "StrictHostKeyChecking=yes" in joined and "BatchMode=yes" in joined
    assert "ProxyCommand=/usr/local/bin/sprite proxy -s tars-browser -W :2222" in command
    assert [command[i + 1] for i, a in enumerate(command) if a in ("-L", "-R", "-D")] == [
        "127.0.0.1:41000:localhost:9333",
        "127.0.0.1:41001:localhost:9334",
        "localhost:1080:127.0.0.1:41002",
    ]
    assert command[-1] == "sprite@tars-browser"


@pytest.mark.parametrize("name", ["a;touch x", "a b", "$(id)", "A", "-oProxyCommand=x", ""])
def test_a_sprite_name_cannot_reach_the_shell(name):
    with pytest.raises(SpriteUnreachable):
        ssh_command(name, 1, 2, 3, "/usr/local/bin/sprite")


def test_a_sprite_name_in_config_is_checked():
    cfg = load_config(REPO_CONFIG, local=False)
    cfg.browser.sprite = "tars browser;x"
    assert any("[browser] sprite" in p for p in _problems(cfg))
    cfg.browser.sprite = "tars-browser"
    assert not any("[browser] sprite" in p for p in _problems(cfg))


@pytest.fixture
def keys(tmp_path, monkeypatch):
    for name, value in (("LINK_KEY", "key"), ("KNOWN_HOSTS", "tars-browser ssh-ed25519 AAAA"), ("CONTROL_KEY", KEY)):
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setattr(sprite, name, path)
    monkeypatch.setattr(sprite, "sprite_cli", lambda: "/usr/local/bin/sprite")


def test_no_keys_set_up_is_said(tmp_path, monkeypatch):
    monkeypatch.setattr(sprite, "LINK_KEY", tmp_path / "none")
    with pytest.raises(SpriteUnreachable, match="setup.sh"), sprite.link("tars-browser", frozenset()):
        pass


def test_a_link_that_closes_says_why(keys):
    def run(command, **kw):
        kw["stderr"].write(b"Host key verification failed.\n")
        return subprocess.Popen(["false"])

    with (
        pytest.raises(SpriteUnreachable, match="Host key verification failed"),
        sprite.link("tars-browser", frozenset(), run),
    ):
        pass


def test_a_sprite_that_never_answers(keys, monkeypatch):
    monkeypatch.setattr(sprite, "UP_S", 1.0)
    run = lambda command, **kw: subprocess.Popen(["sleep", "30"])  # a link that forwards nothing
    with pytest.raises(SpriteUnreachable, match="didn't answer"), sprite.link("tars-browser", frozenset(), run):
        pass


def test_check_says_how_the_sprite_is():
    cfg = load_config(REPO_CONFIG, local=False)
    cfg.browser.enabled, cfg.browser.sprite = True, "tars-browser"
    assert sprite_browser(cfg, "dry runs", reach=lambda name: {"ok": True}).status == OK

    def unreachable(name):
        raise SpriteUnreachable("tars-browser didn't answer in 60 s")

    failed = sprite_browser(cfg, "dry runs", reach=unreachable)
    assert failed.status == FAIL and "setup.sh tars-browser" in failed.fix


def test_chrome_is_closed_as_a_person_closes_it_before_the_control_service_stops_it(keys, monkeypatch):
    """Through its debugging port first, which saves the cookies an errand was given; a signal loses the newest."""
    said = []

    class FakeControl:
        def open(self, profile):
            said.append(("open", profile))

        def close(self):
            said.append(("control close",))

    @contextmanager
    def fake_link(sprite_name, home, run):
        yield FakeControl(), 41000

    monkeypatch.setattr(sprite, "link", fake_link)
    monkeypatch.setattr(sprite, "close_chrome", lambda port: said.append(("chrome close", port)))
    monkeypatch.setattr(sprite, "ADDRESS", Path("/nowhere/sprite-address"))
    with sprite.sprite_chrome("tars-browser", Path("/x/profile-0a1b2c3d4e5f")) as (address, screen):
        assert address == "http://127.0.0.1:41000" and screen is None
    assert said == [("open", "profile-0a1b2c3d4e5f"), ("chrome close", 41000), ("control close",)]


# The hand-over: the gate on the Sprite's public address, and TARS's side


class FakeProcess:
    def __init__(self):
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.stopped = True


@pytest.fixture
def gate(monkeypatch):
    """The gate on a free port, with an echoing server where the viewer would be; the view's clock is the test's."""
    viewer = socket.create_server(("127.0.0.1", 0))

    def serve():
        while True:
            try:
                connection, _ = viewer.accept()
            except OSError:
                return
            with connection:
                head = connection.recv(65536)
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok" if head else b""
                )

    threading.Thread(target=serve, daemon=True).start()
    now, launched = [0.0], []

    def launch(command):
        launched.append(command[0])
        made = FakeProcess()
        launched.append(made)
        return made

    view = control.View(launch=launch, clock=lambda: now[0])
    server = control.Gate(view, port=0, viewer_port=viewer.getsockname()[1])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], view, now, launched
    server.shutdown()
    server.server_close()
    viewer.close()


def get(port: int, path: str, cookie: str = "") -> tuple[int, dict, bytes]:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        extra = f"Cookie: {cookie}\r\n" if cookie else ""
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n{extra}\r\n".encode())
        got = b""
        while chunk := s.recv(65536):
            got += chunk
    head, _, body = got.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers = dict(line.split(": ", 1) for line in lines[1:])
    return int(lines[0].split()[1]), headers, body


def test_the_gate_answers_nothing_without_a_hand_over(gate):
    port, _, _, launched = gate
    assert get(port, "/")[0] == 404
    assert get(port, "/view?t=anything")[0] == 404
    assert get(port, "/vnc.html")[0] == 404
    assert launched == []  # no VNC server or viewer running


def test_the_first_browser_with_the_token_gets_in_and_no_other(gate):
    port, view, _, launched = gate
    token = view.start()
    assert [x for x in launched if isinstance(x, str)] == ["/usr/bin/x11vnc", "/usr/bin/websockify"]
    assert get(port, "/vnc.html")[0] == 403  # no cookie
    assert get(port, "/view?t=wrong")[0] == 403
    status, headers, _ = get(port, f"/view?t={token}")
    assert status == 302 and headers["Location"].startswith("/vnc.html")
    cookie = headers["Set-Cookie"].split(";")[0]
    assert "Secure" in headers["Set-Cookie"] and "HttpOnly" in headers["Set-Cookie"]
    assert get(port, "/vnc.html", cookie)[2] == b"ok"  # passed through to the viewer
    assert get(port, f"/view?t={token}")[0] == 403  # the link lets in one browser only
    assert get(port, "/vnc.html", "tars-view=guess")[0] == 403
    assert view.status() == {"active": True, "opened": True}


def test_a_hand_over_ends_when_told_or_at_its_time(gate):
    port, view, now, launched = gate
    view.start()
    secret = view.admit(view.token)
    view.end()
    assert get(port, "/vnc.html", f"tars-view={secret}")[0] == 404
    assert all(p.stopped for p in launched if isinstance(p, FakeProcess))
    view.start()
    secret = view.admit(view.token)
    now[0] += control.VIEW_S + 1
    assert get(port, "/vnc.html", f"tars-view={secret}")[0] == 404
    assert view.status() == {"active": False, "opened": False}


def test_the_vnc_server_listens_on_the_sprites_localhost_only():
    assert "-localhost" in control.vnc_command()
    assert all(a.startswith("127.0.0.1:") for a in control.viewer_command()[-2:])


class FakeScreen:
    def __init__(self, fails=False):
        self.fails, self.closed = fails, False

    def open(self, clip=""):
        self.clip = clip
        if self.fails:
            raise OSError("no link")
        return "https://s.example/view?t=abc", 15

    def opened(self):
        return True

    def close(self):
        self.closed = True


class FakePage:
    url = "https://wolt.com/en/login"


def test_handing_over_asks_the_person_and_always_ends_the_hand_over(monkeypatch):
    from voice_assistant.browser.run import hand_over

    laid_out = []

    @contextmanager
    def as_phone(page):
        laid_out.append("phone")
        yield "390x780+0+87"
        laid_out.append("back")

    monkeypatch.setattr(sprite, "as_phone", as_phone)
    asked = []

    class Person:
        def take_over(self, site, reason, link, minutes, opened):
            asked.append((site, reason, link, minutes, opened()))
            return True

    session = type("S", (), {"page": FakePage(), "screen": FakeScreen()})()
    said = hand_over(session, "solve the robot check", Person())
    assert asked == [("wolt.com", "solve the robot check", "https://s.example/view?t=abc", 15, True)]
    assert "carry on" in said and session.screen.closed and laid_out == ["phone", "back"]
    assert session.screen.clip == "390x780+0+87"  # the view shows the page alone

    session.screen = FakeScreen(fails=True)
    assert "call done" in hand_over(session, "solve the robot check", Person())


def test_with_no_screen_the_model_is_told_to_say_what_stopped_it():
    from voice_assistant.browser.tools import Tools

    session = type("S", (), {"page": FakePage(), "screen": None})()
    did = Tools(session, approver=None, hand_over=lambda s, r: "never").call("hand_over", {"reason": "captcha"})
    assert did.trouble and "say exactly what" in did.said


def test_a_hand_over_shows_only_the_part_of_the_screen_its_given_and_nothing_odd():
    assert control.vnc_command("390x780+0+87")[-2:] == ["-clip", "390x780+0+87"]
    assert "-clip" not in control.vnc_command()
    view = control.View(launch=lambda command: FakeProcess())
    with pytest.raises(ValueError):
        view.start("390x780+0+87; rm -rf /")
    assert not view.active()
