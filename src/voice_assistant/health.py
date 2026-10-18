"""What the web UI's Status page shows (docs/web-ui.md#status): whether each part of TARS is running, from the
heartbeat each process writes; the cloud services TARS uses, each asked with its own key (the same checks as
--check) and each with its own status page; and the machine: CPU, memory, temperature, throttling, disk and uptime.

A heartbeat is a small file each long-running process rewrites every 30 seconds, under the event log's folder
(heartbeats/<name>.json): the database isn't written to for it, and a process that stopped, or hangs, stops writing."""

import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .files import atomic_write

HEARTBEAT_EVERY_S = 30.0
MISSED_BEATS = 3  # a process that hasn't written for this many beats has stopped, or hangs
HEARTBEATS = "heartbeats"
# The processes TARS runs, as the Status page names them, and their systemd units at home.
PROCESSES = {
    "assistant": ("The assistant", "voice-assistant"),
    "web": ("The web UI", "voice-assistant-web"),
    "telegram": ("Telegram", "voice-assistant-telegram"),
}
# Each service's own status page, where it says it's having trouble: all of these publish Statuspage's JSON.
STATUS_PAGES = {
    "OpenAI": "https://status.openai.com",
    "Groq": "https://groqstatus.com",
    "Cerebras": "https://status.cerebras.ai",
    "Deepgram": "https://status.deepgram.com",
    "Cartesia": "https://status.cartesia.ai",
}
SERVICES_FRESH_S = 60.0  # the services are asked again only once their last answers are this old
CHECK_TIMEOUT_S = 10.0
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


class Heartbeat:
    """Writes <folder>/<name>.json now and every `every_s`, from a thread of its own, until the process ends."""

    def __init__(self, folder: Path, name: str, root: Path | None = None, every_s: float = HEARTBEAT_EVERY_S):
        """`root`: the project's folder, for the version it runs (its git commit)."""
        self._path = folder / HEARTBEATS / f"{name}.json"
        self._every_s = every_s
        self._info = {
            "name": name,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started": time.time(),
            "every_s": every_s,
            "version": version(root) if root else "",
        }

    def start(self) -> Heartbeat:
        self.beat()
        threading.Thread(target=self._run, daemon=True, name=f"heartbeat-{self._info['name']}").start()
        return self

    def beat(self) -> None:
        """Never raises: a heartbeat that can't be written shows as the process stopping, which is the truth the page
        can see."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self._path, json.dumps({**self._info, "beat": time.time()}).encode())
        except OSError as e:
            print(f"(couldn't write the heartbeat: {e!r})")

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


def processes(folder: Path, wanted: list[str], unit_state: Callable[[str], str | None], now: float) -> list[dict]:
    """Each of TARS's processes in `wanted`, from its heartbeat: running or not, since when, and its systemd unit's
    state where there's systemd. One that never wrote a heartbeat is listed as never seen."""
    out = []
    for name in wanted:
        label, unit = PROCESSES[name]
        try:
            beat = json.loads((folder / HEARTBEATS / f"{name}.json").read_text())
        except FileNotFoundError, ValueError:
            beat = {}
        every = float(beat.get("every_s") or HEARTBEAT_EVERY_S)
        last = beat.get("beat")
        out.append(
            {
                "name": name,
                "label": label,
                "running": last is not None and now - last <= every * MISSED_BEATS,
                "started": beat.get("started"),
                "beat": last,
                "pid": beat.get("pid"),
                "version": beat.get("version") or "",
                "unit": unit,
                "unit_state": unit_state(unit),
            }
        )
    return out


def today(store, now: float) -> dict:
    """What TARS did since midnight: wakes, answers, failed answers, and the last of each."""
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    wakes = store.rows("SELECT count(*) AS n, max(ts) AS last FROM events WHERE kind='wake' AND ts >= ?", (midnight,))
    answers = store.rows(
        "SELECT count(*) AS n, sum(failed_at IS NOT NULL) AS failed, max(ts) AS last FROM turns "
        "WHERE role='tars' AND ts >= ?",
        (midnight,),
    )
    last_wake = store.rows("SELECT max(ts) AS ts FROM events WHERE kind='wake'")[0]["ts"]
    return {
        "wakes": wakes[0]["n"],
        "answers": answers[0]["n"] - (answers[0]["failed"] or 0),
        "failed": answers[0]["failed"] or 0,
        "last_wake": last_wake,
    }


