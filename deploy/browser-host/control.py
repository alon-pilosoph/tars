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
    GET  /health                                       {"ok": true, "chrome": false}
    POST /view                                         a hand-over: {"token": "...", "minutes": 15}
    GET  /view                                         {"active": true, "opened": false}
    POST /view/end                                     end it

A hand-over is the person taking over this Chrome's screen from their phone, for what only a person can do (a
captcha, signing in with Google). The Sprite's public address (https://<sprite>.sprites.app) is served by the gate
below, which answers nothing at all unless a hand-over is on. Then it lets in one browser, the first to bring the
hand-over's token, and only that browser: it starts the screen's VNC server (on localhost only) and noVNC's viewer
behind it, and passes that browser's requests through to them. The hand-over ends when the Telegram service says so
(the person tapped Done), or after VIEW_S, and the VNC server and viewer are stopped, which drops the connection."""

import hmac
import json
import os
import re
import secrets
import selectors
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
GATE_PORT = 8080  # the Sprite's public address is routed here (setup.sh: the control service's --http-port)
VNC_PORT, VIEWER_PORT = 5900, 6080  # both on localhost, and only while a hand-over is on
VIEW_S = 15 * 60.0  # the longest a hand-over lasts
COOKIE = "tars-view"
HEAD_MAX = 16384  # the most of a request's head the gate reads
VIEWER_PAGE = "/vnc.html?autoconnect=1&resize=scale&reconnect=1&path=websockify"


def vnc_command() -> list[str]:
    return ["/usr/bin/x11vnc", "-display", DISPLAY, "-localhost", "-rfbport", str(VNC_PORT), "-forever",
            "-nopw", "-quiet", "-noxdamage"]  # fmt: skip


def viewer_command() -> list[str]:
    return ["/usr/bin/websockify", "--web", "/usr/share/novnc", f"127.0.0.1:{VIEWER_PORT}", f"127.0.0.1:{VNC_PORT}"]


def start_quietly(command: list[str]) -> subprocess.Popen:
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        """Stops Chrome if it's still running. The Telegram service closes it first through its debugging port, as a
        person closes it, which is what saves the cookies it was given; SIGTERM here loses the newest of them, so
        it's for a Chrome that didn't go."""
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


class View:
    """The hand-over there is, if any: its token, whether a browser has come with it, and what that browser was
    given to come back with. `launch` and `clock` stand in for starting the VNC server and the viewer, and the time,
    in the tests."""

    def __init__(self, launch=start_quietly, clock=time.monotonic):
        self.launch, self.clock = launch, clock
        self.lock = threading.Lock()
        self.token = self.secret = ""
        self.until = 0.0
        self.opened = False
        self.processes: list[subprocess.Popen] = []

    def start(self) -> str:
        """A new hand-over, ending any other: its token, for the person's link."""
        self.end()
        with self.lock:
            self.token, self.secret, self.opened = secrets.token_urlsafe(32), "", False
            self.until = self.clock() + VIEW_S
            self.processes = [self.launch(vnc_command()), self.launch(viewer_command())]
            return self.token

    def active(self) -> bool:
        with self.lock:
            if self.token and self.clock() >= self.until:
                self._end()
            return bool(self.token)

    def admit(self, token: str) -> str | None:
        """The secret a browser comes back with, for the first to bring the token; None for anyone else."""
        if not self.active():
            return None
        with self.lock:
            if self.opened or not hmac.compare_digest(token.encode(), self.token.encode()):
                return None
            self.opened, self.secret = True, secrets.token_urlsafe(32)
            return self.secret

    def allowed(self, secret: str) -> bool:
        if not self.active():
            return False
        with self.lock:
            return bool(self.secret) and hmac.compare_digest(secret.encode(), self.secret.encode())

    def status(self) -> dict:
        active = self.active()
        with self.lock:
            return {"active": active, "opened": self.opened}

    def end(self) -> None:
        with self.lock:
            self._end()

    def _end(self) -> None:
        self.token = self.secret = ""
        self.opened = False
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
        self.processes = []


