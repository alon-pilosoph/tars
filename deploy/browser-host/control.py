"""The control service on the browser's Sprite (docs/browser.md#on-a-sprite): starts TARS's Chrome for a person's
profile when the Telegram service asks, and shuts it after. Only the standard library, since the Sprite has Python
but not TARS.

It listens on the Sprite's localhost only; the Telegram service reaches it through the SSH link's forward, and asks
with a key as well (in ~/browser-host/control-key, put there by setup.sh). Chrome is started as chrome.py starts it on
the Pi: a real window on the virtual screen, its debugging port on localhost only, plus a proxy setting that sends
every connection it makes back through the link to the way out at home (browser/way_out.py). One Chrome at a time,
as errands are one at a time.

    POST /open  {"profile": "profile-0a1b2c3d4e5f"}   start Chrome with that profile; answers once its port does
    POST /close                                        shut it
    GET  /health                                       {"ok": true, "chrome": false}"""

import hmac
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOME = Path.home()
PROFILES = HOME / "profiles"
KEY_FILE = HOME / "browser-host/control-key"
PORT = 9334
CHROME_PORT = 9333
WAY_OUT = "socks5://127.0.0.1:1080"  # the link's reverse forward to the way out at home
DISPLAY = ":99"
WINDOW = (1280, 1000)
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")  # a folder name and nothing else: no dots, no slashes
START_S = 15.0
STOP_S = 10.0


def chrome_command(profile: Path, chrome: str = "/usr/bin/google-chrome") -> list[str]:
    return [
        chrome, f"--user-data-dir={profile}",
        f"--remote-debugging-port={CHROME_PORT}", "--remote-debugging-address=127.0.0.1",
        "--no-first-run", "--no-default-browser-check", f"--window-size={WINDOW[0]},{WINDOW[1]}",
        "--password-store=basic", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
        # Every connection through the way out, the Sprite's own localhost included (where the way out refuses it),
        # and nothing over UDP, which a SOCKS proxy can't carry and the Sprite's network policy drops anyway.
        f"--proxy-server={WAY_OUT}", "--proxy-bypass-list=<-loopback>",
        "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "about:blank",
    ]  # fmt: skip


def answers(port: int = CHROME_PORT) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)  # nosec B310  # localhost
        return True
    except OSError:
        return False


def start_chrome(command: list[str]) -> subprocess.Popen:
    env = {**os.environ, "DISPLAY": DISPLAY}
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


class Host:
    """The one Chrome there is, if any. `launch` and `ready` stand in for starting Chrome and asking its port in the
    tests."""

    def __init__(self, launch=None, ready=answers, profiles: Path = PROFILES):
        self.launch, self.ready, self.profiles = launch or start_chrome, ready, profiles
        self.process: subprocess.Popen | None = None

    def open(self, profile: str) -> None:
        if not PROFILE_NAME.fullmatch(profile):
            raise ValueError("not a profile name")
        self.close()
        folder = self.profiles / profile
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.process = self.launch(chrome_command(folder))
        deadline = time.monotonic() + START_S
        while time.monotonic() < deadline:
            if self.ready():
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.2)
        self.close()
        raise RuntimeError("Chrome didn't start")

    def close(self) -> None:
        """Chrome on Linux saves its profile and quits on SIGTERM, as on being closed."""
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(STOP_S)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process = None

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None


def handler(host: Host, key: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.allowed() and self.path == "/health":
                self.answer(200, {"ok": True, "chrome": host.running()})
            elif self.allowed():
                self.answer(404, {"error": "no such thing"})

        def do_POST(self) -> None:
            if not self.allowed():
                return
            try:
                size = int(self.headers.get("Content-Length") or 0)
                asked = json.loads(self.rfile.read(min(size, 4096)) or b"{}") if size else {}
                if self.path == "/open":
                    host.open(str(asked.get("profile", "")))
                elif self.path == "/close":
                    host.close()
                else:
                    self.answer(404, {"error": "no such thing"})
                    return
            except ValueError as e:
                self.answer(400, {"error": str(e)})
                return
            except RuntimeError as e:
                self.answer(503, {"error": str(e)})
                return
            self.answer(200, {"ok": True, "chrome": host.running()})

        def allowed(self) -> bool:
            given = self.headers.get("X-Key", "").encode()
            if self.client_address[0] == "127.0.0.1" and hmac.compare_digest(given, key):
                return True
            self.answer(403, {"error": "no"})
            return False

        def answer(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args) -> None:
            sys.stderr.write(f"control: {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

    return Handler


def main() -> None:
    key = KEY_FILE.read_text().strip().encode()
    if len(key) < 32:
        sys.exit("control: the key in ~/browser-host/control-key is missing or too short")
    host = Host()
    server = HTTPServer(("127.0.0.1", PORT), handler(host, key))

    def stop(*_) -> None:
        host.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    print(f"control: listening on 127.0.0.1:{PORT}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    finally:
        host.close()


if __name__ == "__main__":
    main()
