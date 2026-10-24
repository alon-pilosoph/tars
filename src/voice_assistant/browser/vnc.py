"""TARS's browser on the Pi's virtual screen, seen and worked from another machine's web browser: for signing in to a
site by hand, where the Pi has no screen of its own (`python -m voice_assistant.browser signin --vnc`).

x11vnc shares the screen (chrome.DISPLAY) on this machine only, behind a password made for this one sign-in, and
noVNC (websockify) serves it as a web page on `host:port`. That's 127.0.0.1 by default, reached from the laptop by
`ssh -L 6080:localhost:6080 pi` or Tailscale; whoever opens it also needs the password, since a signed-in browser
is the person's accounts. Both stop when the sign-in ends."""

import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .chrome import DISPLAY

NOVNC = Path("/usr/share/novnc")  # Debian's noVNC (apt install novnc)
NEEDS = ("x11vnc", "websockify")


@contextmanager
def vnc_view(host: str = "127.0.0.1", port: int = 6080, display: str = DISPLAY) -> Iterator[tuple[str, str]]:
    """The screen as a web page while it lasts: yields its address and its password."""
    missing = [name for name in NEEDS if not shutil.which(name)] + ([] if NOVNC.exists() else ["novnc"])
    if missing:
        raise RuntimeError(f"no {', '.join(missing)}: apt install x11vnc novnc websockify")
    password = secrets.token_urlsafe(6)[:8]  # VNC passwords are 8 characters at most
    with tempfile.TemporaryDirectory() as folder:
        stored = Path(folder) / "passwd"
        subprocess.run(["x11vnc", "-storepasswd", password, str(stored)], check=True, capture_output=True)
        vnc = subprocess.Popen(["x11vnc", "-display", display, "-localhost", "-rfbport", "5900", "-rfbauth",
                                str(stored), "-forever", "-shared", "-quiet"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
        web = subprocess.Popen(["websockify", "--web", str(NOVNC), f"{host}:{port}", "localhost:5900"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
        try:
            time.sleep(1.5)  # both up before the address is handed out
            for process, name in ((vnc, "x11vnc"), (web, "websockify")):
                if process.poll() is not None:
                    raise RuntimeError(f"{name} didn't start (is the virtual screen {display} running?)")
            shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
            yield f"http://{shown}:{port}/vnc.html?autoconnect=1&resize=scale", password
        finally:
            for process in (web, vnc):
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
