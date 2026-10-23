"""voice-assistant --check: each check's verdict and what it says to do, and that the run never stops or hangs."""

import time
from pathlib import Path

import httpx
import numpy as np
import pytest

from voice_assistant import check
from voice_assistant.audio import NoDeviceError
from voice_assistant.check import FAIL, OK, SKIP, WARN, Result
from voice_assistant.config import ConfigError, load_config

from .conftest import raising
from .test_presence import FakeHomeware, FakeXiaomi, presence_config


@pytest.fixture
def cfg():
    # The repo's, as shipped: OpenAI, Deepgram, Groq and Cerebras.
    return load_config(Path(__file__).parents[1] / "config.toml", local=False)


def test_missing_keys_say_what_runs_instead_and_where_to_put_them(cfg, tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-1\nDEEPGRAM_API_KEY=\n")
    r = check.keys(cfg, env)
    assert r.status == WARN and str(env) in r.fix
    assert "no DEEPGRAM_API_KEY" in r.detail and "no GROQ_API_KEY" in r.detail
    env.write_text("GROQ_API_KEY=g\n")
    assert check.keys(cfg, env).status == FAIL
    env.write_text("OPENAI_API_KEY=a\nDEEPGRAM_API_KEY=b\nGROQ_API_KEY=g\nCEREBRAS_API_KEY=c\nCARTESIA_API_KEY=d\n")
    assert check.keys(cfg, env).status == OK


def test_a_device_is_found_by_name_or_says_how_to_find_it():
    names = ["MacBook Pro Microphone", "Anker PowerConf"]
    assert check.device("input", "powerconf", lambda w, k: 1, names, 0).detail == "Anker PowerConf"
    default = check.device("input", "", lambda w, k: None, names, 0)
    assert default.status == OK and "MacBook Pro Microphone" in default.detail and "input_device" in default.fix
    missing = check.device("input", "jabra", raising(NoDeviceError("no such device")), names, 0)
    assert missing.status == FAIL and "--list-devices" in missing.fix


@pytest.mark.parametrize(
    "level, status, words",
    [(0, FAIL, "silence"), (20, WARN, "very quiet"), (3000, OK, "hears the room")],
)
def test_the_mic_level_tells_muted_from_quiet_from_working(level, status, words):
    block = np.full(1280, level, dtype=np.int16)
    r = check.mic_level(lambda: block, blocks=3)
    assert r.status == status and words in r.detail


def test_a_device_that_never_opens_fails_in_time_instead_of_hanging():
    t = time.perf_counter()
    r = check.within(0.2, "Microphone", lambda: time.sleep(5), "it didn't open", "allow the microphone")
    assert r.status == FAIL and "it didn't open" in r.detail and time.perf_counter() - t < 1


def test_a_service_says_whether_the_key_or_the_model_is_wrong():
    class HTTPError(Exception):
        def __init__(self, status):
            super().__init__(f"HTTP {status}")
            self.status_code = status

    assert "key was refused" in check.service_error("OpenAI", HTTPError(401)).detail
    assert "model isn't available" in check.service_error("OpenAI", HTTPError(404)).detail
    assert check.timed("OpenAI", lambda: "gpt-x").detail.startswith("answers (")


def test_a_quick_service_must_have_the_model_config_asks_for():
    class M:
        def __init__(self, id):
            self.id = id

    assert check.has_model([M("qwen"), M("llama")], "qwen") == "qwen"
    with pytest.raises(ValueError, match="no model 'gpt'"):
        check.has_model([M("qwen")], "gpt")


def test_storage_is_written_to_and_low_space_is_flagged(tmp_path):
    assert check.disk(tmp_path / "events", usage=lambda p: (0, 0, 50 * 1024**3)).status == OK
    assert check.disk(tmp_path / "events", usage=lambda p: (0, 0, 1024**3)).status == WARN


def test_voiceprints_web_ui_and_services(tmp_path):
    assert check.voiceprints([]).status == WARN and check.voiceprints(["alon"]).detail == "alon"
    assert check.web(lambda url: 200).status == OK
    assert check.web(raising(ConnectionRefusedError())).status == WARN
    assert check.systemd("voice-assistant", lambda u: None).status == SKIP
    assert check.systemd("voice-assistant", lambda u: "failed").status == WARN


def test_the_web_ui_is_looked_for_on_the_port_its_unit_serves_on(tmp_path):
    unit = tmp_path / "voice-assistant-web.service"

    def no_systemd(unit):
        return ""

    assert check.web_port(no_systemd, unit) == 8080  # not installed: --web's own
    shipped = (Path(__file__).parents[1] / "deploy" / "voice-assistant-web.service").read_text()
    unit.write_text(shipped.replace("--port 8080", "--port 8443"))
    assert check.web_port(no_systemd, unit) == 8443
    shown = "ExecStart={ path=/home/tars/.local/bin/uv ; argv[]=uv run voice-assistant --web --port 9000 ; }\n"
    assert check.web_port(lambda unit: shown, unit) == 9000  # a drop-in changed it: systemd's word wins
    asked = []
    assert check.web(lambda url: asked.append(url) or 200, 9000).detail == "running on port 9000"
    assert asked == ["http://127.0.0.1:9000/api/status"]


def test_the_voice_is_made_then_played(tmp_path):
    played = []
    results = check.voice(lambda text: iter([text.encode()]), played.extend)
    assert [r.name for r in results] == ["Voice", "Speaker"] and played == [check.READY.encode()]


def test_a_check_that_breaks_or_exits_is_a_failure_and_the_rest_still_run(capsys):
    results = check.run(
        [
            ("Broken", raising(OSError("disk"))),
            ("Bad setting", raising(SystemExit("no input device matching 'x'"))),
            ("Fine", lambda: Result("Fine", OK, "yes")),
            ("Two", lambda: [Result("A", OK, "a"), Result("B", WARN, "b", "do this")]),
        ]
    )
    assert [(r.name, r.status) for r in results] == [
        ("Broken", FAIL),
        ("Bad setting", FAIL),
        ("Fine", OK),
        ("A", OK),
        ("B", WARN),
    ]
    assert results[1].detail == "no input device matching 'x'"
    out = capsys.readouterr().out
    assert "✗ Broken: OSError: disk" in out and "! B: b\n    → do this" in out
    assert check.exit_code(results) == 1 and check.summary(results) == "2 problems to fix, and 1 thing to look at."
    assert check.exit_code(results[2:]) == 0 and check.summary(results[2:4]) == "Everything works."


@pytest.mark.parametrize("name, key", [("Deepgram", "DEEPGRAM_API_KEY"), ("Cartesia", "CARTESIA_API_KEY")])
def test_a_voice_service_tells_a_refused_key_from_an_outage(name, key):
    assert check.service_status(name, key, 200, 0.3).status == OK
    refused = check.service_status(name, key, 401, 0.3)
    assert refused.status == FAIL and key in refused.fix
    outage = check.service_status(name, key, 503, 0.3)
    assert outage.detail == "HTTP 503" and name in outage.fix


def test_http_status_gives_any_status_and_raises_when_nothing_answers():
    import http.server
    import threading

    class Refuse(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Refuse)
    threading.Thread(target=server.handle_request, daemon=True).start()
    assert check.http_status(f"http://127.0.0.1:{server.server_port}/") == 401
    server.server_close()
    with pytest.raises(httpx.TransportError):
        check.http_status(f"http://127.0.0.1:{server.server_port}/")


def test_echo_cancellation_warns_when_the_devices_delay_is_more_than_it_can_take():
    assert check.echo_delay(0.1, 0.2).status == OK
    late = check.echo_delay(0.3, 0.3)
    assert late.status == WARN and "echo_bench" in late.fix


def test_telegram_is_checked_only_when_its_on_and_says_who_its_for(cfg):
    import dataclasses

    from voice_assistant.config import TelegramConfig
    from voice_assistant.telegram import TelegramError

    off = dataclasses.replace(cfg, telegram=TelegramConfig())
    assert check.telegram(off, "t", raising(AssertionError("not asked"))).status == SKIP
    on = dataclasses.replace(cfg, telegram=TelegramConfig(enabled=True, people={"Alon": 1}))
    assert check.telegram(on, None, raising(AssertionError("not asked"))).status == FAIL
    refused = check.telegram(on, "t", raising(TelegramError("getMe: Unauthorized", 401)))
    assert refused.status == FAIL and "TELEGRAM_BOT_TOKEN" in refused.fix
    ok = check.telegram(on, "t", lambda token: {"username": "hey_tars_bot"})
    assert ok.status == OK and "@hey_tars_bot" in ok.detail and "Alon" in ok.detail


def test_the_backup_says_how_its_last_pass_went(cfg, log, convos, tmp_path):
    import dataclasses

    from voice_assistant.backup import S3, Backup, Status
    from voice_assistant.config import BackupConfig

    from .test_backup import NOW, PretendS3, a_request

    on = dataclasses.replace(cfg, backup=BackupConfig(enabled=True, bucket="tars-home", region="eu-central-1"))
    assert check.backup(cfg, raising(AssertionError("not asked")))[0].status == SKIP
    bucket = PretendS3()
    job = Backup(log, on.backup, S3(bucket, "tars-home"), clock=lambda: NOW)
    (never,) = check.backup(on, lambda: job, now=NOW)
    assert never.status == WARN and "no pass has run yet" in never.detail
    a_request(log, convos, NOW)
    job.run()
    (fine,) = check.backup(on, lambda: job, now=NOW + 60)
    assert fine.status == OK and "s3://tars-home/tars answers" in fine.detail and "2 files there" in fine.detail
    assert check.backup(on, lambda: job, now=NOW + 3 * 3600)[0].status == WARN  # no good pass for hours
    Status(last_run=NOW, last_ok=NOW - 60, error="S3 put_object: Access Denied").save(job.status_path)
    (failed,) = check.backup(on, lambda: job, now=NOW)
    assert failed.status == FAIL
    assert failed.detail.startswith("s3://tars-home/tars answers (")
    assert failed.detail.endswith("; the last pass failed: S3 put_object: Access Denied")
    bucket.down = True
    assert check.backup(on, lambda: job, now=NOW)[0].status == FAIL
    assert check.backup(on, raising(ConfigError("The backup: put AWS_ACCESS_KEY_ID in .env")))[0].status == FAIL


def test_the_service_checks_skip_what_has_no_key(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=o\n")
    cfg = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    checks = check.service_checks(cfg, tmp_path).listed()
    names = [
        "OpenAI",
        "Groq",
        "Cerebras",
        "Deepgram",
        "Cartesia",
        "Telegram",
        "Telegram reminders",
        "Home Assistant",
        "Who's home",
    ]
    assert [name for name, _ in checks] == names
    assert all(c().status == SKIP for name, c in checks if name != "OpenAI")  # no key for them, or not used


def test_keys_that_cant_run_tars_are_one_failure_in_place_of_the_services(tmp_path):
    (tmp_path / ".env").write_text("GROQ_API_KEY=g\n")
    cfg = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    services = check.service_checks(cfg, tmp_path)
    assert services.keyed == [] and [name for name, _ in services.listed()] == [
        "API keys",
        "Telegram",
        "Telegram reminders",
        "Home Assistant",
        "Who's home",
    ]
    keys = services.keys()
    assert keys.status == FAIL and "DEEPGRAM_API_KEY or OPENAI_API_KEY" in keys.detail


def test_home_assistant_is_one_of_the_services_when_home_uses_it(tmp_path):
    from voice_assistant.config import HomeConfig

    (tmp_path / ".env").write_text("OPENAI_API_KEY=o\n")
    assert check.service_checks(cfg_with(HomeConfig()), tmp_path).home_assistant().status == SKIP
    on = cfg_with(HomeConfig(url="http://ha.invalid:8123"))
    hass = check.service_checks(on, tmp_path).home_assistant()
    assert hass.status == FAIL and "HOME_ASSISTANT_TOKEN" in hass.detail


def test_reminders_waiting_for_telegram_are_counted_while_nothing_posts_them(log):
    import dataclasses

    from voice_assistant.config import TelegramConfig
    from voice_assistant.health import Heartbeat
    from voice_assistant.reminders import REMINDER, TELEGRAM, NewReminder, Reminders

    base = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    on = dataclasses.replace(base, telegram=TelegramConfig(enabled=True, people={"Alon": 1}))
    now = time.time()
    assert check.telegram_waiting(on, log.folder, now).status == SKIP  # none
    reminders = Reminders(log.store)
    for minutes in (10, 1):  # one long overdue, one only just due
        new = NewReminder(REMINDER, "call mum", "alon", due=now - minutes * 60)
        reminders.add(new, TELEGRAM, channel=TELEGRAM, now=now - minutes * 60 - 1)
    stopped = check.telegram_waiting(on, log.folder, now)
    assert stopped.status == FAIL and stopped.detail == "1 waiting to be posted: Telegram isn't running"
    off = check.telegram_waiting(dataclasses.replace(on, telegram=TelegramConfig()), log.folder, now)
    assert off.status == WARN and "[telegram] off" in off.detail
    Heartbeat(log.folder, "telegram").beat()
    assert check.telegram_waiting(on, log.folder, time.time()).status == SKIP  # it's running: it'll post them


def test_home_assistant_is_checked_only_when_its_used_and_says_what_to_do():
    from voice_assistant.config import HomeConfig
    from voice_assistant.home import HomeError

    off = check.home_assistant(cfg_with(HomeConfig()), "t", raising(AssertionError("not asked")))
    assert off.status == SKIP
    on = cfg_with(HomeConfig(url="http://ha:8123"))
    assert "Long-lived access tokens" in check.home_assistant(on, None, lambda: None).fix
    refused = check.home_assistant(on, "t", raising(HomeError("Home Assistant refused the token")))
    assert refused.status == FAIL and "running at http://ha:8123" in refused.fix
    assert check.home_assistant(on, "t", lambda: "API running.").status == OK


def cfg_with(home):
    import dataclasses

    from voice_assistant.config import load_config

    return dataclasses.replace(load_config(Path(__file__).parents[1] / "config.toml", local=False), home=home)


def test_the_house_says_each_device_as_it_was_made_and_what_it_found():
    from evals.house import make_house
    from voice_assistant.home import Built

    h = make_house({"lock": "locked"})
    results = check.house(Built(h.tools, {"garage": "the plugin garden didn't load: oops"}), hass_ok=True)
    by_name = {r.name: r for r in results}
    assert by_name["garage"].status == FAIL and "didn't load" in by_name["garage"].detail
    assert by_name["front door"].status == OK and "lock.front_door locked" in by_name["front door"].detail
    assert by_name["tv"].status == OK and "it does tasks with pretend" in by_name["tv"].detail
    without = {r.name: r.status for r in check.house(Built(h.tools, {}), hass_ok=False)}
    assert without["front door"] == SKIP and without["tv"] == OK  # the box needs no Home Assistant


# Who's home: --check signs into each router; the Status page goes by how they last answered the assistant.

TWO_ROUTERS = (
    '[presence]\nenabled = true\npeople = { Alon = ["ca:fe:00:00:00:01"] }\n'
    '[[presence.routers]]\nkind = "technicolor"\naddress = "192.168.1.1"\nsecret = "ROUTER_PASSWORD"\n'
    '[[presence.routers]]\nkind = "xiaomi"\naddress = "192.168.1.160"\nsecret = "XIAOMI_PASSWORD"\n'
)


def test_check_signs_into_each_router_and_says_whose_phone_it_sees(tmp_path):
    from voice_assistant.routers import make

    fakes = {"technicolor": FakeHomeware(), "xiaomi": FakeXiaomi()}
    results = check.routers_now(
        presence_config(tmp_path, TWO_ROUTERS),
        {"ROUTER_PASSWORD": "hunter2!"}.get,
        lambda spec, password: make(spec, password, httpx.MockTransport(fakes[spec["kind"]])),
    )
    assert [(r.name, r.status, r.detail) for r in results] == [
        ("Router 192.168.1.1", OK, "3 devices connected; sees the phone of Alon"),
        ("Router 192.168.1.160", FAIL, "no XIAOMI_PASSWORD in .env"),
    ]
    assert check.routers_now(cfg_(), {}.get).status == SKIP


def cfg_():
    return load_config(Path(__file__).parents[1] / "config.toml", local=False)


def test_the_status_page_goes_by_how_the_routers_last_answered_the_assistant(tmp_path):
    from voice_assistant.presence import Board, People, RouterStatus, State
    from voice_assistant.store import Store

    cfg, folder, now = presence_config(tmp_path, TWO_ROUTERS), tmp_path / "events", 1_800_000_000.0
    assert check.routers_last(cfg, folder, now).status == WARN  # no event log yet: asked about, never made
    assert not folder.exists()
    board = Board(Store(folder), People(cfg.presence.people))
    routers = [
        RouterStatus("192.168.1.1", "technicolor", ok_at=now, connected=9),
        RouterStatus("192.168.1.160", "xiaomi", ok_at=now - 600, error="no answer", error_at=now - 30),
    ]
    board.write({"alon": State(True, None, now)}, routers, now)
    assert [(r.status, r.detail) for r in check.routers_last(cfg, folder, now + 30)] == [
        (OK, "9 devices connected, just now"),
        (FAIL, "no answer"),
        (OK, "Alon: home"),
    ]
    # Hours later, with the assistant stopped: nothing is known now, failing or not.
    later = check.routers_last(cfg, folder, now + 3 * 3600)
    assert [r.status for r in later] == [WARN, WARN, OK] and later[-1].detail == "Alon: not known yet"
    assert check.routers_last(cfg_(), folder).status == SKIP


@pytest.mark.parametrize(("state", "held"), [("active", True), ("failed", False), ("inactive", False), (None, False)])
def test_a_device_that_wont_open_is_the_running_assistants_not_broken(state, held):
    asked = []

    def state_of(unit):
        asked.append(unit)
        return state

    result = check.assistant_has_it("Microphone", state_of)
    assert asked == ["voice-assistant"]
    if held:  # on Linux one program at a time has it, and the service only runs with it open
        assert result.status == OK and "running assistant" in result.detail
    else:  # nothing has it: the device's own failure stands
        assert result is None
