"""TARS's browser on a Fly Sprite: the control service that runs there (deploy/browser-host/control.py), and this
machine's side of the link (browser/sprite.py), without a Sprite: the control service runs here with a stand-in for
Chrome, and ssh is replaced by a process that forwards nothing."""

import importlib.util
import json
import subprocess
import threading
import urllib.error
import urllib.request
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
