"""TARS's Chrome on a Fly Sprite instead of this machine ([browser] sprite, docs/browser.md#on-a-sprite): the same
browser, profile per person and all, so the errands stop needing a desktop at home, while the pages still load from
the home's address and the vault never leaves.

For each errand this machine opens one SSH connection to the Sprite, through `sprite proxy` (the Sprite has no
public port), and keeps it for the errand. Through it:
- this machine reaches the Sprite's control service (deploy/browser-host/control.py), which starts Chrome with the
  person's profile, and Chrome's debugging port, which Playwright attaches to; both listen on the Sprite's localhost;
- the Sprite's Chrome reaches the internet: its proxy setting points at the link's reverse forward, which ends at
  the way out on this machine (way_out.py), and the way out refuses anything at home.

The Sprite's own network policy denies everything else, so a Chrome that skipped its proxy would load nothing. The
key the link uses may open those three forwards and nothing else, not even a command (deploy/browser-host/setup.sh
sets that up). Opening the link wakes a sleeping Sprite; closing it lets it sleep again, which is what it's billed
by."""

import json
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..config import SPRITE_NAME
from .chrome import PROFILE
from .way_out import WayOut

FOLDER = PROFILE.parent  # beside the vault, in the folder only TARS's user can read (vault.private_folder)
LINK_KEY = FOLDER / "sprite-link"  # the SSH key the link signs in with; its .pub is on the Sprite
KNOWN_HOSTS = FOLDER / "sprite-known-hosts"  # the Sprite's host key, recorded by setup.sh: a different one is refused
CONTROL_KEY = FOLDER / "sprite-control-key"  # what the control service asks for besides the link
SSH_PORT, CHROME_PORT, CONTROL_PORT, WAY_OUT_PORT = 2222, 9333, 9334, 1080  # all on the Sprite's localhost
UP_S = 60.0  # how long the Sprite is given to wake and its control service to answer
OPEN_S = 30.0  # how long Chrome is given to start there


class SpriteUnreachable(RuntimeError):
    """The Sprite didn't answer, or didn't start Chrome: the errand can't run, and says why."""


def sprite_cli() -> str:
    for found in (shutil.which("sprite"), str(Path.home() / ".local/bin/sprite")):
        if found and Path(found).exists():
            return found
    raise SpriteUnreachable("the sprite command isn't installed (deploy/browser-host/setup.sh)")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def ssh_command(sprite: str, chrome: int, control: int, way_out: int, cli: str) -> list[str]:
    """The SSH link to `sprite`: Chrome's port and the control service's to `chrome` and `control` here, and the
    Sprite's port 1080 back to the way out on `way_out` here. No command runs, nothing else is forwarded, and a host
    key other than the recorded one ends it."""
    if not re.fullmatch(SPRITE_NAME, sprite) or any(c.isspace() or c in "%\"'" for c in cli):
        raise SpriteUnreachable(f"not a Sprite's name: {sprite!r}")  # ssh runs ProxyCommand with a shell
    return [
        "ssh", "-N", "-F", "none",
        "-i", str(LINK_KEY), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "StrictHostKeyChecking=yes", "-o", f"HostKeyAlias={sprite}",
        "-o", f"ProxyCommand={cli} proxy -s {sprite} -W :{SSH_PORT}",
        "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "-o", "ClearAllForwardings=no", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
        "-L", f"127.0.0.1:{chrome}:localhost:{CHROME_PORT}",
        "-L", f"127.0.0.1:{control}:localhost:{CONTROL_PORT}",
        "-R", f"localhost:{WAY_OUT_PORT}:127.0.0.1:{way_out}",
        f"sprite@{sprite}",
    ]  # fmt: skip


class Control:
    """The control service on the Sprite, through the link's forward."""

    def __init__(self, port: int, key: str):
        self.base, self.key = f"http://127.0.0.1:{port}", key

    def ask(self, path: str, body: dict | None = None, timeout: float = 5.0) -> dict:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, headers={"X-Key": self.key})
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # nosec B310  # the link's forward, localhost
            return json.load(answer)

    def health(self) -> dict:
        return self.ask("/health", timeout=2.0)

    def open(self, profile: str) -> None:
        try:
            self.ask("/open", {"profile": profile}, timeout=OPEN_S)
        except urllib.error.HTTPError as e:
            raise SpriteUnreachable(f"the Sprite couldn't start Chrome ({e.code})") from e
        except OSError as e:
            raise SpriteUnreachable(f"the Sprite didn't start Chrome in time ({e})") from e

    def close(self) -> None:
        self.ask("/close", {}, timeout=15.0)


@contextmanager
def sprite_chrome(
    sprite: str, profile: Path, home: frozenset[str] = frozenset(), run=subprocess.Popen
) -> Iterator[str]:
    """TARS's Chrome on `sprite` with `profile`'s name, for as long as it's needed, then shut and the link closed;
    yields the address Playwright attaches to, as chrome.own_chrome does. `home`: the house's own names, which the
    way out refuses besides any home network's."""
    with link(sprite, home, run) as (control, chrome):
        control.open(profile.name)
        try:
            yield f"http://127.0.0.1:{chrome}"
        finally:
            try:
                control.close()
            except OSError:
                pass  # the link went; the Sprite's Chrome goes when it sleeps


def reachable(sprite: str, run=subprocess.Popen) -> dict:
    """Wakes `sprite`, asks its control service how it is through a link, and lets it sleep again: for --check."""
    with link(sprite, frozenset(), run) as (control, _):
        return control.health()


@contextmanager
def link(sprite: str, home: frozenset[str], run=subprocess.Popen) -> Iterator[tuple[Control, int]]:
    """The link to `sprite`, with the way out running behind it, once its control service answers: (the control
    service, the port here Chrome's debugging port is forwarded to)."""
    if not (LINK_KEY.exists() and KNOWN_HOSTS.exists() and CONTROL_KEY.exists()):
        raise SpriteUnreachable(f"no link to {sprite} set up here yet (deploy/browser-host/setup.sh)")
    chrome, control_port = free_port(), free_port()
    with WayOut(home=home) as way_out, tempfile.TemporaryFile() as said:
        ssh = run(ssh_command(sprite, chrome, control_port, way_out.port, sprite_cli()),
                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=said)  # fmt: skip
        try:
            control = Control(control_port, CONTROL_KEY.read_text().strip())
            _wait_up(ssh, said, control, sprite)
            yield control, chrome
        finally:
            ssh.terminate()
            try:
                ssh.wait(5)
            except subprocess.TimeoutExpired:
                ssh.kill()


def _wait_up(link, said, control: Control, sprite: str) -> None:
    """Until the control service answers through the link; SpriteUnreachable, with ssh's last word, if it can't."""
    deadline = time.monotonic() + UP_S
    while time.monotonic() < deadline:
        if link.poll() is not None:
            said.seek(0)
            lines = said.read().decode(errors="replace").strip().splitlines()
            raise SpriteUnreachable(f"the link to {sprite} closed: {lines[-1] if lines else 'no reason given'}")
        try:
            control.health()
            return
        except urllib.error.HTTPError as e:
            raise SpriteUnreachable(f"{sprite}'s control service refused this machine ({e.code})") from e
        except OSError:
            time.sleep(0.5)
    raise SpriteUnreachable(f"{sprite} didn't answer in {UP_S:.0f} s")
