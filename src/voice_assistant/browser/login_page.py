"""The one-time page TARS sends a link to when it needs a login it doesn't have, for the site its browser is at (as
code saw it, never as a page or a model said). It asks for what that site's sign-in form asks, by the site's own
labels, as code read them off the form (login.py): an email and a password, a phone number, an ID number. What's
entered goes
into the vault (vault.py) for that site only, and the page stops working: once it's used, or after LOGIN_WAIT_S.

It's served while it's waiting and no longer, from this machine, at the address the person's phone reaches it at (at
home, or over Tailscale). Only the link's token opens it, and only once."""

import html
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LOGIN_WAIT_S = 600
MOST_BYTES = 10_000  # a login form's worth

# TARS's web UI's look (webui_static): its dark paper, ink and accent, its type.
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex">
<title>TARS · a login for {host}</title>
<style>
  :root {{ --paper: #11151c; --ink: #e6e9ef; --muted: #9399a6; --faint: #646b78; --line: #252b36;
           --line-strong: #3c4350; --tint: #1b2029; --raised: #222834; --accent: #8cb0e6; --accent-tint: #1a2537;
           --on-ink: #11151c; --focus: 0 0 0 3px #8cb0e666; color-scheme: dark;
           --serif: "Newsreader", Georgia, "Times New Roman", serif;
           --sans: "Instrument Sans", system-ui, -apple-system, "Segoe UI", sans-serif; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--paper); color: var(--ink); font: 17px/1.5 var(--sans); }}
  main {{ max-width: 440px; margin: 0 auto; padding: 40px 20px 32px; }}
  .who {{ color: var(--muted); font-size: 15px; letter-spacing: .02em; }}
  h1 {{ font: 500 30px/1.2 var(--serif); margin: 6px 0 4px; word-break: break-word; }}
  .why {{ color: var(--muted); margin: 0 0 28px; }}
  label {{ display: block; font-size: 15px; color: var(--muted); margin: 18px 0 6px; }}
  input {{ width: 100%; padding: 13px 14px; border-radius: 12px; border: 1px solid var(--line-strong);
           background: var(--tint); color: var(--ink); font: inherit; }}
  input:focus {{ outline: none; border-color: var(--accent); box-shadow: var(--focus); }}
  button {{ margin-top: 28px; width: 100%; min-height: 48px; border: 0; border-radius: 999px;
            background: var(--accent); color: var(--on-ink); font: 600 17px var(--sans); cursor: pointer; }}
  .safe {{ border-top: 1px solid var(--line); margin-top: 32px; padding-top: 16px; color: var(--faint);
           font-size: 14px; }}
</style></head>
<body><main>
  <div class="who">TARS needs a login for</div>
  <h1>{host}</h1>
  <p class="why">{why}</p>
  <form method="post" action="/login" autocomplete="on">
    <input type="hidden" name="token" value="{token}">
{inputs}
    <button>Save and sign in</button>
  </form>
  <p class="safe">It's kept encrypted on TARS's own machine and typed only into {site}, never shown to TARS's model.
  This page works once. If {host} isn't the site you expected, don't fill it in.</p>
</main></body></html>"""
DONE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>TARS</title><style>body {{ margin: 0; background: #11151c; color: #e6e9ef; font: 17px/1.5 system-ui; }}
main {{ max-width: 440px; margin: 0 auto; padding: 40px 20px; }}</style></head>
<body><main><p>{message}</p></main></body></html>"""


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
        inputs.append(f'    <label for="f{n}">{html.escape(f.label)}</label>\n'
                      f'    <input id="f{n}" name="{html.escape(f.key)}"{kind} autocapitalize="off" spellcheck="false"'
                      f'{" required" if f.required else ""}>')  # fmt: skip
    return PAGE.format(site=html.escape(asking.site), host=html.escape(host), why=html.escape(asking.why),
                       token=token, inputs="\n".join(inputs))  # fmt: skip


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
                self.reply(404, DONE.format(message="This link isn't valid any more."))
                return
            self.reply(200, form)

        def do_POST(self) -> None:
            length = min(int(self.headers.get("Content-Length") or 0), MOST_BYTES)
            sent = parse_qs(self.rfile.read(length).decode(errors="replace"), keep_blank_values=True)
            if urlsplit(self.path).path != "/login" or not self._ours(_one(sent, "token")):
                self.reply(404, DONE.format(message="This link isn't valid any more."))
                return
            values = {f.key: _one(sent, f.key) if f.secret else _one(sent, f.key).strip() for f in asking.fields}
            if any(f.required and not values[f.key] for f in asking.fields):
                self.reply(400, DONE.format(message="Something needed is missing. Go back and fill it in."))
                return
            given.append(values)
            got.set()
            self.reply(200, DONE.format(message="Saved. TARS is signing in; you can close this page."))

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
