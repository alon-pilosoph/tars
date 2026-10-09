"""voice-assistant --check: everything TARS needs, checked in one go, each with what to do about it. For setting up a
new machine (the Pi), and for when something stops working: one command instead of reading the logs.

It says "TARS is ready." through the speaker and listens to the mic for a few seconds. With TARS running as a service
the mic is shared (PipeWire), so it works either way.
"""

import os
import shutil
import subprocess
import threading
import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from dotenv import dotenv_values

from .clustering import MIN_REQUESTS_TO_ENROLL
from .config import DEEPGRAM_KEY, OPENAI_KEY, QUICK_PROVIDERS, WEB_PORT, Config, ConfigError, QuickProvider, with_keys
from .echo import MAX_DELAY_S

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
MARKS = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "-"}
LISTEN_S = 3.0
QUIET_DBFS = -55.0  # a peak below this, with someone talking, is a mic that isn't the one in the room
LOW_DISK_BYTES = 2 * 1024**3
WEB_URL = f"http://127.0.0.1:{WEB_PORT}/api/status"
MODELS_S = 300.0  # long enough to download every local model on a slow line, the first time
READY = "TARS is ready."


@dataclass
class Result:
    name: str
    status: str
    detail: str
    fix: str = ""


Check = Callable[[], Result | Iterable[Result]]


def run(checks: list[tuple[str, Check]]) -> list[Result]:
    """Every check, in order. One that raises is a failure with the error, never the end of the run."""
    results = []
    for name, check in checks:
        try:
            got = check()
        except (Exception, SystemExit) as e:  # noqa: BLE001 - reported, and the rest still run (a bad setting exits)
            got = Result(name, FAIL, str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}")
            if not isinstance(e, OSError | RuntimeError | ValueError | SystemExit):
                traceback.print_exc()
        for r in [got] if isinstance(got, Result) else got:
            print(line(r), flush=True)
            results.append(r)
    return results


def line(r: Result) -> str:
    text = f"{MARKS[r.status]} {r.name}: {r.detail}"
    return f"{text}\n    → {r.fix}" if r.fix else text


def exit_code(results: list[Result]) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0


def summary(results: list[Result]) -> str:
    failed = sum(r.status == FAIL for r in results)
    warned = sum(r.status == WARN for r in results)
    look = f"{warned} thing{'s' if warned != 1 else ''} to look at"
    if failed:
        fix = f"{failed} problem{'s' if failed != 1 else ''} to fix"
        return f"{fix}, and {look}." if warned else f"{fix}."
    return f"Everything works, with {look}." if warned else "Everything works."


def present_keys(env: Path) -> set[str]:
    return {name for name, key in dotenv_values(env).items() if key} if env.exists() else set()


def keys(cfg: Config, env: Path) -> Result:
    have = present_keys(env)
    try:
        _, notes = with_keys(cfg, have)
    except ConfigError as e:
        return Result("API keys", FAIL, str(e.code), f"put them in {env} (see .env.example)")
    if notes:
        return Result("API keys", WARN, "; ".join(notes), f"for all of config.toml's setup, put the keys in {env}")
    used = [OPENAI_KEY, DEEPGRAM_KEY, *(provider.key_name for provider in QUICK_PROVIDERS)]
    return Result("API keys", OK, ", ".join(name for name in used if name in have))


def device(kind: str, wanted: str, find: Callable[[str, str], int | None], names: list[str], default: int) -> Result:
    """`names`: every device's name, by index; `default`: the system's default for `kind`."""
    name = f"{kind.capitalize()} device"
    try:
        index = find(wanted, kind)
    except SystemExit:
        return Result(name, FAIL, f"nothing matches {wanted!r}", "run --list-devices and fix [audio] in config.toml")
    if index is None:
        return Result(
            name,
            OK,
            f"the system default, {names[default] if 0 <= default < len(names) else 'none'}",
            f"if that isn't the speakerphone, set [audio] {kind}_device (--list-devices)",
        )
    return Result(name, OK, names[index])


