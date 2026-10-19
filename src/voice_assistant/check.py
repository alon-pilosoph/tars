"""voice-assistant --check: everything TARS needs, checked in one go, each with what to do about it. For setting up a
new machine (the Pi), and for when something stops working: one command instead of reading the logs.

It says "TARS is ready." through the speaker and listens to the mic for a few seconds. With TARS running as a service
the mic is shared (PipeWire), so it works either way.
"""

import functools
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

from .clustering import MIN_REQUESTS_TO_ENROLL
from .config import (
    CARTESIA_KEY,
    DEEPGRAM_KEY,
    OPENAI_KEY,
    QUICK_PROVIDERS,
    TELEGRAM_KEY,
    WEB_PORT,
    Config,
    ConfigError,
    QuickProvider,
    with_keys,
)
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


def keys(cfg: Config, env: Path) -> Result:
    from .__main__ import env_keys

    have = env_keys(env)
    try:
        _, notes = with_keys(cfg, have)
    except ConfigError as e:
        return Result("API keys", FAIL, str(e.code), f"put them in {env} (see .env.example)")
    if notes:
        return Result("API keys", WARN, "; ".join(notes), f"for all of config.toml's setup, put the keys in {env}")
    used = [OPENAI_KEY, DEEPGRAM_KEY, CARTESIA_KEY, *(provider.key_name for provider in QUICK_PROVIDERS)]
    return Result("API keys", OK, ", ".join(name for name in used if name in have))


def device(kind: str, wanted: str, find: Callable[[str, str], int | None], names: list[str], default: int) -> Result:
    """`names`: every device's name, by index; `default`: the system's default for `kind`."""
    from .audio import NoDeviceError

    name = f"{kind.capitalize()} device"
    try:
        index = find(wanted, kind)
    except SystemExit, NoDeviceError:
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


def telegram(cfg: Config, token: str | None, me: Callable[[str], dict]) -> Result:
    """The bot's token works, and who may chat with it. `me`: the bot Telegram says the token is for."""
    if not cfg.telegram.enabled:
        return Result("Telegram", SKIP, "off ([telegram] enabled)")
    if not token:
        return Result("Telegram", FAIL, f"no {TELEGRAM_KEY}", "make a bot with @BotFather and put its token in .env")
    from .telegram import TelegramError

    t = time.perf_counter()
    try:
        bot = me(token)
    except TelegramError as e:
        if e.status in (401, 404):
            return Result("Telegram", FAIL, "the bot's token was refused", f"check {TELEGRAM_KEY} in .env")
        return Result("Telegram", FAIL, str(e), "check the network")
    people = ", ".join(cfg.telegram.people)
    return Result("Telegram", OK, f"@{bot.get('username')} answers ({time.perf_counter() - t:.1f} s), for {people}")


def systemd(unit: str, active: Callable[[str], str | None]) -> Result:
    state = active(unit)
    if state is None:
        return Result(unit, SKIP, "no systemd here")
    if state != "active":
        return Result(unit, WARN, state, f"systemctl --user start {unit}; journalctl --user-unit {unit} says why")
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


def service_status(name: str, key: str, status: int, took: float) -> Result:
    if status in (401, 403):
        return Result(name, FAIL, f"the API key was refused ({status})", f"check {key} in .env")
    if status != 200:
        return Result(name, FAIL, f"HTTP {status}", f"check the network, and {name}'s status page")
    return Result(name, OK, f"answers ({took:.1f} s)")


def home_assistant(cfg: Config, token: str | None, ping: Callable[[], object]) -> Result:
    """Home Assistant answers, with the token TARS has. `ping`: asks it, raising HomeError."""
    from .home import TOKEN_KEY, HomeError

    if not cfg.home.url:
        return Result("Home Assistant", SKIP, "not used ([home] url is empty)")
    if not token:
        fix = f"in Home Assistant: your profile, Security, Long-lived access tokens; put it in .env as {TOKEN_KEY}"
        return Result("Home Assistant", FAIL, f"no {TOKEN_KEY}", fix)
    t = time.perf_counter()
    try:
        ping()
    except HomeError as e:
        return Result("Home Assistant", FAIL, str(e), f"is it running at {cfg.home.url}? docs/home.md")
    return Result("Home Assistant", OK, f"answers at {cfg.home.url} ({time.perf_counter() - t:.1f} s)")


