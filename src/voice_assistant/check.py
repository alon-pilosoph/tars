"""voice-assistant --check: everything TARS needs, checked in one go, each with what to do about it. For setting up a
new machine (the Pi), and for when something stops working: one command instead of reading the logs.

It says "TARS is ready." through the speaker and listens to the mic for a few seconds. With TARS running as a service
the mic is shared (PipeWire), so it works either way.
"""

import functools
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from dotenv import dotenv_values

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
from .errors import SERVICE_FAILURES

if TYPE_CHECKING:
    from .backup import Backup
    from .backup import Status as BackupStatus

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
MARKS = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "-"}
LISTEN_S = 3.0
QUIET_DBFS = -55.0  # a peak below this, with someone talking, is a mic that isn't the one in the room
LOW_DISK_BYTES = 2 * 1024**3
WEB_UNIT = Path.home() / ".config/systemd/user/voice-assistant-web.service"  # where deploy/install-pi.sh puts it
MODELS_S = 300.0  # long enough to download every local model on a slow line, the first time
READY = "TARS is ready."
STALE_BACKUP_S = 3600.0  # the timer runs a pass every 15 minutes: an hour without a good one is worth a look
BACKUP_CHECK_S = 20.0  # S3's client retries a bucket that doesn't answer; --check never waits longer than this
DISPLAY_UNIT = "voice-assistant-display"  # the browser's virtual screen, on a machine with none (the Pi)
OVERDUE_S = 3 * 60.0  # a reminder Telegram should have posted this long ago is waiting for it
PRESENCE = "Who's home"
ROUTER_FIX = "check its address, user, and its password in .env (the name under secret); docs/presence.md"


@dataclass
class Result:
    name: str
    status: str
    detail: str
    fix: str = ""


Check = Callable[[], Result | Iterable[Result]]


@dataclass(frozen=True)
class ServiceChecks:
    """The services TARS uses, each asked with its own key (service_checks)."""

    keyed: list[tuple[str, Check]]  # the paid ones, by name; none when the keys can't run TARS
    keys: Check  # the keys themselves, which --check runs first
    telegram: Check
    telegram_reminders: Check
    home_assistant: Check
    presence: Check  # how the routers last answered the assistant: the Status page never signs into them itself

    def listed(self) -> list[tuple[str, Check]]:
        """The web UI's Status page's: the paid services, or the keys' failure in their place, then the others."""
        return [
            *(self.keyed or [("API keys", self.keys)]),
            ("Telegram", self.telegram),
            ("Telegram reminders", self.telegram_reminders),
            ("Home Assistant", self.home_assistant),
            (PRESENCE, self.presence),
        ]


def run(checks: list[tuple[str, Check]]) -> list[Result]:
    """Every check, in order, each printed as it's done."""
    results = []
    for name, check in checks:
        for r in outcome(name, check):
            print(line(r), flush=True)
            results.append(r)
    return results


def outcome(name: str, check: Check) -> list[Result]:
    """A check's results. One that raises is a failure with the error, never the end of a run (a bad setting exits)."""
    try:
        got = check()
    except (Exception, SystemExit) as e:  # noqa: BLE001 - reported, and the rest still run
        got = Result(name, FAIL, str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}")
        if not isinstance(e, (OSError, RuntimeError, ValueError, SystemExit, *SERVICE_FAILURES)):
            traceback.print_exc()
    return [got] if isinstance(got, Result) else list(got)


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


def assistant_has_it(name: str, state: Callable[[str], str | None]) -> Result | None:
    """For a device that wouldn't open, or isn't even listed: whether the assistant's service is running, and so has
    it. On Linux, a device opened directly is one program's at a time (and others can't list it), and the service only
    stays up with its devices open, so then it's working. None if the service isn't running: the failure stands."""
    from .health import PROCESSES

    if state(PROCESSES["assistant"][1]) != "active":
        return None
    return Result(name, OK, "in use by the running assistant, which only runs with it open")


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


def web_port(exec_start: Callable[[str], str] | None = None, unit_file: Path = WEB_UNIT) -> int:
    """The port the web UI serves on: its unit's --port, as systemd runs it (drop-ins included) or else as installed;
    without either, the one --web uses by default."""
    try:
        installed = unit_file.read_text()
    except OSError:
        installed = ""
    for found in ((exec_start or unit_exec_start)("voice-assistant-web"), installed):
        if port := re.search(r"^ExecStart=.*?--port[= ](\d+)", found, re.MULTILINE):
            return int(port[1])
    return WEB_PORT