def mic_level(read: Callable[[], np.ndarray], blocks: int) -> Result:
    """`read` gives the mic's next block; `blocks` of them make LISTEN_S."""
    audio = np.concatenate([read() for _ in range(blocks)]).astype(np.float32)
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak == 0:
        return Result(
            "Microphone",
            FAIL,
            "only silence, not even room noise",
            "it's muted, or this program isn't allowed to use it (macOS: System Settings → Privacy → Microphone)",
        )
    dbfs = 20 * np.log10(peak / 32768)
    if dbfs < QUIET_DBFS:
        return Result(
            "Microphone",
            WARN,
            f"very quiet (peak {dbfs:.0f} dBFS)",
            "if you were talking, it may be the wrong mic: check [audio] input_device; --mic-test shows it live",
        )
    return Result("Microphone", OK, f"hears the room (peak {dbfs:.0f} dBFS)")


def within(
    seconds: float, name: str, check: Callable[[], Result | list[Result]], why: str, fix: str
) -> Result | list[Result]:
    """`check`, given `seconds` at most. Opening a device can block for good (a permission prompt that can't show,
    a driver that's stuck), and a check must never hang."""
    done: list[Result | list[Result] | BaseException] = []

    def go():
        try:
            done.append(check())
        except BaseException as e:  # noqa: BLE001 - handed back below
            done.append(e)

    worker = threading.Thread(target=go, daemon=True, name=f"check-{name}")
    worker.start()
    worker.join(seconds)
    if not done:
        return Result(name, FAIL, f"{why} ({seconds:.0f} s)", fix)
    if isinstance(done[0], BaseException):
        raise done[0]
    return done[0]


def voice(stream: Callable[[str], Iterable[bytes]], play: Callable[[list[bytes]], None]) -> list[Result]:
    t = time.perf_counter()
    audio = list(stream(READY))
    took = time.perf_counter() - t
    results = [Result("Voice", OK, f'made "{READY}" in {took:.1f} s')]
    play(audio)
    results.append(
        Result("Speaker", OK, f'played "{READY}": you should have heard it', "if not, check [audio] output_device")
    )
    return results


def timed(name: str, call: Callable[[], str | None]) -> Result:
    """A service answering: `call` returns a detail to add, or raises."""
    t = time.perf_counter()
    extra = call()
    took = time.perf_counter() - t
    return Result(name, OK, f"answers ({took:.1f} s){f', {extra}' if extra else ''}")


def service_error(name: str, e: Exception) -> Result:
    status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
    if status == 401:
        return Result(name, FAIL, "the API key was refused (401)", "check the key in .env")
    if status == 404:
        return Result(name, FAIL, "the model isn't available to this key (404)", "check the model in config.toml")
    return Result(name, FAIL, f"{type(e).__name__}: {e}", "check the network, and the service's status page")


def has_model(models: Iterable, wanted: str) -> str:
    ids = [m.id for m in models]
    if wanted not in ids:
        raise ValueError(f"no model {wanted!r} here (it has: {', '.join(ids[:8])})")
    return wanted


def echo_delay(mic_s: float, speaker_s: float) -> Result:
    delay = mic_s + speaker_s
    room = "check it in the room: uv run python tools/echo_bench.py"
    if delay > MAX_DELAY_S:
        why = f"the devices' delay ({delay:.2f} s) is more than the canceller can take ({MAX_DELAY_S:.1f} s)"
        return Result("Echo cancellation", WARN, why, f"expect echo; {room}")
    return Result("Echo cancellation", OK, f"loaded, devices' delay {delay:.2f} s; {room}")


def disk(folder: Path, usage: Callable[[Path], tuple] = shutil.disk_usage) -> Result:
    folder.mkdir(parents=True, exist_ok=True)
    probe = folder / ".check"
    probe.write_text("ok")
    probe.unlink()
    free = usage(folder)[2]
    gb = free / 1024**3
    if free < LOW_DISK_BYTES:
        return Result("Storage", WARN, f"{folder} is writable, {gb:.1f} GB free", "free some space: audio goes there")
    return Result("Storage", OK, f"{folder} is writable, {gb:.0f} GB free")


