"""The one-time page TARS sends a link to when it needs a login it doesn't have, for the site its browser is at (as
code saw it, never as a page or a model said). It asks for what that site's sign-in form asks, by the site's own
labels, as code read them off the form (login.py): an email and a password, a phone number, an ID number. What's
entered goes
into the vault (vault.py) for that site only, and the page stops working: once it's used, or after LOGIN_WAIT_S.

It's served while it's waiting and no longer, from this machine, at the address the person's phone reaches it at (at
home, or over Tailscale). Only the link's token opens it, and only once."""

import html
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

LOGIN_WAIT_S = 600
MOST_BYTES = 10_000  # a login form's worth

# The page and what comes after it, in the web UI's look (login_page.html, login_done.html beside this file): no
# script, font or image, since the page is served to allow none (ask). Their {names} are filled in by _fill.
PAGE = Path(__file__).with_name("login_page.html").read_text()
DONE = Path(__file__).with_name("login_done.html").read_text()
SLOTS = re.compile(r"\{(host|site|why|token|inputs|message)\}")


def _fill(template: str, **values: str) -> str:
    """`template` with each {name} replaced by its value, in one pass, so a value is never itself filled in."""
    return SLOTS.sub(lambda m: values[m.group(1)], template)


@dataclass(frozen=True)
class Field:
    """One of the site's sign-in fields: its key in the vault (login.py), its label as the site shows it, whether it's
    secret (shown as dots), and whether the page can't go on without it."""

    key: str
    label: str
    secret: bool = False
    required: bool = True


@dataclass(frozen=True)
class Asking:
    """What the page asks for."""

    site: str  # the site it's for, as the browser is at it: https://www.ksp.co.il
    why: str
    fields: tuple[Field, ...]


def page(asking: Asking, token: str) -> str:
    host = urlsplit(asking.site).hostname or asking.site
    inputs = []
    for n, f in enumerate(asking.fields):
        kind = ' type="password" autocomplete="current-password"' if f.secret else (
            ' autocomplete="username"' if f.key == "username" else ' autocomplete="off"')  # fmt: skip
        inputs.append(f'    <label for="f{n}" dir="auto">{html.escape(f.label)}</label>\n'
                      f'    <input id="f{n}" name="{html.escape(f.key)}"{kind} dir="auto" autocapitalize="off"'
                      f' spellcheck="false"{" required" if f.required else ""}>')  # fmt: skip
    return _fill(PAGE, site=html.escape(asking.site), host=html.escape(host), why=html.escape(asking.why),
                 token=token, inputs="\n".join(inputs))  # fmt: skip


def _done(message: str) -> str:
    return _fill(DONE, message=html.escape(message))


def ask(
    asking: Asking,
    send_link: Callable[[str], None],
    host: str,
    port: int,
    wait_s: float = LOGIN_WAIT_S,
) -> dict[str, str] | None:
    """Serves the page on `host:port`, sends its link with `send_link`, and waits up to `wait_s` for the login: each
    field's value by its key, or None if none came. The page stops working as this returns."""
    token, given, got = secrets.token_urlsafe(24), [], threading.Event()
    form = page(asking, token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # nothing about it in the logs
            return

        def reply(self, status: int, body: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _ours(self, given_token: str) -> bool:
            return not got.is_set() and secrets.compare_digest(given_token.encode(), token.encode())

        def do_GET(self) -> None:
            if urlsplit(self.path).path != "/login" or not self._ours(
                _one(parse_qs(urlsplit(self.path).query), "token")
            ):
                self.reply(404, _done("This link isn't valid any more."))
                return
            self.reply(200, form)

        def do_POST(self) -> None:
            try:
                length = max(0, min(int(self.headers.get("Content-Length") or 0), MOST_BYTES))
            except ValueError:
                length = 0
            sent = parse_qs(self.rfile.read(length).decode(errors="replace"), keep_blank_values=True)
            if urlsplit(self.path).path != "/login" or not self._ours(_one(sent, "token")):
                self.reply(404, _done("This link isn't valid any more."))
                return
            values = {f.key: _one(sent, f.key) if f.secret else _one(sent, f.key).strip() for f in asking.fields}
            if any(f.required and not values[f.key] for f in asking.fields):
                self.reply(400, _done("Something needed is missing. Go back and fill it in."))
                return
            given.append(values)
            got.set()
            self.reply(200, _done("Saved. TARS is signing in; you can close this page."))

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="login-page").start()
    try:
        shown = f"[{host}]" if ":" in host else host
        send_link(f"http://{shown}:{port}/login?token={token}")
        return given[0] if got.wait(wait_s) else None
    finally:
        server.shutdown()
        server.server_close()


def _one(fields: dict[str, list[str]], name: str) -> str:
    return (fields.get(name) or [""])[0]


def reachable_address() -> str:
    """This machine's Tailscale address, where the person's phone can reach a page from anywhere, else its address
    on the home network."""
    import shutil
    import socket
    import subprocess

    for tailscale in (shutil.which("tailscale"), "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if not tailscale:
            continue
        try:
            found = subprocess.run([tailscale, "ip", "-4"], capture_output=True, text=True, timeout=5, check=False)
        except OSError, subprocess.TimeoutExpired:
            continue
        if address := found.stdout.split()[:1]:
            return address[0]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:  # the address a packet out would leave from
        try:
            probe.connect(("192.0.2.1", 9))  # nothing is sent: a UDP connect only picks the way out
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"
