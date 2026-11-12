"""TARS's own user on the household's Mosquitto (mosquitto.py): deploy/install-pi.sh's, for the doors' locks."""

import dataclasses
import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values

from voice_assistant import mosquitto
from voice_assistant.config import load_config
from voice_assistant.mosquitto import is_for, password_line, set_up

# What Mosquitto 2.1's own mosquitto_passwd wrote for tars / hunter2 (1000 rounds, a 64-byte salt).
REAL = (
    "tars:$7$1000$TvL3dLuI4B4Ru092xBhqkw7ixkr8zmJxPMrttW8MsKy6R0Uq5vjc23zgr0iERdITwzGNNtUiTYXA23xPvrvW5A==$RgO4F86Z2C5ef"
    "phHSEp3oeeYB5ySiyCHaXPzzC9Rtft2WnMf4kIHr0qM0YfrUEbfXPft02bRhF/MzaUYKnMOMA=="
)
OTHERS = ["homeassistant:$7$101$c2FsdA==$aGFzaA==", "nuki:$7$101$c2FsdA==$aGFzaA=="]


def test_a_line_is_checked_against_a_password_as_mosquitto_writes_it():
    assert is_for(REAL, "hunter2") and not is_for(REAL, "hunter3")
    made = password_line("tars", "s3cret")
    assert made.startswith("tars:$7$101$") and is_for(made, "s3cret") and not is_for(made, "other")
    assert password_line("tars", "x") != password_line("tars", "x")  # a salt of its own each time
    for other in ("tars:$6$c2FsdA==$aGFzaA==", "tars", "tars:$7$x$y$z", ""):
        assert not is_for(other, "hunter2")


class Docker:
    """docker, as set_up uses it: Mosquitto's passwords file in its folder, and whether its container runs."""

    def __init__(self, passwords: list[str], running: bool = True, fail: str = ""):
        self.passwords, self.running, self.fail = list(passwords), running, fail
        self.calls: list[list[str]] = []

    def __call__(self, args, input=None, cwd=None, **kw):
        assert args[0] == "docker" and kw["timeout"] > 0 and kw["check"] is False
        args = args[1:]
        self.calls.append(args)
        done = subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "inspect":
            done.stdout = "true\n" if self.running else "false\n"
        elif args[0] == "compose":
            assert cwd.name == "home-assistant"
        elif args[-2:] == ["cat", "/m/passwords"]:
            done.stdout = "".join(f"{line}\n" for line in self.passwords)
        elif args[-3:-1] == ["sh", "-c"]:
            if self.fail:
                return subprocess.CompletedProcess(args, 1, "", self.fail)
            assert "mv -f /m/.passwords.new /m/passwords" in args[-1] and "chmod 600" in args[-1]
            self.passwords = input.splitlines()
        return done

    def did(self, what: str) -> bool:
        return any(what in " ".join(call) for call in self.calls)


@pytest.fixture
def house(tmp_path):
    (tmp_path / "deploy/home-assistant/mosquitto").mkdir(parents=True)
    (tmp_path / "deploy/home-assistant/mosquitto/passwords").write_text("")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-1")  # no newline at the end
    cfg = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    presence = dataclasses.replace(
        cfg.presence,
        doors=[{"name": "front door", "topic": "nuki/4224E764"}],
        mqtt={"address": "localhost", "user": "tars", "secret": "MQTT_PASSWORD"},
    )
    return dataclasses.replace(cfg, presence=presence), tmp_path