def voiceprints(names: list[str]) -> Result:
    if not names:
        return Result(
            "Voiceprints",
            WARN,
            "none yet, so TARS can't greet anyone by name",
            f"name your voice in the web UI's Voices tab after {MIN_REQUESTS_TO_ENROLL} requests, or --record-voice "
            "NAME then --enroll NAME",
        )
    return Result("Voiceprints", OK, ", ".join(names))


def web(get: Callable[[str], int]) -> Result:
    try:
        status = get(WEB_URL)
    except Exception:  # noqa: BLE001 - refused, timed out, not HTTP: not running, as far as TARS can tell
        status = None
    if status != 200:
        return Result(
            "Web UI",
            WARN,
            "not running here",
            "systemctl --user start voice-assistant-web on the Pi, or voice-assistant --web",
        )
    return Result("Web UI", OK, f"running on port {WEB_PORT}")


def systemd(unit: str, active: Callable[[str], str | None]) -> Result:
    state = active(unit)
    if state is None:
        return Result(unit, SKIP, "no systemd here")
    if state != "active":
        return Result(unit, WARN, state, f"systemctl --user start {unit}; journalctl --user -u {unit} says why")
    return Result(unit, OK, "running")


def systemd_state(unit: str) -> str | None:
    if not shutil.which("systemctl"):
        return None
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", unit], capture_output=True, text=True, check=False, timeout=5
        )
    except subprocess.TimeoutExpired:
        return "not answering"
    return out.stdout.strip() or "unknown"


def http_status(url: str, headers: dict | None = None) -> int:
    """The HTTP status, whatever it is; OSError if nothing answered."""
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    try:
        with urlopen(Request(url, headers=headers or {}), timeout=5) as response:
            return response.status
    except HTTPError as e:
        return e.code


def deepgram_status(status: int, took: float) -> Result:
    if status in (401, 403):
        return Result("Deepgram", FAIL, f"the API key was refused ({status})", "check DEEPGRAM_API_KEY in .env")
    if status != 200:
        return Result("Deepgram", FAIL, f"HTTP {status}", "check the network, and Deepgram's status page")
    return Result("Deepgram", OK, f"answers ({took:.1f} s)")


