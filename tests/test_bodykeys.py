"""Network bodies' device keys (bodykeys.py) and pairing: a key is shown once and kept only as its public half, a
removed one is refused, and none of it shows in the logs or --check."""

import dataclasses
import json
import time
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from voice_assistant import check
from voice_assistant.bodykeys import BodyKeys, parse_key, public_of, sign, verify
from voice_assistant.bodylink import BodyListener, welcome
from voice_assistant.config import BodiesConfig, load_config

REPO_CONFIG = Path(__file__).parents[1] / "config.toml"


@pytest.fixture
def keys(log):
    return BodyKeys(log.store, clock=lambda: 1_800_000_000.0)


def test_a_key_is_the_device_and_its_private_key_and_only_the_public_half_is_kept(keys, log):
    text = keys.add("Ari's room", kind="atom")
    device, seed = parse_key(text)
    assert text.startswith("tars-body-1:aris-room-") and device.startswith("aris-room-")
    assert keys.public_key(device) == public_of(seed)
    stored = json.dumps(log.store.rows("SELECT * FROM bodies"))
    assert text.rsplit(":", 1)[1] not in stored and seed.hex() not in stored  # the private key isn't kept anywhere
    assert keys.room_of(device) == "Ari's room"


def test_a_signature_proves_the_key_and_nothing_else_does():
    seed, other = b"a" * 32, b"b" * 32
    proof = sign(seed, "nonce-1")
    assert verify(public_of(seed), "nonce-1", proof)
    assert not verify(public_of(seed), "nonce-2", proof)  # another challenge
    assert not verify(public_of(other), "nonce-1", proof)
    assert not verify(public_of(seed), "nonce-1", "not hex")


@pytest.mark.parametrize("text", ["", "tars-body-2:x:AAAA", "tars-body-1::AAAA", "tars-body-1:x:short", "hello"])
def test_something_that_isnt_a_key_is_said_so(text):
    with pytest.raises(ValueError):
        parse_key(text)


def test_one_body_a_room_until_it_is_removed(keys):
    keys.add("Kitchen")
    with pytest.raises(ValueError, match="already has a body"):
        keys.add("kitchen")
    assert keys.remove("KITCHEN") == 1 and keys.listed() == []
    keys.add("Kitchen")
    assert [r["room"] for r in keys.listed()] == ["Kitchen"]


def body_connects(port: int, key: str) -> bool:
    device, seed = parse_key(key)
    with connect(f"ws://127.0.0.1:{port}") as ws:
        nonce = json.loads(ws.recv(timeout=5))["nonce"]
        ws.send(json.dumps({"type": "hello", "protocol": 1, "device": device, "proof": sign(seed, nonce)}))
        try:
            return json.loads(ws.recv(timeout=5))["type"] == "welcome"
        except ConnectionClosed:
            return False


def test_a_paired_body_gets_in_is_noted_and_a_removed_one_is_refused(keys, capsys):
    key = keys.add("Kitchen")
    listener = BodyListener(keys.public_key, lambda hello: welcome("Kitchen", 24000), host="127.0.0.1",
                            seen=keys.seen, sleep=lambda s: None).start()  # fmt: skip
    try:
        assert body_connects(listener.port, key)
        deadline = time.monotonic() + 5  # noted just after it's welcomed, on the listener's thread
        while keys.listed()[0]["last_seen"] is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert keys.listed()[0]["last_seen"] == 1_800_000_000.0
        keys.remove("Kitchen")
        assert not body_connects(listener.port, key)
    finally:
        listener.close()
    said = capsys.readouterr().out
    assert key.rsplit(":", 1)[1] not in said  # never in the log


def test_check_says_whether_bodies_may_connect_and_each_one_paired_without_its_key(log, keys):
    key = keys.add("Kitchen")
    cfg = load_config(REPO_CONFIG, local=False)
    off = check.bodies(cfg, log.folder)
    assert [(r.status, r.detail) for r in off] == [
        ("warn", "off ([bodies] enabled), with 1 paired: they can't connect")
    ]
    on = dataclasses.replace(cfg, bodies=BodiesConfig(enabled=True))
    lines = [check.line(r) for r in check.bodies(on, log.folder, now=1_800_000_000.0 + 600)]
    assert "port 8766" in lines[0] and "Body: Kitchen" in lines[1] and "never connected yet" in lines[1]
    keys.seen(parse_key(key)[0])
    lines = [check.line(r) for r in check.bodies(on, log.folder, now=1_800_000_000.0 + 600)]
    assert "last connected 10 minutes ago" in lines[1]
    assert all(key.rsplit(":", 1)[1] not in line for line in lines)


def test_with_none_paired_and_bodies_off_nothing_is_said(tmp_path):
    cfg = load_config(REPO_CONFIG, local=False)
    assert [r.status for r in check.bodies(cfg, tmp_path)] == ["skip"]


def test_the_commands_pair_list_and_remove(tmp_path, monkeypatch, capsys):
    from voice_assistant import entrypoints

    cfg = load_config(REPO_CONFIG, local=False)
    cfg = dataclasses.replace(cfg, learning=dataclasses.replace(cfg.learning, folder=str(tmp_path / "events")))
    entrypoints.bodies_command(cfg, tmp_path, add="Kitchen", listed=False, remove=None)
    out = capsys.readouterr().out
    key = next(line for line in out.splitlines() if line.startswith("tars-body-1:"))
    assert "shown this once" in out and "[bodies] is off" in out
    entrypoints.bodies_command(cfg, tmp_path, add=None, listed=True, remove=None)
    listed = capsys.readouterr().out
    assert "Kitchen" in listed and "last connected never" in listed and key.rsplit(":", 1)[1] not in listed
    entrypoints.bodies_command(cfg, tmp_path, add=None, listed=False, remove="kitchen")
    assert "unpaired" in capsys.readouterr().out
    entrypoints.bodies_command(cfg, tmp_path, add=None, listed=True, remove=None)
    assert "No network bodies paired" in capsys.readouterr().out


def test_bodies_settings_are_checked():
    from voice_assistant.config import ConfigError, _problems

    cfg = load_config(REPO_CONFIG, local=False)
    assert cfg.bodies == BodiesConfig() and not cfg.bodies.enabled  # off as shipped: nothing listens
    bad = dataclasses.replace(cfg, bodies=BodiesConfig(enabled=True, port=0, plain_from_home=False))
    problems = _problems(bad)
    assert any("port" in p for p in problems) and any("plain_from_home" in p for p in problems)
    assert ConfigError