def unit_exec_start(unit: str) -> str:
    """The command systemd runs for a unit, as `systemctl show` says it ("ExecStart={ path=... argv[]=... }"); "" with
    no systemd to ask."""
    if not shutil.which("systemctl"):
        return ""
    try:
        command = ["systemctl", "--user", "show", "-p", "ExecStart", unit]
        return subprocess.run(command, capture_output=True, text=True, check=False, timeout=5).stdout
    except subprocess.TimeoutExpired:
        return ""


def web(get: Callable[[str], int], port: int = WEB_PORT) -> Result:
    try:
        status = get(f"http://127.0.0.1:{port}/api/status")
    except Exception:  # noqa: BLE001 - refused, timed out, not HTTP: not running, as far as TARS can tell
        status = None
    if status != 200:
        return Result(
            "Web UI",
            WARN,
            f"not answering on port {port}",
            "systemctl --user start voice-assistant-web on the Pi, or voice-assistant --web",
        )
    return Result("Web UI", OK, f"running on port {port}")


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


def telegram_waiting(cfg: Config, folder: Path, now: float | None = None) -> Result:
    """Reminders for a Telegram chat that are overdue while nothing posts them: they wait, and are posted late, with
    when they were due. `folder`: the event log's."""
    from .store import DB_FILE

    name = "Telegram reminders"
    if not (cfg.reminders.enabled and (folder / DB_FILE).exists()):
        return Result(name, SKIP, "no reminders")
    from .events import EventLog
    from .health import processes
    from .reminders import Reminders

    now = time.time() if now is None else now
    waiting = len(Reminders(EventLog(folder).store).overdue(now - OVERDUE_S))
    if not waiting:
        return Result(name, SKIP, "none waiting")
    said = f"{waiting} waiting to be posted"
    if not cfg.telegram.enabled:
        return Result(name, WARN, f"{said}, with [telegram] off", "turn it back on to post them, late")
    if not processes(folder, ["telegram"], lambda unit: None, now)[0]["running"]:
        return Result(name, FAIL, f"{said}: Telegram isn't running", "systemctl --user status voice-assistant-telegram")
    return Result(name, SKIP, f"{said}; Telegram is running")


def browser(cfg: Config) -> Result:
    """TARS's browser can start: its extra is installed, and there's a Chrome or Chromium to run."""
    if not cfg.browser.enabled:
        return Result("Browser", SKIP, "off ([browser] enabled)")
    try:
        from .browser.chrome import executable
    except ImportError:
        return Result("Browser", FAIL, 'no optional extra "browser"', "uv sync --all-extras")
    try:
        found = executable()
    except RuntimeError:
        return Result("Browser", FAIL, "no Chrome or Chromium", "sudo apt install chromium (install-pi.sh does)")
    orders = "orders once approved" if cfg.browser.really_order else "approved orders are a dry run"
    return Result("Browser", OK, f"{cfg.browser.model} at {cfg.browser.effort} effort, in {found}; {orders}")


def errands_waiting(cfg: Config, folder: Path, now: float | None = None) -> Result:
    """Errands asked aloud that wait in the queue while nothing runs them: the Telegram service does. `folder`: the
    event log's."""
    from .store import DB_FILE

    name = "Errands"
    if not (cfg.browser.enabled and (folder / DB_FILE).exists()):
        return Result(name, SKIP, "none")
    from .events import EventLog
    from .health import processes

    now = time.time() if now is None else now
    from .browser.errands import Queue

    waiting = Queue(EventLog(folder).store).waiting(now - OVERDUE_S)
    if not waiting:
        return Result(name, SKIP, "none waiting")
    said = f"{len(waiting)} waiting to be run"
    if not processes(folder, ["telegram"], lambda unit: None, now)[0]["running"]:
        return Result(name, FAIL, f"{said}: Telegram isn't running", "systemctl --user status voice-assistant-telegram")
    return Result(
        name,
        WARN,
        f"{said}, with Telegram running",
        "is an errand stuck? (journalctl --user -u voice-assistant-telegram)",
    )