def machine(folder: Path, throttled: Callable[[], int | None] | None = None) -> dict:
    """The machine TARS runs on, as it is now. `folder`: whose disk to measure (the event log's)."""
    import psutil

    memory, disk = psutil.virtual_memory(), shutil.disk_usage(folder)
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    flags = (throttled or pi_throttled)()
    return {
        "host": socket.gethostname(),
        "system": f"{platform.system()} {platform.release()}, {platform.machine()}",
        "python": platform.python_version(),
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "cores": psutil.cpu_count() or 1,
        "load": [round(x, 2) for x in load],
        "memory_total": memory.total,
        "memory_used": memory.total - memory.available,
        "temperature_c": temperature(psutil),
        "throttled": None if flags is None else [said for bit, said in THROTTLED.items() if flags >> bit & 1],
        "disk_total": disk.total,
        "disk_used": disk.used,
        "uptime_s": time.time() - psutil.boot_time(),
    }


def temperature(psutil) -> float | None:
    """The CPU's temperature in °C where the system says (Linux, a Pi), else None (a Mac doesn't tell)."""
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


def status_page(url: str, get: Callable[[str], dict]) -> dict:
    """A service's own status page, as it says it is: {indicator: none|minor|major|critical|unknown, description}."""
    try:
        found = get(f"{url}/api/v2/status.json")["status"]
        return {"indicator": found["indicator"], "description": found["description"], "url": url}
    except Exception as e:  # noqa: BLE001 - a status page that can't be read says only that
        return {"indicator": "unknown", "description": f"couldn't read it ({type(e).__name__})", "url": url}


class Services:
    """The services TARS uses, each asked with its key, and each one's status page, all at once; kept for
    SERVICES_FRESH_S, so a page reloaded again and again asks them once a minute at most."""

    def __init__(
        self,
        checks: Callable[[], list[tuple[str, Callable]]],
        get: Callable[[str], dict] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        """`checks`: check.service_checks for the setup. `get`: fetches a URL's JSON (httpx, by default)."""
        self._checks, self._get, self._clock = checks, get or _get_json, clock
        self._lock = threading.Lock()
        self._found: list[dict] = []
        self._at = 0.0

    def get(self) -> tuple[list[dict], float]:
        """The services, and when they were asked."""
        with self._lock:
            if self._clock() - self._at > SERVICES_FRESH_S:
                self._found, self._at = self._ask(), self._clock()
            return self._found, self._at

    def _ask(self) -> list[dict]:
        from .check import SKIP, Result

        def check(call) -> Result:
            try:
                got = call()
            except Exception as e:  # noqa: BLE001 - what went wrong is the result
                return Result("", "fail", f"{type(e).__name__}: {e}", "")
            return got if isinstance(got, Result) else next(iter(got))

        checks = self._checks()
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="status") as pool:
            results = list(pool.map(lambda c: check(c[1]), checks))
            used = [(name, r) for (name, _), r in zip(checks, results, strict=True) if r.status != SKIP]
            pages = list(pool.map(lambda name: self._page(name), [name for name, _ in used]))
        return [
            {"name": name, "status": r.status, "detail": r.detail, "fix": r.fix, "page": page}
            for (name, r), page in zip(used, pages, strict=True)
        ]

    def _page(self, name: str) -> dict | None:
        return status_page(STATUS_PAGES[name], self._get) if name in STATUS_PAGES else None


def _get_json(url: str) -> dict:
    import httpx

    response = httpx.get(url, timeout=CHECK_TIMEOUT_S, follow_redirects=True)
    response.raise_for_status()
    return response.json()
