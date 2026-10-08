"""voice-assistant --check: each check's verdict and what it says to do, and that the run never stops or hangs."""

import time
from pathlib import Path

import numpy as np
import pytest

from voice_assistant import check
from voice_assistant.check import FAIL, OK, SKIP, WARN, Result
from voice_assistant.config import load_config

from .conftest import raising


@pytest.fixture
def cfg():
    return load_config(Path(__file__).parents[1] / "config.toml")  # the repo's: OpenAI, Deepgram and Cerebras


def test_missing_keys_are_named_and_where_to_put_them(cfg, tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-1\nDEEPGRAM_API_KEY=\n")
    r = check.keys(cfg, env)
    assert r.status == FAIL and r.detail == "missing DEEPGRAM_API_KEY, CEREBRAS_API_KEY" and str(env) in r.fix
    env.write_text("OPENAI_API_KEY=a\nDEEPGRAM_API_KEY=b\nCEREBRAS_API_KEY=c\n")
    assert check.keys(cfg, env).status == OK


def test_a_device_is_found_by_name_or_says_how_to_find_it():
    names = ["MacBook Pro Microphone", "Anker PowerConf"]
    assert check.device("input", "powerconf", lambda w, k: 1, names, 0).detail == "Anker PowerConf"
    default = check.device("input", "", lambda w, k: None, names, 0)
    assert default.status == OK and "MacBook Pro Microphone" in default.detail and "input_device" in default.fix
    missing = check.device("input", "jabra", raising(SystemExit("no such device")), names, 0)
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


def test_cerebras_must_have_the_model_config_asks_for():
    class M:
        def __init__(self, id):
            self.id = id

    assert check.cerebras_has([M("qwen"), M("llama")], "qwen") == "qwen"
    with pytest.raises(ValueError, match="no model 'gpt'"):
        check.cerebras_has([M("qwen")], "gpt")


def test_storage_is_written_to_and_low_space_is_flagged(tmp_path):
    assert check.disk(tmp_path / "events", usage=lambda p: (0, 0, 50 * 1024**3)).status == OK
    assert check.disk(tmp_path / "events", usage=lambda p: (0, 0, 1024**3)).status == WARN


def test_voiceprints_web_ui_and_services(tmp_path):
    assert check.voiceprints([]).status == WARN and check.voiceprints(["alon"]).detail == "alon"
    assert check.web(lambda url: 200).status == OK
    assert check.web(raising(ConnectionRefusedError())).status == WARN
    assert check.systemd("voice-assistant", lambda u: None).status == SKIP
    assert check.systemd("voice-assistant", lambda u: "failed").status == WARN


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


def test_deepgram_tells_a_refused_key_from_an_outage():
    assert check.deepgram_status(200, 0.3).status == OK
    refused = check.deepgram_status(401, 0.3)
    assert refused.status == FAIL and "DEEPGRAM_API_KEY" in refused.fix
    assert check.deepgram_status(503, 0.3).detail == "HTTP 503"


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
    with pytest.raises(OSError):
        check.http_status(f"http://127.0.0.1:{server.server_port}/")


def test_echo_cancellation_warns_when_the_devices_delay_is_more_than_it_can_take():
    assert check.echo_delay(0.1, 0.2).status == OK
    late = check.echo_delay(0.3, 0.3)
    assert late.status == WARN and "echo_bench" in late.fix