def backup(cfg: Config, make: Callable[[], Backup], now: float | None = None) -> list[Result]:
    """The S3 bucket answers with the key TARS has, and how the timer's last pass went. `make`: the Backup, raising
    ConfigError if it can't be made."""
    from .backup import BackupError, Status

    if not cfg.backup.enabled:
        return [Result("Backup", SKIP, "off ([backup] enabled)")]
    try:
        job = make()
    except ConfigError as e:
        return [Result("Backup", FAIL, str(e.code), "docs/backup.md")]
    t = time.perf_counter()
    try:
        late = within(BACKUP_CHECK_S, "Backup", job.check, "the bucket didn't answer", "check the network")
    except BackupError as e:
        fix = "check the bucket's name and region, and the key's policy (docs/backup.md)"
        return [Result("Backup", FAIL, str(e), fix)]
    if late is not None:  # within's own failure: no answer in time
        return [late]
    where = f"s3://{cfg.backup.bucket}/{cfg.backup.prefix} answers ({time.perf_counter() - t:.1f} s)"
    first, *rest = last_pass(Status.load(job.status_path), now or time.time())
    said = f"the last pass failed: {first.detail}" if first.status == FAIL else first.detail
    return [replace(first, detail=f"{where}; {said}"), *rest]


def last_pass(status: BackupStatus, now: float) -> list[Result]:
    """How the timer's last pass went, as it left it in backup.json: for --check, and the web UI's Status page."""
    start = "systemctl --user start voice-assistant-backup, and see journalctl --user-unit voice-assistant-backup"
    if not status.last_run:
        return [Result("Backup", WARN, "no pass has run yet", start)]
    if status.error:
        return [Result("Backup", FAIL, status.error, start)]
    ago = _ago(now - status.last_ok)
    files = f"{status.in_bucket} files there, {status.only_in_bucket} only there; the last good pass {ago}"
    stale = now - status.last_ok > STALE_BACKUP_S
    results = [Result("Backup", WARN if stale else OK, files, start if stale else "")]
    if not status.lifecycle_ok:
        results.append(Result("Backup lifecycle", WARN, status.lifecycle, "set the rules by hand (docs/backup.md)"))
    return results


def _ago(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} minutes ago"
    return f"{seconds / 3600:.1f} hours ago"


def backup_timer(active: Callable[[str], str | None]) -> Result:
    """The backup's timer, whose passes log under the service it starts."""
    found = systemd("voice-assistant-backup.timer", active)
    if found.status == WARN:
        fix = (
            "systemctl --user enable --now voice-assistant-backup.timer; the passes' logs: "
            "journalctl --user-unit voice-assistant-backup"
        )
        return Result(found.name, WARN, found.detail, fix)
    return found


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
    """The HTTP status, whatever it is; httpx.TransportError if nothing answered."""
    import httpx

    return httpx.get(url, headers=headers, timeout=5, follow_redirects=True).status_code


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


def routers_now(
    cfg: Config, secret: Callable[[str], str | None], make_router: Callable | None = None
) -> Result | list[Result]:
    """--check's: each router in [[presence.routers]], signed into now, and whose phone it sees."""
    from .presence import People
    from .routers import RouterError, make

    if not cfg.presence.enabled:
        return Result(PRESENCE, SKIP, "off ([presence] enabled)")
    make_router = make_router or make
    people, results = People(cfg.presence.people), []
    for spec in cfg.presence.routers:
        where = f"Router {spec['address']}"
        password = secret(spec["secret"])
        if not password:
            results.append(Result(where, FAIL, f"no {spec['secret']} in .env", ROUTER_FIX))
            continue
        router = make_router(spec, password)
        try:
            seen = router.connected()
        except RouterError as e:
            results.append(Result(where, FAIL, str(e), ROUTER_FIX))
            continue
        finally:
            router.close()
        here = sorted(people.names[p] for p in people.whose(seen))
        phones = f"; sees the phone of {', '.join(here)}" if here else "; sees nobody's phone just now"
        results.append(Result(where, OK, f"{len(seen)} devices connected{phones}"))
    return results