def check_all(cfg: Config, root: Path) -> int:
    import sounddevice as sd

    from . import __main__ as cli
    from .audio import BLOCK_SECONDS, Microphone, Speaker, device_names, find_device

    env = root / ".env"
    have = present_keys(env)
    try:
        cfg, _ = with_keys(cfg, have)
        have_keys = True
    except ConfigError:
        have_keys = False

    def audio_device(kind: str):
        names, default_in, default_out = device_names()
        wanted = cfg.audio.input_device if kind == "input" else cfg.audio.output_device
        return device(kind, wanted, find_device, names, default_in if kind == "input" else default_out)

    def listen():
        print(f"(listening {LISTEN_S:.0f} s: say something)", flush=True)

        def measure():
            with Microphone(find_device(cfg.audio.input_device, "input")) as mic:
                return mic_level(mic.read, round(LISTEN_S / BLOCK_SECONDS))

        return within(
            LISTEN_S + 7,
            "Microphone",
            measure,
            "it didn't open, or sent no audio",
            "it may be waiting for permission (macOS: run it from a terminal you can see, and allow the microphone), "
            "or in use by something that won't share it",
        )

    def speaking():
        if not have_keys:
            return Result("Voice", SKIP, "needs the API keys")
        _, _, tts = cli.make_pipeline(cfg, root, typed=True)
        out = find_device(cfg.audio.output_device, "output")

        def say():
            with Speaker(out, tts.sample_rate, cfg.audio.playback_prebuffer_s) as speaker:
                return voice(tts.stream, lambda audio: speaker.play_pcm_stream(iter(audio), tts.sample_rate))

        return within(30, "Voice", say, "no voice in time", "check the network, and [audio] output_device")

    def openai():
        if OPENAI_KEY not in have:
            return Result("OpenAI", SKIP, f"not used (no {OPENAI_KEY})")
        if not have_keys:
            return Result("OpenAI", SKIP, "needs the API keys")
        try:
            return timed("OpenAI", lambda: cli.make_openai_client(env).models.retrieve(cfg.llm.model).id)
        except Exception as e:  # noqa: BLE001 - what went wrong is the result
            return service_error("OpenAI", e)

    def quick(provider: QuickProvider):
        model = getattr(cfg.llm, provider.setting)
        if not model:
            why = f"no {provider.key_name}" if provider.key_name not in have else f"[llm] {provider.setting} is empty"
            return Result(provider.name, SKIP, f"not used ({why})")
        if not have_keys:
            return Result(provider.name, SKIP, "needs the API keys")
        try:
            client = cli.make_quick_client(env, provider)
            return timed(provider.name, lambda: has_model(client.models.list(), model))
        except Exception as e:  # noqa: BLE001
            return service_error(provider.name, e)

    def deepgram():
        if "openai" == cfg.stt.provider == cfg.tts.provider:
            return Result("Deepgram", SKIP, "not used")
        if not have_keys:
            return Result("Deepgram", SKIP, "needs the API keys")
        key = cli.api_key(env, "DEEPGRAM_API_KEY")
        t = time.perf_counter()
        try:
            host = cfg.stt.deepgram_host if cfg.stt.provider in ("deepgram", "flux") else cfg.tts.deepgram_host
            status = http_status(f"https://{host}/v1/projects", {"Authorization": f"Token {key}"})
        except OSError as e:
            return service_error("Deepgram", e)
        return deepgram_status(status, time.perf_counter() - t)

    def models():
        def load():
            t = time.perf_counter()
            cli.make_trigger(cfg, False, root)
            cli.make_recorder(cfg, root)
            took = time.perf_counter() - t
            return Result("Local models", OK, f"wake word, double-check and speech detectors loaded ({took:.0f} s)")

        why = "still not loaded (a download that stalled?)"
        return within(MODELS_S, "Local models", load, why, "check the network and run it again")

    def echo():
        if not cfg.audio.echo_cancel:
            return Result("Echo cancellation", SKIP, "off ([audio] echo_cancel)")
        try:
            cli.load_echo_canceller()
        except Exception as e:  # noqa: BLE001 - what went wrong is the result
            return Result("Echo cancellation", FAIL, f"can't start: {e}", "or set [audio] echo_cancel = false")
        return echo_delay(device_delay("input"), device_delay("output"))

    def device_delay(kind: str) -> float:
        wanted = cfg.audio.input_device if kind == "input" else cfg.audio.output_device
        return sd.query_devices(find_device(wanted, kind), kind)[f"default_high_{kind}_latency"]

    def speakers():
        if not cfg.speaker.enabled:
            return Result("Voiceprints", SKIP, "speaker ID is off")
        why = "the speaker model didn't load in time (a download that stalled?)"
        return within(
            MODELS_S,
            "Voiceprints",
            lambda: voiceprints(cli.make_speaker_id(cfg, root).names()),
            why,
            "check the network and run it again",
        )

    checks: list[tuple[str, Check]] = [
        ("API keys", lambda: keys(cfg, env)),
        ("Input device", lambda: audio_device("input")),
        ("Output device", lambda: audio_device("output")),
        ("Local models", models),
        ("Echo cancellation", echo),
        ("Voiceprints", speakers),
        ("OpenAI", openai),
        *((provider.name, lambda provider=provider: quick(provider)) for provider in QUICK_PROVIDERS),
        ("Deepgram", deepgram),
        ("Voice", speaking),
        ("Microphone", listen),
        ("Storage", lambda: disk(root / cfg.learning.folder)),
        ("Web UI", lambda: web(http_status)),
        ("voice-assistant", lambda: systemd("voice-assistant", systemd_state)),
        ("voice-assistant-web", lambda: systemd("voice-assistant-web", systemd_state)),
    ]
    print("Checking TARS...\n", flush=True)
    results = run(checks)
    print(f"\n{summary(results)}", flush=True)
    if any(t.name.startswith("check-") for t in threading.enumerate()):
        os._exit(exit_code(results))  # a device still blocked would hold up the exit
    return exit_code(results)
