"""What the web UI's Status page shows (docs/web-ui.md#the-status-page): whether each part of TARS is running, from
the heartbeat each process writes; what TARS did today; the cloud services TARS uses, each asked with its own key (the
same checks as --check) and each with its own status page, and Home Assistant; how the backup's last pass went, and its
timer; and the machine: CPU, memory, temperature, throttling, disk and uptime.

A heartbeat is a small file each long-running process rewrites every 30 seconds, under the event log's folder
(heartbeats/<name>.json), from a thread of its own: the database isn't written to for it. A process that crashed, was
killed or froze whole stops writing; one stuck inside its own loop doesn't, which is what the assistant's own watchdog
on the mic is for."""

import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TypedDict

from .check import FAIL, LOW_DISK_BYTES, SKIP, Check, Result, backup_timer, last_pass, outcome
from .config import Config
from .conversations import ConversationLog
from .events import EventLog
from .files import atomic_write

HEARTBEAT_EVERY_S = 30.0
MISSED_BEATS = 3  # a process that hasn't written for this many beats has stopped
AHEAD_S = 5.0  # a beat further ahead than this was written before the clock was set back: no sign of life now
HEARTBEATS = "heartbeats"
# The processes TARS runs, as the Status page names them, and their systemd units at home.
PROCESSES = {
    "assistant": ("The assistant", "voice-assistant"),
    "web": ("The web UI", "voice-assistant-web"),
    "telegram": ("Telegram", "voice-assistant-telegram"),
}
# Each service's own status page, where it says it's having trouble: all of these publish Statuspage's JSON. Keyed by
# the names check.service_checks gives them.
STATUS_PAGES = {
    "OpenAI": "https://status.openai.com",
    "Groq": "https://groqstatus.com",
    "Cerebras": "https://status.cerebras.ai",
    "Deepgram": "https://status.deepgram.com",
    "Cartesia": "https://status.cartesia.ai",
}
SERVICES_FRESH_S = 60.0  # the services are asked again only once their last answers are this old
# The services and their status pages are all asked at once, and given this long together: one that hasn't answered
# by then is a failure, so a service that hangs can't hold the page up.
ASK_DEADLINE_S = 10.0
STATUS_PAGE_TIMEOUT_S = 8.0
HOT_C = 80.0  # a Pi 5 slows itself down at 85 °C, and its fan is on well before: 80 is worth a look
# The Raspberry Pi's firmware: what `vcgencmd get_throttled` reports, bit by bit, now and since it started.
THROTTLED = {
    0: "under-voltage now: the power supply can't keep up",
    1: "running slower now, to stay within power or heat",
    2: "throttled now",
    3: "at the soft temperature limit now",
    16: "under-voltage since it started",
    17: "slowed down since it started",
    18: "throttled since it started",
    19: "at the soft temperature limit since it started",
}


# What /api/health answers, as webui/src/types.ts has it (Health).
class ProcessHealth(TypedDict):
    name: str
    label: str
    running: bool
    started: float | None
    beat: float | None
    version: str
    unit_state: str | None  # systemd's, where there's systemd


class Today(TypedDict):
    wakes: int
    answers: int
    failed: int
    last_wake: float | None


class StatusPage(TypedDict):
    indicator: str  # Statuspage's: none, minor, major, critical or maintenance; or unknown, when it can't be read
    description: str
    url: str


class ServiceHealth(TypedDict):
    name: str
    status: str  # check.py's: ok, warn or fail
    detail: str
    fix: str
    page: StatusPage | None


class CheckLine(TypedDict):
    name: str
    status: str  # check.py's: ok, warn or fail
    detail: str
    fix: str


class MachineHealth(TypedDict):
    host: str
    system: str
    python: str
    cpu_percent: float
    cores: int
    load: list[float]
    memory_total: int
    memory_used: int
    temperature_c: float | None
    hot: bool
    throttled: list[str] | None  # None: not a Pi
    disk_total: int
    disk_free: int
    disk_low: bool
    uptime_s: float


class Report(TypedDict):
    now: float
    processes: list[ProcessHealth]
    today: Today
    services: list[ServiceHealth]
    services_asked: float
    backup: list[CheckLine] | None  # None: [backup] is off
    machine: MachineHealth


class Heartbeat:
    """Writes <folder>/heartbeats/<name>.json now and every `every_s`, from a thread of its own, until the process
    ends."""

    def __init__(self, folder: Path, name: str, root: Path | None = None, every_s: float = HEARTBEAT_EVERY_S):
        """`root`: the project's folder, for the commit it runs."""
        self._path = folder / HEARTBEATS / f"{name}.json"
        self._name, self._every_s = name, every_s
        self._version = version(root) if root else ""
        self._since = time.monotonic()  # when it started, whatever the clock does meanwhile
        self._failing = False

    def start(self) -> Heartbeat:
        self.beat()
        threading.Thread(target=self._run, daemon=True, name=f"heartbeat-{self._name}").start()
        return self

    def beat(self) -> None:
        """Never raises: a heartbeat that can't be written shows as the process stopping, which is the truth the page
        can see. Said once in the log, until one is written again."""
        now = time.time()
        # When it started, worked out again each time: a Pi without a clock starts on the wrong time, and only later
        # sets it from the network.
        started = now - (time.monotonic() - self._since)
        beat = {"pid": os.getpid(), "started": started, "beat": now, "every_s": self._every_s, "version": self._version}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self._path, json.dumps(beat))
            self._failing = False
        except OSError as e:
            if not self._failing:
                print(f"(couldn't write the heartbeat: {e!r})", flush=True)
            self._failing = True

    def _run(self) -> None:
        while True:
            time.sleep(self._every_s)
            self.beat()