class Gate(socketserver.ThreadingTCPServer):
    """What the Sprite's public address reaches. Nothing, unless a hand-over is on; then /view?t=<token> once, which
    gives that browser a cookie and sends it to the viewer, and every request with that cookie is passed through to
    the viewer as it came (its pages, and the screen's WebSocket)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, view: View, port: int = GATE_PORT, viewer_port: int = VIEWER_PORT):
        self.view, self.viewer_port = view, viewer_port
        super().__init__(("127.0.0.1", port), _GateHandler)


class _GateHandler(socketserver.BaseRequestHandler):
    server: Gate

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(30)
        try:
            head = _head(client)
        except OSError, ValueError:
            return
        line, _, rest = head.partition(b"\r\n")
        parts = line.decode("latin-1").split(" ")
        if len(parts) != 3:
            return _plain(client, 400)
        target = urlsplit(parts[1])
        view = self.server.view
        if not view.active():
            return _plain(client, 404)
        if target.path == "/view":
            secret = view.admit(parse_qs(target.query).get("t", [""])[0])
            if secret is None:
                return _plain(client, 403)
            print("control: the screen was opened", file=sys.stderr, flush=True)
            cookie = f"{COOKIE}={secret}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={int(VIEW_S)}"
            return _plain(client, 302, [("Location", VIEWER_PAGE), ("Set-Cookie", cookie)])
        if not view.allowed(_cookie(rest, COOKIE)):
            return _plain(client, 403)
        try:
            viewer = _connect_when_up(self.server.viewer_port)
        except OSError:
            return _plain(client, 503)
        with viewer:
            viewer.sendall(head)
            _pump(client, viewer)


def _head(client: socket.socket) -> bytes:
    """A request's line and headers, and whatever came after them in the same read."""
    got = b""
    while b"\r\n\r\n" not in got:
        chunk = client.recv(4096)
        if not chunk:
            raise ValueError("closed")
        got += chunk
        if len(got) > HEAD_MAX:
            raise ValueError("too long")
    return got


def _cookie(headers: bytes, name: str) -> str:
    for line in headers.decode("latin-1").split("\r\n"):
        key, _, value = line.partition(":")
        if key.strip().lower() == "cookie":
            for pair in value.split(";"):
                k, _, v = pair.strip().partition("=")
                if k == name:
                    return v
    return ""


def _plain(client: socket.socket, status: int, headers: list[tuple[str, str]] = ()) -> None:
    reason = {302: "Found", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 503: "Unavailable"}[status]
    lines = [f"HTTP/1.1 {status} {reason}", "Content-Length: 0", "Connection: close", "Cache-Control: no-store",
             *(f"{k}: {v}" for k, v in headers)]  # fmt: skip
    try:
        client.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    except OSError:
        return


def _connect_when_up(port: int, wait_s: float = 10.0) -> socket.socket:
    """The viewer was just started with the hand-over: it may take a moment to listen."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def _pump(one: socket.socket, other: socket.socket) -> None:
    one.setblocking(False)
    other.setblocking(False)
    peer = {one: other, other: one}
    with selectors.DefaultSelector() as waiting:
        for side in peer:
            waiting.register(side, selectors.EVENT_READ)
        while ready := waiting.select(VIEW_S):
            for key, _ in ready:
                try:
                    data = key.fileobj.recv(65536)
                    if not data:
                        return
                    peer[key.fileobj].settimeout(30)
                    peer[key.fileobj].sendall(data)
                    peer[key.fileobj].setblocking(False)
                except OSError:
                    return


def handler(host: Host, key: bytes, view: View | None = None):
    view = view or View()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if not self.allowed():
                return
            if self.path == "/health":
                self.answer(200, {"ok": True, "chrome": host.running()})
            elif self.path == "/view":
                self.answer(200, view.status())
            else:
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
                    view.end()
                    host.close()
                elif self.path == "/view":
                    self.answer(200, {"token": view.start(), "minutes": int(VIEW_S // 60)})
                    return
                elif self.path == "/view/end":
                    view.end()
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
    host, view = Host(), View()
    server = HTTPServer(("127.0.0.1", PORT), handler(host, key, view))
    gate = Gate(view)
    threading.Thread(target=gate.serve_forever, daemon=True, name="gate").start()

    def stop(*_) -> None:
        view.end()
        host.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    print(f"control: listening on 127.0.0.1:{PORT}, the gate on 127.0.0.1:{GATE_PORT}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    finally:
        view.end()
        host.close()


if __name__ == "__main__":
    main()
