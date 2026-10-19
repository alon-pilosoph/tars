"""The Status page's data: each process's heartbeat says whether it's running, the day's answers are counted (and only
answers), the services are asked with their keys and read from their own status pages (once a minute at most, and
never for longer than ASK_DEADLINE_S), and the machine's numbers are read, the Pi's throttling included."""

import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from voice_assistant import health
from voice_assistant.check import FAIL, OK, SKIP, Result
from voice_assistant.config import load_config
from voice_assistant.health import (
    HEARTBEAT_EVERY_S,
    STATUS_PAGES,
    Health,
    Heartbeat,
    Services,
    expected,
    machine,
    processes,
    status_page,
    today,
)
from voice_assistant.verify import ANSWER
from voice_assistant.webui import create_app

from .conftest import AUDIO

REPO = Path(__file__).parents[1]
NOW = 1_800_000_000.0


def states(**by_unit):
    return lambda unit: by_unit.get(unit, "inactive")


def test_a_process_that_beat_lately_is_running_and_one_that_stopped_isnt(tmp_path):
    Heartbeat(tmp_path, "assistant").beat()
    unit_state = states(**{"voice-assistant": "active"})
    found = {p["name"]: p for p in processes(tmp_path, ["assistant", "web"], unit_state, time.time())}
    assistant, web = found["assistant"], found["web"]
    assert assistant["running"] and assistant["label"] == "The assistant" and assistant["unit_state"] == "active"
    assert not web["running"] and web["beat"] is None and web["unit_state"] == "inactive"  # never seen
    later = time.time() + HEARTBEAT_EVERY_S * 4
    assert not processes(tmp_path, ["assistant"], unit_state, later)[0]["running"]  # it stopped beating


def test_a_beat_from_before_the_clock_was_set_back_is_no_sign_of_life(tmp_path):
    Heartbeat(tmp_path, "assistant").beat()
    an_hour_back = time.time() - 3600
    assert not processes(tmp_path, ["assistant"], states(), an_hour_back)[0]["running"]


def test_a_heartbeat_says_when_it_started_and_which_commit_runs(tmp_path):
    beat = Heartbeat(tmp_path, "web", root=REPO)
    beat.beat()
    saved = json.loads((tmp_path / "heartbeats" / "web.json").read_text())
    assert saved["version"] and saved["beat"] >= saved["started"] > saved["beat"] - 5


def test_a_heartbeat_that_cant_be_written_doesnt_stop_the_process_and_says_so_once(tmp_path, capsys):
    (tmp_path / "heartbeats").write_text("a file where the folder should be")
    beat = Heartbeat(tmp_path, "assistant")
    beat.beat()
    beat.beat()
    assert capsys.readouterr().out.count("couldn't write the heartbeat") == 1


@pytest.mark.parametrize("written", ["null", "[1, 2]", '{"beat": "soon"}', "not json"])
def test_a_heartbeat_that_isnt_one_reads_as_never_seen(tmp_path, written):
    (tmp_path / "heartbeats").mkdir()
    (tmp_path / "heartbeats" / "assistant.json").write_text(written)
    found = processes(tmp_path, ["assistant"], states(), time.time())[0]
    assert not found["running"] and found["beat"] is None


def test_systemd_with_no_session_to_ask_says_nothing(tmp_path):
    assert processes(tmp_path, ["web"], lambda unit: "unknown", time.time())[0]["unit_state"] is None


def test_the_processes_are_the_setups(tmp_path):
    cfg = load_config(REPO / "config.toml", local=False)
    assert expected(cfg) == ["assistant", "web"] + (["telegram"] if cfg.telegram.enabled else [])


def test_today_counts_the_wakes_and_answers_since_midnight_and_nothing_tars_said_by_itself(log, convos):
    now = time.time()
    yesterday = now - 2 * 24 * 3600
    wake = log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9, ts=now - 1)
    log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9, ts=yesterday)
    conversation = convos.start(wake, ts=now - 1)
    convos.add_tars_turn(conversation, "Nine.", ts=now - 1, answered_by="quick")
    convos.add_tars_turn(conversation, "", ts=now - 1, failed_at="llm", error="down")
    convos.add_tars_turn(conversation, "Time to leave.", ts=now - 1)  # a reminder: it answered nobody
    found = today(log, convos, now)
    assert (found["wakes"], found["answers"], found["failed"]) == (1, 1, 1)
    assert found["last_wake"] == pytest.approx(now - 1)