def routers_last(cfg: Config, folder: Path, now: float | None = None) -> Result | list[Result]:
    """The Status page's: how each router last answered the assistant, and who's home, from the event log; it never
    signs into a router itself. `folder`: the event log's."""
    from .store import DB_FILE

    if not cfg.presence.enabled:
        return Result(PRESENCE, SKIP, "off ([presence] enabled)")
    if not cfg.learning.log_events:
        return Result(PRESENCE, FAIL, "it's kept in the event log", "set [learning] log_events = true")
    if not (folder / DB_FILE).exists():
        return Result(PRESENCE, WARN, "the routers haven't been asked yet", "is the assistant running? (it asks them)")
    from .events import EventLog
    from .presence import STALE_AFTER_S, Board, People

    now = time.time() if now is None else now
    board = Board(EventLog(folder).store, People(cfg.presence.people), clock=lambda: now)
    answers, results = board.routers(), []
    for spec in cfg.presence.routers:
        where, a = f"Router {spec['address']}", answers.get(spec["address"])
        last = max((t for t in (a.ok_at, a.error_at) if t is not None), default=None) if a else None
        if last is None or now - last > STALE_AFTER_S:
            results.append(Result(where, WARN, "not asked lately", "is the assistant running? (it asks the routers)"))
        elif a.error:
            results.append(Result(where, FAIL, a.error, ROUTER_FIX))
        else:
            results.append(Result(where, OK, f"{a.connected} devices connected, {_ago(now - a.ok_at)}"))
    results.append(Result(PRESENCE, OK, board.summary()))
    return results


def env_value(env: Path, name: str) -> str | None:
    return dotenv_values(env).get(name) or None


def with_keys_there(cfg: Config, env: Path) -> tuple[Config, set[str], bool]:
    """The setup cut down to the keys in `env` (config.with_keys), the keys, and whether they're enough to run on; if
    they aren't, the setup as it's written."""
    from . import __main__ as cli

    have = cli.env_keys(env)
    try:
        return with_keys(cfg, have)[0], have, True
    except ConfigError:
        return cfg, have, False


def service_checks(cfg: Config, root: Path) -> ServiceChecks:
    """The cloud services TARS uses, and Home Assistant, each asked with its own key: --check runs them, and so does
    the web UI's Status page. A service TARS doesn't use, as config.toml and .env set it up, is a SKIP."""
    import httpx

    from . import __main__ as cli
    from .hass import HomeAssistant
    from .home import TOKEN_KEY
    from .tts import CARTESIA_API, CARTESIA_VERSION

    env = root / ".env"
    asked = cfg
    cfg, have, have_keys = with_keys_there(cfg, env)

    def openai():
        if OPENAI_KEY not in have:
            return Result("OpenAI", SKIP, f"not used (no {OPENAI_KEY})")
        try:
            return timed("OpenAI", lambda: cli.make_openai_client(env).models.retrieve(cfg.llm.model).id)
        except Exception as e:  # noqa: BLE001 - what went wrong is the result
            return service_error("OpenAI", e)

    def quick(provider: QuickProvider):
        model = getattr(cfg.llm, provider.setting)
        if not model:
            why = f"no {provider.key_name}" if provider.key_name not in have else f"[llm] {provider.setting} is empty"
            return Result(provider.name, SKIP, f"not used ({why})")
        try:
            client = cli.make_quick_client(env, provider)
            return timed(provider.name, lambda: has_model(client.models.list(), model))
        except Exception as e:  # noqa: BLE001
            return service_error(provider.name, e)

    def deepgram():
        if cfg.stt.provider == "openai" and cfg.tts.provider != "deepgram":
            return Result("Deepgram", SKIP, "not used")
        key = cli.api_key(env, DEEPGRAM_KEY)
        t = time.perf_counter()
        try:
            host = cfg.stt.deepgram_host if cfg.stt.provider in ("deepgram", "flux") else cfg.tts.deepgram_host
            # /v1/auth/token: the EU servers don't serve /v1/projects (404 with a good key); both answer this one
            status = http_status(f"https://{host}/v1/auth/token", {"Authorization": f"Token {key}"})
        except httpx.TransportError as e:
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
        except httpx.TransportError as e:
            return service_error("Cartesia", e)
        return service_status("Cartesia", CARTESIA_KEY, status, time.perf_counter() - t)

    def bot_me(token: str) -> dict:
        from .telegram import Bot

        return Bot(token).me()

    def hass_ping(token: str) -> str:
        hass = HomeAssistant(cfg.home.url, token)
        try:
            return hass.ping()
        finally:
            hass.close()

    def hass():
        token = env_value(env, TOKEN_KEY)
        return home_assistant(cfg, token, lambda: hass_ping(token or ""))

    keyed: list[tuple[str, Check]] = [
        ("OpenAI", openai),
        *((provider.name, lambda provider=provider: quick(provider)) for provider in QUICK_PROVIDERS),
        ("Deepgram", deepgram),
        ("Cartesia", cartesia),
    ]
    return ServiceChecks(
        keyed if have_keys else [],
        lambda: keys(asked, env),
        lambda: telegram(cfg, env_value(env, TELEGRAM_KEY), bot_me),
        lambda: telegram_waiting(cfg, root / cfg.learning.folder),
        hass,
        lambda: routers_last(cfg, root / cfg.learning.folder),
    )