def version(root: Path) -> str:
    """The git commit TARS runs, short, or "" outside a checkout."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def expected(cfg: Config) -> list[str]:
    """The processes this setup runs (PROCESSES): Telegram's only when it's on."""
    return ["assistant", "web", *(["telegram"] if cfg.telegram.enabled else [])]


def processes(
    folder: Path, wanted: list[str], unit_state: Callable[[str], str | None], now: float
) -> list[ProcessHealth]:
    """Each of TARS's processes in `wanted`, from its heartbeat: running or not, since when, and its systemd unit's
    state where there's systemd. One that never wrote a heartbeat is listed as never seen."""
    out: list[ProcessHealth] = []
    for name in wanted:
        label, unit = PROCESSES[name]
        beat = _read_beat(folder / HEARTBEATS / f"{name}.json")
        last = beat.get("beat")
        state = unit_state(unit)
        out.append(
            {
                "name": name,
                "label": label,
                "running": last is not None and -AHEAD_S <= now - last <= beat["every_s"] * MISSED_BEATS,
                "started": beat.get("started"),
                "beat": last,
                "version": beat.get("version") or "",
                "unit_state": None if state == "unknown" else state,  # systemd there, but no user session to ask
            }
        )
    return out


def _read_beat(path: Path) -> dict:
    """A heartbeat's numbers, each only if it's a number; a file that's missing or isn't one reads as never seen."""
    try:
        found = json.loads(path.read_text())
    except OSError, ValueError:
        found = None
    if not isinstance(found, dict):
        found = {}
    beat = {k: v for k in ("beat", "started", "every_s") if isinstance(v := found.get(k), int | float)}
    beat.setdefault("every_s", HEARTBEAT_EVERY_S)
    if isinstance(found.get("version"), str):
        beat["version"] = found["version"]
    return beat


# What the Status page calls the backup's lines, by the names --check gives them.
BACKUP_LINES = {"Backup": "The last pass", "Backup lifecycle": "The bucket's rules"}


def backup(folder: Path, unit_state: Callable[[str], str | None], now: float) -> list[CheckLine]:
    """How the backup's last pass went, from what it left in the event log's folder (as --check says it, without
    asking the bucket), and its timer, where there's systemd."""
    from .backup import STATUS_FILE, Status

    found = last_pass(Status.load(folder / STATUS_FILE), now)
    timer = backup_timer(lambda unit: None if (state := unit_state(unit)) == "unknown" else state)
    if timer.status != SKIP:
        found.append(replace(timer, name="The timer"))
    return [
        {"name": BACKUP_LINES.get(r.name, r.name), "status": r.status, "detail": r.detail, "fix": r.fix} for r in found
    ]


def today(log: EventLog, conversations: ConversationLog, now: float) -> Today:
    """What TARS did since midnight: wakes, answers and failed answers; and when it last woke."""
    day = time.localtime(now)
    midnight = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1))
    answers, failed = conversations.answers_since(midnight)
    return {"wakes": log.wakes_since(midnight), "answers": answers, "failed": failed, "last_wake": log.last_wake()}


def machine(folder: Path, read_throttled: Callable[[], int | None] | None = None) -> MachineHealth:
    """The machine TARS runs on, as it is now. `folder`: whose disk to measure (the event log's)."""
    import psutil

    memory, disk = psutil.virtual_memory(), shutil.disk_usage(folder)
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    flags = (read_throttled or pi_throttled)()
    celsius = temperature()
    return {
        "host": socket.gethostname(),
        "system": f"{platform.system()} {platform.release()}, {platform.machine()}",
        "python": platform.python_version(),
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "cores": psutil.cpu_count() or 1,
        "load": [round(x, 2) for x in load],
        "memory_total": memory.total,
        "memory_used": memory.total - memory.available,
        "temperature_c": celsius,
        "hot": celsius is not None and celsius >= HOT_C,
        "throttled": None if flags is None else [said for bit, said in THROTTLED.items() if flags >> bit & 1],
        "disk_total": disk.total,
        "disk_free": disk.free,
        "disk_low": disk.free < LOW_DISK_BYTES,  # as --check has it
        "uptime_s": time.time() - psutil.boot_time(),
    }


def temperature() -> float | None:
    """The CPU's temperature in °C where the system says (Linux, a Pi), else None (a Mac doesn't tell)."""
    import psutil

    try:
        sensors = psutil.sensors_temperatures()
    except AttributeError, OSError:
        return None
    for name in ("cpu_thermal", "coretemp", "k10temp", "soc_thermal"):
        if sensors.get(name):
            return round(sensors[name][0].current, 1)
    readings = [r.current for rs in sensors.values() for r in rs]
    return round(max(readings), 1) if readings else None


