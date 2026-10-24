"""TARS's own Chrome: a real browser with its own profile, which stays signed in to what it's been signed in to.

It's started without automation flags (sites refuse sign-in to a browser that says it's automated) and with a
debugging port on this machine only, and Playwright attaches to it. It's a real window, not headless: headless Chrome
gives itself away (it says HeadlessChrome, and its screen is smaller than its window), and some sites answer it with
an "I am human" check. A profile can be open in one Chrome at a time, so a Chrome already running on the port is
closed first, the way quitting it would (so its profile is saved).

On Linux (the Pi) it's Debian's Chromium, drawing to a virtual screen (Xvfb, `deploy/voice-assistant-display.service`)
when there's no real one: the same window as on a desktop, which nothing shows. Pi OS Lite has no desktop keyring
either, so Chromium keeps its logins with `--password-store=basic`: encrypted with a fixed key, so the profile is only
as safe as its folder (TARS's user only, and kept out of the backup). Signing in is done by hand, through a VNC view of
that screen (`python -m voice_assistant.browser signin --vnc`)."""

import json
import os
import platform
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PROFILE = Path.home() / ".cache/tars-browser/profile"
PORT = 9333
WINDOW = (1280, 1000)  # the window, for every run alike: the page then lays out the same way each time
DISPLAY = ":99"  # the virtual screen on Linux, when there's no other (voice-assistant-display.service)
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def executable() -> str:
    if platform.system() == "Darwin" and Path(MAC_CHROME).exists():
        return MAC_CHROME
    for name in ("google-chrome", "chromium", "chromium-browser"):
        if found := shutil.which(name):
            return found
    raise RuntimeError("no Chrome or Chromium found")


@contextmanager
def own_chrome(profile: Path = PROFILE, port: int = PORT, headless: bool = False) -> Iterator[str]:
    """TARS's Chrome for as long as it's needed, then shut; yields the address Playwright attaches to."""
    close(port)
    profile.mkdir(parents=True, exist_ok=True)
    flags = [
        f"--user-data-dir={profile}", f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
        "--no-first-run", "--no-default-browser-check", f"--window-size={WINDOW[0]},{WINDOW[1]}",
    ]  # fmt: skip
    env = dict(os.environ)
    if platform.system() == "Linux":
        flags.append("--password-store=basic")  # no desktop keyring to keep the logins in (see above)
        # No GPU on a virtual screen: WebGL drawn in software, since a browser without it looks like a bot to
        # Cloudflare (rail.co.il's "Just a moment" never clears).
        flags += ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
        if not headless:
            env.setdefault("DISPLAY", DISPLAY)
    if headless:
        flags.append("--headless=new")
    process = subprocess.Popen([executable(), *flags, "about:blank"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env=env)  # fmt: skip
    try:
        for _ in range(75):
            if answers(port):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("TARS's Chrome didn't start")
        yield f"http://127.0.0.1:{port}"
    finally:
        close(port)
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()


def close(port: int = PORT) -> None:
    """Closes the Chrome on `port` if one is running there, through its debugging socket (Playwright won't attach to a
    Chrome with no windows left, and on a Mac closing the last window leaves Chrome running)."""
    from websockets.sync.client import connect

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as answer:
            socket_url = json.load(answer)["webSocketDebuggerUrl"]
    except OSError:
        return
    with connect(socket_url, max_size=None) as socket:
        socket.send(json.dumps({"id": 1, "method": "Browser.close"}))
    for _ in range(50):
        if not answers(port):
            return
        time.sleep(0.2)


def answers(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
        return True
    except OSError:
        return False