def test_the_machine_is_read_and_a_pis_throttling_said_in_words(tmp_path):
    found = machine(tmp_path, read_throttled=lambda: 0x50005)  # under-voltage and throttled, now and since it started
    assert found["cores"] >= 1 and 0 < found["memory_used"] <= found["memory_total"]
    assert 0 < found["disk_free"] <= found["disk_total"] and found["uptime_s"] > 0
    assert found["throttled"] == [
        health.THROTTLED[0],
        health.THROTTLED[2],
        health.THROTTLED[16],
        health.THROTTLED[18],
    ]
    assert machine(tmp_path, read_throttled=lambda: None)["throttled"] is None  # not a Pi
    assert machine(tmp_path, read_throttled=lambda: 0)["throttled"] == []  # a Pi with nothing wrong


def test_a_services_own_status_page_is_read_and_one_that_cant_be_is_said_so():
    def fetch_json(url):
        assert url == "https://status.deepgram.com/api/v2/status.json"
        return {"status": {"indicator": "minor", "description": "Partially Degraded Service"}}

    page = status_page("https://status.deepgram.com", fetch_json)
    assert page == {
        "indicator": "minor",
        "description": "Partially Degraded Service",
        "url": "https://status.deepgram.com",
    }

    def down(url):
        raise httpx.ConnectError("no route to host")

    page = status_page("https://status.deepgram.com", down)
    assert page["indicator"] == "unknown" and "ConnectError" in page["description"]


def answering(asked: list, name: str, status: str):
    return lambda: asked.append(name) or Result(name, status, "answers (0.2 s)")


def test_the_services_are_asked_together_once_a_minute_with_their_status_pages():
    asked, clock = [], [1000.0]
    checks = [
        ("OpenAI", answering(asked, "OpenAI", OK)),
        ("Groq", answering(asked, "Groq", FAIL)),
        ("Cartesia", answering(asked, "Cartesia", SKIP)),  # not used: left out
        ("Telegram", answering(asked, "Telegram", OK)),  # no status page of its own
    ]
    pages = {"https://status.openai.com": "none", "https://groqstatus.com": "major"}

    def fetch_json(url):
        return {"status": {"indicator": pages.get(url.removesuffix("/api/v2/status.json"), "none"), "description": ""}}

    services = Services(lambda: checks, fetch_json, clock=lambda: clock[0], wall=lambda: NOW)
    found, at = services.get()
    assert [s["name"] for s in found] == ["OpenAI", "Groq", "Telegram"] and at == NOW
    assert [(s["status"], (s["page"] or {}).get("indicator")) for s in found] == [
        (OK, "none"),
        (FAIL, "major"),
        (OK, None),
    ]
    clock[0] += 30
    services.get()
    assert len(asked) == 4  # not asked again within the minute
    clock[0] += 31
    services.get()
    assert len(asked) == 8


def test_a_service_that_hangs_is_a_failure_after_the_deadline_not_a_page_that_never_comes(monkeypatch):
    monkeypatch.setattr(health, "ASK_DEADLINE_S", 0.2)
    hung = threading.Event()
    checks = [("OpenAI", lambda: hung.wait(5) and Result("OpenAI", OK, "late")), ("Groq", answering([], "Groq", OK))]
    t = time.monotonic()
    found, _ = Services(lambda: checks, lambda url: hung.wait(5) or {}).get()
    hung.set()
    assert time.monotonic() - t < 1
    assert [(s["name"], s["status"]) for s in found] == [("OpenAI", FAIL), ("Groq", OK)]
    assert "no answer within" in found[0]["detail"] and found[1]["page"]["indicator"] == "unknown"


def test_while_the_services_are_asked_others_get_the_last_answers_at_once():
    asking, release = threading.Event(), threading.Event()
    calls = []

    def slow():
        calls.append(1)
        if len(calls) > 1:
            asking.set()
            release.wait(5)
        return Result("OpenAI", OK, "answers")

    clock = [0.0]
    services = Services(lambda: [("OpenAI", slow)], lambda url: {}, clock=lambda: clock[0])
    services.get()
    clock[0] += 61
    threading.Thread(target=services.get, daemon=True).start()
    assert asking.wait(5)
    t = time.monotonic()
    found, _ = services.get()  # the last answers, without waiting for the new ones
    release.set()
    assert time.monotonic() - t < 0.5 and found[0]["detail"] == "answers"


