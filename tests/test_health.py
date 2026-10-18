"""The Status page's data: each process's heartbeat says whether it's running, the day's wakes and answers are
counted, the services are asked with their keys and read from their own status pages (once a minute at most), and the
machine's numbers are read, the Pi's throttling included."""

import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from voice_assistant import health
from voice_assistant.check import FAIL, OK, SKIP, Result, service_checks
from voice_assistant.config import load_config
from voice_assistant.health import (
    HEARTBEAT_EVERY_S,
    Heartbeat,
    Services,
    machine,
    processes,
    status_page,
    today,
)
from voice_assistant.webui import create_app

REPO_CONFIG = Path(__file__).parents[1] / "config.toml"


def test_a_process_that_beat_lately_is_running_and_one_that_stopped_isnt(tmp_path):
    Heartbeat(tmp_path, "assistant").beat()
    states = {"voice-assistant": "active", "voice-assistant-web": "inactive"}
    found = {p["name"]: p for p in processes(tmp_path, ["assistant", "web"], states.get, time.time())}
    assistant, web = found["assistant"], found["web"]
    assert assistant["running"] and assistant["pid"] and assistant["label"] == "The assistant"
    assert assistant["unit_state"] == "active"
    assert not web["running"] and web["beat"] is None and web["unit_state"] == "inactive"  # never seen
    later = time.time() + HEARTBEAT_EVERY_S * 4
    assert not processes(tmp_path, ["assistant"], states.get, later)[0]["running"]  # it stopped beating


def test_a_heartbeat_says_which_version_runs(tmp_path):
    beat = Heartbeat(tmp_path, "web", root=Path(__file__).parents[1])
    beat.beat()
    saved = json.loads((tmp_path / "heartbeats" / "web.json").read_text())
    assert saved["version"] and saved["host"] and saved["beat"] >= saved["started"]


def test_a_heartbeat_that_cant_be_written_doesnt_stop_the_process(tmp_path, capsys):
    (tmp_path / "heartbeats").write_text("a file where the folder should be")
    Heartbeat(tmp_path, "assistant").beat()
    assert "couldn't write the heartbeat" in capsys.readouterr().out


def test_today_counts_the_wakes_answers_and_failures_since_midnight(log, convos):
    import numpy as np

    now = time.time()
    yesterday = now - 2 * 24 * 3600
    wake = log.add_wake(np.zeros(1600, np.int16), 0.9, "answer", "hey tars", 0.9, ts=now - 1)
    log.add_wake(np.zeros(1600, np.int16), 0.9, "answer", "hey tars", 0.9, ts=yesterday)
    conversation = convos.start(wake, ts=now - 1)
    convos.add_tars_turn(conversation, "Nine.", ts=now - 1)
    convos.add_tars_turn(conversation, "", ts=now - 1, failed_at="llm", error="down")
    found = today(log.store, now)
    assert (found["wakes"], found["answers"], found["failed"]) == (1, 1, 1)
    assert found["last_wake"] == pytest.approx(now - 1)


def test_the_machine_is_read_and_a_pis_throttling_said_in_words(tmp_path):
    found = machine(tmp_path, throttled=lambda: 0x50005)  # under-voltage and throttled, now and since it started
    assert found["cores"] >= 1 and 0 < found["memory_used"] <= found["memory_total"]
    assert 0 < found["disk_used"] <= found["disk_total"] and found["uptime_s"] > 0
    assert found["throttled"] == [
        health.THROTTLED[0],
        health.THROTTLED[2],
        health.THROTTLED[16],
        health.THROTTLED[18],
    ]
    assert machine(tmp_path, throttled=lambda: None)["throttled"] is None  # not a Pi
    assert machine(tmp_path, throttled=lambda: 0)["throttled"] == []  # a Pi with nothing wrong


def test_a_services_own_status_page_is_read_and_one_that_cant_be_is_said_so():
    def get(url):
        assert url == "https://status.deepgram.com/api/v2/status.json"
        return {"status": {"indicator": "minor", "description": "Partially Degraded Service"}}

    page = status_page("https://status.deepgram.com", get)
    assert page == {
        "indicator": "minor",
        "description": "Partially Degraded Service",
        "url": "https://status.deepgram.com",
    }

    def down(url):
        raise httpx.ConnectError("no route to host")

    assert status_page("https://status.deepgram.com", down)["indicator"] == "unknown"


def test_the_services_are_asked_together_once_a_minute_with_their_status_pages():
    asked, now = [], [1000.0]

    def answering(name, status):
        return lambda: asked.append(name) or Result(name, status, "answers (0.2 s)")

    checks = [
        ("OpenAI", answering("OpenAI", OK)),
        ("Groq", answering("Groq", FAIL)),
        ("Cartesia", answering("Cartesia", SKIP)),  # not used: left out
        ("Telegram", answering("Telegram", OK)),  # no status page of its own
    ]
    pages = {
        "https://status.openai.com/api/v2/status.json": "none",
        "https://groqstatus.com/api/v2/status.json": "major",
    }

    def get(url):
        return {"status": {"indicator": pages[url], "description": "..."}}

    services = Services(lambda: checks, get, clock=lambda: now[0])
    found, at = services.get()
    assert [s["name"] for s in found] == ["OpenAI", "Groq", "Telegram"] and at == 1000.0
    assert [(s["status"], (s["page"] or {}).get("indicator")) for s in found] == [
        (OK, "none"),
        (FAIL, "major"),
        (OK, None),
    ]
    now[0] += 30
    services.get()
    assert len(asked) == 4  # not asked again within the minute
    now[0] += 31
    assert services.get()[1] == 1061.0 and len(asked) == 8


def test_a_check_that_raises_is_a_failure_not_a_crash():
    def broken():
        raise RuntimeError("boom")

    (found,), _ = Services(lambda: [("OpenAI", broken)], lambda url: {"status": {}}).get()
    assert found["status"] == FAIL and "boom" in found["detail"]


def test_the_service_checks_are_the_ones_check_runs(tmp_path):
    (tmp_path / ".env").write_text("")
    cfg = load_config(REPO_CONFIG, local=False)
    names = [name for name, _ in service_checks(cfg, tmp_path)]
    assert names == ["OpenAI", "Groq", "Cerebras", "Deepgram", "Cartesia", "Telegram"]
    results = {name: check() for name, check in service_checks(cfg, tmp_path)}
    assert results["OpenAI"].status == SKIP and results["Telegram"].status == SKIP  # no keys: not used


def test_the_status_page_is_served_only_when_set_up(log):
    client = TestClient(create_app(log, health=lambda: {"now": 1}), base_url="http://localhost")
    assert client.get("/api/health").json() == {"now": 1}
    assert client.get("/api/status").json()["health"] is True
    bare = TestClient(create_app(log), base_url="http://localhost")
    assert bare.get("/api/health").status_code == 404 and bare.get("/api/status").json()["health"] is False