def test_tars_gets_a_password_in_env_and_its_user_on_mosquitto_beside_the_others(house):
    cfg, root = house
    docker = Docker(OTHERS)
    said = set_up(cfg, root, docker, lambda: "made-up-password")
    assert said == "TARS's user tars is on Mosquitto, with a new password in .env as MQTT_PASSWORD"
    assert dotenv_values(root / ".env") == {"OPENAI_API_KEY": "sk-1", "MQTT_PASSWORD": "made-up-password"}
    assert (root / ".env").stat().st_mode & 0o777 == 0o600
    assert docker.passwords[:2] == OTHERS and is_for(docker.passwords[2], "made-up-password")
    # Brought up to date before its files change, then told to read its passwords and ACL again.
    assert [c[0] for c in docker.calls] == ["inspect", "compose", "run", "run", "kill"]
    assert docker.calls[1] == ["compose", "--profile", "mqtt", "up", "-d", "mosquitto"]
    assert docker.calls[-1] == ["kill", "-s", "HUP", "mosquitto"]


def test_run_again_it_changes_nothing(house):
    cfg, root = house
    (root / ".env").write_text("MQTT_PASSWORD=hunter2\n")
    docker = Docker([*OTHERS, REAL])
    assert set_up(cfg, root, docker) == "TARS's user tars is on Mosquitto already"
    assert docker.passwords == [*OTHERS, REAL] and not docker.did("sh -c")
    assert dotenv_values(root / ".env") == {"MQTT_PASSWORD": "hunter2"}


def test_a_line_for_another_password_is_replaced_and_the_others_kept(house):
    cfg, root = house
    (root / ".env").write_text("MQTT_PASSWORD=new-one\n")
    docker = Docker([REAL, *OTHERS])
    assert set_up(cfg, root, docker) == "TARS's user tars is on Mosquitto now, with MQTT_PASSWORD's password from .env"
    assert docker.passwords[:2] == OTHERS and is_for(docker.passwords[2], "new-one") and len(docker.passwords) == 3


def test_a_mosquitto_that_isnt_running_is_left_stopped_and_said(house):
    cfg, root = house
    docker = Docker(OTHERS, running=False)
    said = set_up(cfg, root, docker, lambda: "pw")
    assert said.endswith("; Mosquitto isn't running: cd deploy/home-assistant && docker compose --profile mqtt up -d")
    assert not docker.did("compose") and not docker.did("kill") and is_for(docker.passwords[-1], "pw")


@pytest.mark.parametrize(
    "change, says",
    [
        ({"doors": []}, "no [[presence.doors]]: TARS doesn't use the broker"),
        ({"mqtt": {"address": "192.168.1.20", "user": "tars", "secret": "MQTT_PASSWORD"}}, "isn't this machine's"),
        ({"mqtt": {"address": "localhost"}}, "!! [presence.mqtt] needs user and secret"),
    ],
)
def test_without_doors_or_with_a_broker_elsewhere_nothing_is_done(house, change, says):
    cfg, root = house
    cfg = dataclasses.replace(cfg, presence=dataclasses.replace(cfg.presence, **change))
    docker = Docker(OTHERS)
    assert says in set_up(cfg, root, docker) and docker.calls == []
    assert "MQTT_PASSWORD" not in (root / ".env").read_text()


def test_a_mosquitto_not_set_up_yet_waits_for_its_passwords(house):
    cfg, root = house
    (root / "deploy/home-assistant/mosquitto/passwords").unlink()
    docker = Docker([])
    assert set_up(cfg, root, docker).startswith("Mosquitto isn't set up on this machine yet") and docker.calls == []


def test_docker_trouble_is_said_and_marked(house, monkeypatch, capsys):
    cfg, root = house
    assert set_up(cfg, root, Docker(OTHERS, fail="no space left")) == (
        "!! couldn't write Mosquitto's passwords: no space left"
    )

    def missing(*a, **kw):
        raise FileNotFoundError("docker")

    assert set_up(cfg, root, missing).startswith("!! no Docker here")
    monkeypatch.chdir(root)
    monkeypatch.setattr("voice_assistant.config.load_config", lambda path: cfg)
    monkeypatch.setattr(mosquitto, "set_up", lambda cfg, root: "!! no Docker here")
    with pytest.raises(SystemExit) as exited:
        mosquitto.main()
    assert exited.value.code == 1 and "!! no Docker here" in capsys.readouterr().out