def pi_throttled() -> int | None:
    """The Pi firmware's throttling flags, or None on anything else."""
    if not shutil.which("vcgencmd"):
        return None
    try:
        command = ["vcgencmd", "get_throttled"]
        out = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False).stdout
        return int(out.strip().split("=")[1], 16)
    except OSError, subprocess.TimeoutExpired, IndexError, ValueError:
        return None


def status_page(url: str, fetch_json: Callable[[str], dict]) -> StatusPage:
    """A service's own status page, as it says it is; one that can't be read is "unknown", and says why."""
    try:
        found = fetch_json(f"{url}/api/v2/status.json")["status"]
        return {"indicator": found["indicator"], "description": found["description"], "url": url}
    except Exception as e:  # noqa: BLE001 - a status page that can't be read says only that
        return {"indicator": "unknown", "description": f"Couldn't read it ({type(e).__name__})", "url": url}


class Services:
    """The services TARS uses, each asked with its key, and their status pages, all at once and within
    ASK_DEADLINE_S; kept for SERVICES_FRESH_S, so a page reloaded again and again asks them once a minute at most.
    One asks at a time: meanwhile the others get the last answers, or, before there are any, wait for them."""

    def __init__(
        self,
        checks: Callable[[], list[tuple[str, Check]]],
        fetch_json: Callable[[str], dict] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        """`checks`: check.service_checks for the setup. `fetch_json`: a URL's JSON (httpx, by default). `clock`
        says how old the answers are, and `wall` when they were asked."""
        self._checks, self._fetch_json = checks, fetch_json or _fetch_json
        self._clock, self._wall = clock, wall
        self._lock = threading.Lock()
        self._found: list[ServiceHealth] | None = None
        self._asked, self._at = 0.0, 0.0

    def get(self) -> tuple[list[ServiceHealth], float]:
        """The services, and when they were asked."""
        if not self._lock.acquire(blocking=self._found is None):
            return self._found or [], self._asked
        try:
            if self._found is None or self._clock() - self._at > SERVICES_FRESH_S:
                self._found, self._asked, self._at = self._ask(), self._wall(), self._clock()
            return self._found, self._asked
        finally:
            self._lock.release()

    def _ask(self) -> list[ServiceHealth]:
        checks = self._checks()
        calls: dict[tuple[str, str], Callable] = {}
        for name, check in checks:
            calls["check", name] = lambda name=name, check=check: outcome(name, check)
        for name, url in STATUS_PAGES.items():
            calls["page", name] = lambda url=url: status_page(url, self._fetch_json)
        done = _all_within(calls, ASK_DEADLINE_S)
        late = f"no answer within {ASK_DEADLINE_S:.0f} s"
        found: list[ServiceHealth] = []
        for name, _ in checks:
            page = None
            if url := STATUS_PAGES.get(name):
                page = done.get(("page", name)) or {"indicator": "unknown", "description": late, "url": url}
            for r in done.get(("check", name)) or [Result(name, FAIL, late)]:
                if r.status != SKIP:
                    found.append({"name": r.name, "status": r.status, "detail": r.detail, "fix": r.fix, "page": page})
        return found


def _all_within(calls: dict, seconds: float) -> dict:
    """Every call at once, each in a thread of its own, all given `seconds` at most together: what each returned, by
    its key, leaving out any still going (left to finish by itself). The calls never raise."""
    done: dict = {}
    threads = [
        threading.Thread(target=lambda key=key, call=call: done.__setitem__(key, call()), daemon=True, name="status")
        for key, call in calls.items()
    ]
    deadline = time.monotonic() + seconds
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return dict(done)


class Health:
    """The Status page's report, put together when asked."""

    def __init__(
        self,
        log: EventLog,
        wanted: list[str],
        services: Services,
        unit_state: Callable[[str], str | None],
        read_throttled: Callable[[], int | None] | None = None,
        backed_up: bool = False,
    ):
        """`wanted`: the processes this setup runs (expected). `unit_state`: systemd's state of a unit, or None.
        `backed_up`: [backup] is on."""
        self._log, self._conversations = log, ConversationLog(log)
        self._wanted, self._services, self._unit_state = wanted, services, unit_state
        self._read_throttled, self._backed_up = read_throttled, backed_up

    def report(self) -> Report:
        services, asked = self._services.get()
        now = time.time()
        return {
            "now": now,
            "processes": processes(self._log.folder, self._wanted, self._unit_state, now),
            "today": today(self._log, self._conversations, now),
            "services": services,
            "services_asked": asked,
            "backup": backup(self._log.folder, self._unit_state, now) if self._backed_up else None,
            "machine": machine(self._log.folder, self._read_throttled),
        }


def _fetch_json(url: str) -> dict:
    import httpx

    response = httpx.get(url, timeout=STATUS_PAGE_TIMEOUT_S, follow_redirects=True)
    response.raise_for_status()
    return response.json()