def test_a_check_that_raises_or_exits_is_a_failure_not_a_crash():
    from voice_assistant.config import ConfigError

    def broken():
        raise RuntimeError("boom")

    def misconfigured():
        raise ConfigError("Set OPENAI_API_KEY in .env")

    checks = [("OpenAI", broken), ("Groq", misconfigured)]
    found, _ = Services(lambda: checks, lambda url: {"status": {}}).get()
    assert [s["status"] for s in found] == [FAIL, FAIL]
    assert "boom" in found[0]["detail"] and "OPENAI_API_KEY" in found[1]["detail"]


def test_every_service_with_a_status_page_is_one_check_asks(tmp_path):
    from voice_assistant.check import service_checks

    (tmp_path / ".env").write_text("OPENAI_API_KEY=o\nDEEPGRAM_API_KEY=d\n")
    cfg = load_config(REPO / "config.toml", local=False)
    names = {name for name, _ in service_checks(cfg, tmp_path)}
    assert set(STATUS_PAGES) <= names


def test_the_status_page_is_served_only_when_set_up(log, convos):
    services = Services(lambda: [("OpenAI", answering([], "OpenAI", OK))], lambda url: {"status": {}})
    report = Health(log, ["assistant", "web"], services, states(), read_throttled=lambda: None).report
    found = TestClient(create_app(log, health=report)).get("/api/health").json()
    assert set(found) == set(health.Report.__annotations__)
    assert set(found["machine"]) == set(health.MachineHealth.__annotations__)
    assert [p["name"] for p in found["processes"]] == ["assistant", "web"]
    assert found["services"][0]["name"] == "OpenAI"
    assert TestClient(create_app(log, health=report)).get("/api/status").json()["health"] is True
    bare = TestClient(create_app(log))
    assert bare.get("/api/health").status_code == 501 and bare.get("/api/status").json()["health"] is False


def test_the_backup_says_how_its_last_pass_went_and_its_timer(log):
    from voice_assistant.backup import STATUS_FILE, Status

    timer = states(**{"voice-assistant-backup.timer": "active"})
    assert [(b["name"], b["status"]) for b in health.backup(log.folder, timer, NOW)] == [
        ("The last pass", "warn"),  # none yet
        ("The timer", OK),
    ]
    Status(last_run=NOW - 60, last_ok=NOW - 60, in_bucket=12, only_in_bucket=3).save(log.folder / STATUS_FILE)
    (last, _) = health.backup(log.folder, timer, NOW)
    assert last["status"] == OK and "12 files there, 3 only there" in last["detail"]
    Status(last_run=NOW, last_ok=NOW - 7200, error="S3 put_object: Access Denied").save(log.folder / STATUS_FILE)
    (last,) = health.backup(log.folder, lambda unit: None, NOW)  # no systemd: no timer to ask
    assert last["status"] == FAIL and "Access Denied" in last["detail"] and "journalctl" in last["fix"]


def test_the_backup_is_on_the_status_page_only_when_its_on(log):
    services = Services(list, lambda url: {})
    off = Health(log, ["web"], services, states(), read_throttled=lambda: None).report()
    on = Health(log, ["web"], services, states(), read_throttled=lambda: None, backed_up=True).report()
    assert off["backup"] is None and on["backup"][0]["name"] == "The last pass"
    assert set(on["backup"][0]) == set(health.CheckLine.__annotations__)


def test_keys_that_cant_run_tars_show_as_one_failure_not_a_page_of_nothing(tmp_path):
    from voice_assistant.check import service_checks

    (tmp_path / ".env").write_text("GROQ_API_KEY=g\n")
    cfg = load_config(REPO / "config.toml", local=False)
    found, _ = Services(lambda: service_checks(cfg, tmp_path), lambda url: {}).get()
    assert [(s["name"], s["status"]) for s in found] == [("API keys", FAIL)]
    assert "OPENAI_API_KEY" in found[0]["detail"] and ".env" in found[0]["fix"]