def house(built, hass_ok: bool) -> list[Result]:
    """Every device in [home], as TARS made it (home.build), and what each says about itself: the entities it reads,
    whether a box or a blaster answers, a profile that was never tried. One that couldn't be made says why."""
    from .hass import Entity
    from .home import HomeError

    results = [Result(name, FAIL, why, "docs/home.md") for name, why in built.unavailable.items()]
    for name, device in (built.tools.devices if built.tools else {}).items():
        if isinstance(device, Entity) and not hass_ok:
            results.append(Result(name, SKIP, "needs Home Assistant"))
            continue
        try:
            found = device.check()
        except HomeError as e:
            found = [(FAIL, str(e), "")]
        results += [Result(name, status, detail, fix) for status, detail, fix in found] or [Result(name, OK, "ready")]
    return results


def check_all(cfg: Config, root: Path) -> int:
    import sounddevice as sd
    from dotenv import dotenv_values

    from . import __main__ as cli
    from .audio import BLOCK_SECONDS, Microphone, Speaker, device_names, find_device
    from .tts import CARTESIA_API, CARTESIA_VERSION

    env = root / ".env"
    have, asked = cli.env_keys(env), cfg
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
        if cfg.stt.provider == "openai" and cfg.tts.provider != "deepgram":
            return Result("Deepgram", SKIP, "not used")
        if not have_keys:
            return Result("Deepgram", SKIP, "needs the API keys")
        key = cli.api_key(env, "DEEPGRAM_API_KEY")
        t = time.perf_counter()
        try:
            host = cfg.stt.deepgram_host if cfg.stt.provider in ("deepgram", "flux") else cfg.tts.deepgram_host
            # /v1/auth/token: the EU servers don't serve /v1/projects (404 with a good key); both answer this one
            status = http_status(f"https://{host}/v1/auth/token", {"Authorization": f"Token {key}"})
        except OSError as e:
            return service_error("Deepgram", e)
        return service_status("Deepgram", DEEPGRAM_KEY, status, time.perf_counter() - t)

    def cartesia():
        if cfg.tts.provider != "cartesia":
            return Result("Cartesia", SKIP, "not used")
        key = cli.api_key(env, CARTESIA_KEY)
        t = time.perf_counter()
        try:
            status = http_status(
                f"{CARTESIA_API}/voices?limit=1", {"X-API-Key": key, "Cartesia-Version": CARTESIA_VERSION}
            )
        except OSError as e:
            return service_error("Cartesia", e)
        return service_status("Cartesia", CARTESIA_KEY, status, time.perf_counter() - t)

    def bot_me(token: str) -> dict:
        from .telegram import Bot

        return Bot(token).me()

    def secret(name: str) -> str | None:
        return dotenv_values(env).get(name) or None

    @functools.cache
    def made_house():
        """The house as TARS makes it: once, for both lines below."""
        from .home import Context, build

        context = Context(root, secret, announce=lambda line: None)
        context.tv_agent = functools.partial(cli.make_tv_agent, cfg, env, context.announce)
        return build(cfg.home, context), context

    @functools.cache
    def hass_check():
        from .home import TOKEN_KEY

        if not cfg.home.url:
            return home_assistant(cfg, None, lambda: None)
        _, context = made_house()
        return home_assistant(cfg, secret(TOKEN_KEY), lambda: context.hass.ping())

    def devices():
        if not cfg.home.devices:
            return Result("The house", SKIP, "no devices ([[home.devices]] in config.toml)")
        built, _ = made_house()
        return within(60, "The house", lambda: house(built, hass_check().status == OK), "a device didn't answer", "")

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
        ("API keys", lambda: keys(asked, env)),
        ("Input device", lambda: audio_device("input")),
        ("Output device", lambda: audio_device("output")),
        ("Local models", models),
        ("Echo cancellation", echo),
        ("Voiceprints", speakers),
        ("OpenAI", openai),
        *((provider.name, lambda provider=provider: quick(provider)) for provider in QUICK_PROVIDERS),
        ("Deepgram", deepgram),
        ("Cartesia", cartesia),
        ("Voice", speaking),
        ("Microphone", listen),
        ("Storage", lambda: disk(root / cfg.learning.folder)),
        ("Web UI", lambda: web(http_status)),
        ("Home Assistant", hass_check),
        ("The house", devices),
        ("Telegram", lambda: telegram(cfg, dotenv_values(env).get(TELEGRAM_KEY), bot_me)),
        ("voice-assistant", lambda: systemd("voice-assistant", systemd_state)),
        ("voice-assistant-web", lambda: systemd("voice-assistant-web", systemd_state)),
        *(
            [("voice-assistant-telegram", lambda: systemd("voice-assistant-telegram", systemd_state))]
            if cfg.telegram.enabled
            else []
        ),
    ]
    print("Checking TARS...\n", flush=True)
    results = run(checks)
    print(f"\n{summary(results)}", flush=True)
    if any(t.name.startswith("check-") for t in threading.enumerate()):
        os._exit(exit_code(results))  # a device still blocked would hold up the exit
    return exit_code(results)
