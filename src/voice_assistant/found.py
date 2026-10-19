"""Devices on the home network whose address can change. The router hands out addresses, and not every router lets a
household fix one. Where TARS last found each device is kept in voice_data/found.json; one that stops answering there
is looked for again by what doesn't change: an Android TV box by its hardware (MAC) address, a Tuya device by its id
(the example plugin's IR blaster)."""

import asyncio
import ipaddress
import json
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from .files import atomic_write

FOUND_FILE = "voice_data/found.json"
SWEEP_TIMEOUT_S = 0.6  # each address's port, all at once: a sweep of the home network takes about this long
_lock = threading.Lock()  # one writer at a time, whichever device found itself


class Seen:
    """Where each device was last found: {key: {"address": ..., "mac": ...}}."""

    def __init__(self, path: Path):
        self.path = path

    def get(self, key: str) -> dict:
        return self._read().get(key, {})

    def put(self, key: str, **values: str) -> None:
        with _lock:
            everything = self._read()
            everything[key] = {**everything.get(key, {}), **{k: v for k, v in values.items() if v}}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, json.dumps(everything, indent=1))

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError, ValueError:
            return {}
        return data if isinstance(data, dict) else {}


def reachable(host: str, port: int, timeout_s: float) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout_s).close()
    except OSError:
        return False
    return True


def mac_of(ip: str) -> str | None:
    """The hardware address the home network knows `ip` by (the ARP table), lower case, or None. Only an address this
    machine talked to lately is in it."""
    if sys.platform == "linux":
        try:
            return arp_linux(Path("/proc/net/arp").read_text(), ip)
        except OSError:
            return None
    try:
        out = subprocess.run(["arp", "-n", ip], capture_output=True, text=True, timeout=3, check=False).stdout
    except OSError, subprocess.TimeoutExpired:
        return None
    return arp_bsd(out)


def arp_linux(table: str, ip: str) -> str | None:
    """From /proc/net/arp: "192.168.1.209  0x1  0x2  0c:dc:91:3d:ef:60  *  wlan0"."""
    for line in table.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[0] == ip and parts[3] != "00:00:00:00:00:00":
            return normal_mac(parts[3])
    return None


def arp_bsd(out: str) -> str | None:
    """From macOS's arp -n: "? (192.168.1.209) at c:dc:91:3d:ef:60 on en0 ifscope [ethernet]" (no leading zeros)."""
    found = re.search(r" at ((?:[0-9a-f]{1,2}:){5}[0-9a-f]{1,2})\b", out, re.IGNORECASE)
    return normal_mac(found.group(1)) if found else None


def normal_mac(mac: str) -> str:
    return ":".join(part.zfill(2) for part in mac.lower().split(":"))


def find_by_mac(
    mac: str,
    port: int,
    near: str,
    open_on: Callable[[list[str], int], list[str]] | None = None,
    mac_of: Callable[[str], str | None] = mac_of,
) -> str | None:
    """The address on `near`'s home network (its /24) where `port` is open and the hardware address is `mac`. Only
    the port is knocked on: nothing else is asked of any device, so no other box shows a prompt."""
    try:
        network = ipaddress.ip_network(f"{near}/24", strict=False)
    except ValueError:
        return None
    hosts = [str(h) for h in network.hosts()]
    for host in (open_on or _open_on)(hosts, port):
        if mac_of(host) == normal_mac(mac):
            return host
    return None


def _open_on(hosts: list[str], port: int) -> list[str]:
    async def knock(host: str) -> str | None:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), SWEEP_TIMEOUT_S)
        except OSError, TimeoutError:
            return None
        writer.close()
        return host

    async def sweep() -> list[str]:
        return [h for h in await asyncio.gather(*(knock(h) for h in hosts)) if h]

    return asyncio.run(sweep())