def check_all(cfg: Config, root: Path) -> int:
    import sounddevice as sd

    from . import __main__ as cli
    from .audio import BLOCK_SECONDS, Microphone, NoDeviceError, Speaker, device_names, find_device
    from .health import PROCESSES, expected

    env = root / ".env"
    asked = cfg
    cfg, _, have_keys = with_keys_there(cfg, env)

    def audio_device(kind: str):
        names, default_in, default_out = device_names()
        wanted = cfg.audio.input_device if kind == "input" else cfg.audio.output_device
        found = device(kind, wanted, find_device, names, default_in if kind == "input" else default_out)
        # A device the running assistant has isn't even listed: nothing else can ask it about itself.
        return (found.status == FAIL and assistant_has_it(found.name, systemd_state)) or found

    def listen():
        print(f"(listening {LISTEN_S:.0f} s: say something)", flush=True)

        def measure():
            try:
                mic = Microphone(find_device(cfg.audio.input_device, "input"))
            except NoDeviceError:
                if held := assistant_has_it("Microphone", systemd_state):
                    return held
                raise
            with mic:
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

        def say():
            try:
                speaker = Speaker(
                    find_device(cfg.audio.output_device, "output"), tts.sample_rate, cfg.audio.playback_prebuffer_s
                )
            except NoDeviceError:
                if held := assistant_has_it("Speaker", systemd_state):
                    return held  # the voice service's own check says whether it answers
                raise
            with speaker:
                return voice(tts.stream, lambda audio: speaker.play_pcm_stream(iter(audio), tts.sample_rate))

        return within(30, "Voice", say, "no voice in time", "check the network, and [audio] output_device")

    def made_house():
        from .home import Context, build

        context = Context(root, functools.partial(env_value, env), announce=lambda line: None)
        from .androidtv import make_tv_agent

        context.tv_agent = functools.partial(make_tv_agent, cfg, env, context.announce)
        return build(cfg.home, context)

    services = service_checks(asked, root)
    hass_check = functools.cache(services.home_assistant)  # asked once: the house's devices want to know too
    secret = functools.partial(env_value, env)

    def devices():
        if not cfg.home.devices:
            return Result("The house", SKIP, "no devices ([[home.devices]] in config.toml)")
        built = made_house()
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
        ("API keys", services.keys),
        ("Input device", lambda: audio_device("input")),
        ("Output device", lambda: audio_device("output")),
        ("Local models", models),
        ("Echo cancellation", echo),
        ("Voiceprints", speakers),
        *services.keyed,
        ("Telegram", services.telegram),
        ("Telegram reminders", services.telegram_reminders),
        ("Voice", speaking),
        ("Microphone", listen),
        ("Storage", lambda: disk(root / cfg.learning.folder)),
        ("Web UI", lambda: web(http_status, web_port())),
        ("Home Assistant", hass_check),
        ("The house", devices),
        (PRESENCE, lambda: within(60, PRESENCE, lambda: routers_now(cfg, secret), "a router didn't answer", "")),
        ("Backup", lambda: backup(cfg, lambda: cli.make_backup(cfg, root))),
        ("Browser", lambda: browser(cfg)),
        ("Errands", lambda: errands_waiting(cfg, root / cfg.learning.folder)),
        *((unit, lambda unit=unit: systemd(unit, systemd_state)) for unit in (PROCESSES[p][1] for p in expected(cfg))),
        *([("voice-assistant-backup.timer", lambda: backup_timer(systemd_state))] if cfg.backup.enabled else []),
        *([(DISPLAY_UNIT, lambda: systemd(DISPLAY_UNIT, systemd_state))] if cfg.browser.enabled else []),
    ]
    print("Checking TARS...\n", flush=True)
    results = run(checks)
    print(f"\n{summary(results)}", flush=True)
    if any(t.name.startswith("check-") for t in threading.enumerate()):
        os._exit(exit_code(results))  # a device still blocked would hold up the exit
    return exit_code(results)
