"""Signing TARS's browser in, by code (a prototype): the login comes from the vault (tools/browser_vault.py) and goes
only into a page that is that login's site, and a one-time code is asked for on Telegram. A model can ask for this,
and hears how it went, but never sees a password or a code.

Before anything is typed, the page must be at one of the login's origins exactly (as the browser reports its address,
not as the page says it is), over HTTPS; the field must be in the page itself, not in a frame from somewhere else; and
its form, if it has one, must send to that same origin. That's checked again before every step, since each step
loads a new page. A page that fails any of it gets nothing."""

import re
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from browser_vault import Login, for_page, origin
from playwright.sync_api import Page

ROOT = Path(__file__).resolve().parents[1]
STEPS = 5  # a sign-in is a few pages at most: the name, the password, a code
CODE_WAIT_S = 300

# What the page is asking for, from the fields a person could see: in the page's own document only (not frames).
FIELDS_JS = """() => {
  const seen = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled; };
  const all = [...document.querySelectorAll('input')].filter(seen);
  const label = (e) => [e.name, e.id, e.autocomplete, e.getAttribute('aria-label'), e.placeholder]
    .join(' ').toLowerCase();
  const pick = (test) => { const e = all.find(test); if (!e) return null;
    const form = e.form; return {id: e.id, name: e.name, empty: !e.value,
      action: form ? form.getAttribute('action') : null}; };
  const text = (e) => ['text', 'email', 'tel', ''].includes(e.type);
  return {
    code: pick((e) => text(e) && (e.autocomplete === 'one-time-code' ||
                     /otp|one.?time|verif|cvf|mfa|totp|(^|[^a-z])code([^a-z]|$)/.test(label(e)))),
    password: pick((e) => e.type === 'password'),
    username: pick((e) => (e.type === 'email' || text(e)) && /email|user|login|phone|account/.test(label(e))),
  };
}"""


class Refused(Exception):
    """The page isn't the login's site, or its form would send elsewhere."""


def _locate(page: Page, field: dict):
    if field["id"]:
        return page.locator(f"#{_css(field['id'])}").first
    return page.locator(f'input[name="{field["name"]}"]').first


def _css(ident: str) -> str:
    return re.sub(r"([^a-zA-Z0-9_-])", r"\\\1", ident)


def _check(page: Page, login: Login, field: dict) -> None:
    """Raises Refused unless typing into `field` on `page` sends it to `login`'s site and nowhere else."""
    here = origin(page.url)
    if here not in login.origins or not here.startswith("https://"):
        raise Refused(f"the page is at {here}, not {', '.join(login.origins)}")
    action = field.get("action")
    if action and not action.startswith("javascript:"):
        sends_to = origin(urljoin(page.url, action))
        if sends_to not in login.origins:
            raise Refused(f"its form sends to {sends_to}, not {', '.join(login.origins)}")


def _settle(page: Page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
        page.wait_for_timeout(800)
    except Exception:  # noqa: BLE001 - looked at as it is
        return


def sign_in(page: Page, person: Person, goal: str = "") -> str:
    """Signs in on the page as it is, step by step; how it went, for the model. With no login stored for this site,
    the person is asked for one on a TARS page (Person.add_login), and it goes into the vault before anything is typed.
    A one-time code is asked for on Telegram."""
    here = origin(page.url)
    login = for_page(page.url)
    if login is None:
        fields = page.evaluate(FIELDS_JS)
        if not here.startswith("https://") or not (fields["username"] or fields["password"]):
            return f"no sign-in form here ({here}), and no login stored for it"
        if not person.add_login(here, goal):
            return f"stopped: no login for {here} was added in time"
        if origin(page.url) != here or (login := for_page(page.url)) is None:
            return f"stopped: the page moved on from {here} while the login was being added"
    done = []
    for _ in range(STEPS):
        _settle(page)
        if for_page(page.url) is None or for_page(page.url).name != login.name:
            break  # off the site (signed in and sent on, or somewhere else): nothing more to type here
        fields = page.evaluate(FIELDS_JS)
        try:
            if fields["code"]:
                _check(page, login, fields["code"])
                code = person.code(origin(page.url))
                if not code:
                    return "stopped: the person didn't send the sign-in code"
                field = _locate(page, fields["code"])
                field.fill(code)
                field.press("Enter")
                done.append("entered the code they sent")
            elif fields["password"]:
                _check(page, login, fields["password"])
                if fields["username"] and fields["username"]["empty"]:
                    _check(page, login, fields["username"])
                    _locate(page, fields["username"]).fill(login.username)
                field = _locate(page, fields["password"])
                field.fill(login.password)
                field.press("Enter")
                done.append("entered the password")
            elif fields["username"]:
                _check(page, login, fields["username"])
                field = _locate(page, fields["username"])
                field.fill(login.username)
                field.press("Enter")
                done.append("entered the username")
            else:
                break  # nothing left to fill: signed in, or the site wants something only the person can do
        except Refused as e:
            return f"refused to type the login: {e}"
    title = page.title()
    steps = ", ".join(done) or "nothing to fill"
    return f"{steps}; now at {origin(page.url)}, titled {title!r}"


LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TARS: a login for {host}</title>
<style>
  body {{ margin: 0; padding: 24px; background: #14161a; color: #e8e6e1; font: 17px system-ui, sans-serif; }}
  .site {{ font-size: 26px; font-weight: 700; margin: 8px 0 4px; word-break: break-all; }}
  .why {{ color: #a9a69f; margin-bottom: 20px; }}
  label {{ display: block; margin: 14px 0 6px; }}
  input {{ width: 100%; box-sizing: border-box; padding: 12px; border-radius: 10px; border: 1px solid #444;
          background: #22252b; color: inherit; font: inherit; }}
  button {{ margin-top: 22px; width: 100%; padding: 14px; border: 0; border-radius: 10px; background: #3a5f9e;
           color: #fff; font: inherit; font-weight: 600; }}
  .note {{ color: #a9a69f; font-size: 14px; margin-top: 16px; }}
</style></head>
<body>
  <div>TARS needs a login for</div>
  <div class="site">{site}</div>
  <div class="why">{why}</div>
  <form method="post" action="/login">
    <input type="hidden" name="token" value="{token}">
    <label for="u">Email or phone</label>
    <input id="u" name="username" autocomplete="username" autocapitalize="off" spellcheck="false" required>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    <button>Save for {host} and sign in</button>
  </form>
  <p class="note">It's kept encrypted on TARS's machine and typed only into {site}, never shown to TARS's model.
  If that isn't the site you expected, don't fill this in.</p>
</body></html>"""
DONE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<style>body {{ margin: 0; padding: 24px; background: #14161a; color: #e8e6e1; font: 17px system-ui; }}</style></head>
<body><p>{message}</p></body></html>"""
LOGIN_WAIT_S = 600


class Person:
    """The person TARS works for, reached on Telegram ([telegram] people, the first if not named): asked for a
    one-time code in a chat, and for a missing login on a TARS page served at `host` (this machine's Tailscale
    address) for one use."""

    def __init__(self, host: str, person: str = "", port: int = 8091):
        from voice_assistant.check import env_value
        from voice_assistant.config import load_config

        people = load_config(ROOT / "config.toml").telegram.people
        self.chat = int(people[person or next(iter(people))])
        self.api = f"https://api.telegram.org/bot{env_value(ROOT / '.env', 'TELEGRAM_BOT_TOKEN')}"
        self.host, self.port = host, port

    def say(self, text: str) -> None:
        httpx.post(f"{self.api}/sendMessage", json={"chat_id": self.chat, "text": text}, timeout=20)

    def code(self, site: str) -> str | None:
        """A one-time code the person sends in the chat, or None after CODE_WAIT_S."""
        with httpx.Client(timeout=40) as http:
            latest = http.get(f"{self.api}/getUpdates", params={"offset": -1, "timeout": 0}).json().get("result", [])
            offset = latest[-1]["update_id"] + 1 if latest else 0
            self.say(f"{site} is asking TARS's browser for a sign-in code. Reply with the code it sent you.")
            deadline = time.monotonic() + CODE_WAIT_S
            while time.monotonic() < deadline:
                updates = http.get(f"{self.api}/getUpdates", params={"offset": offset, "timeout": 25}).json()
                for u in updates.get("result", []):
                    offset = u["update_id"] + 1
                    message = u.get("message") or {}
                    if message.get("chat", {}).get("id") != self.chat:
                        continue
                    if found := re.search(r"\b\d{4,8}\b", message.get("text", "")):
                        http.get(f"{self.api}/getUpdates", params={"offset": offset, "timeout": 0})  # mark it read
                        self.say("Got it, signing in.")
                        return found.group()
        self.say("No code came in time: I stopped.")
        return None

    def add_login(self, site: str, goal: str) -> bool:
        """Sends the person a one-time link to a page asking for their login for `site` (what code saw the browser at,
        never what a page or a model said), saves what they enter into the vault for that site only, and returns
        whether they did within LOGIN_WAIT_S."""
        import html
        import secrets
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlsplit

        from browser_vault import store

        token, saved = secrets.token_urlsafe(18), threading.Event()
        host = urlsplit(site).hostname or site
        why = f"to finish: {goal}" if goal else "to carry on with what you asked"
        form = LOGIN_PAGE.format(site=html.escape(site), host=html.escape(host), why=html.escape(why), token=token)

        class Page(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # nothing about it in the logs
                return

            def reply(self, status: int, body: str) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                given = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
                if saved.is_set() or not secrets.compare_digest(given, token):
                    self.reply(403, DONE_PAGE.format(message="This link isn't valid any more."))
                else:
                    self.reply(200, form)

            def do_POST(self) -> None:
                length = min(int(self.headers.get("Content-Length") or 0), 10_000)
                sent = parse_qs(self.rfile.read(length).decode())
                given = sent.get("token", [""])[0]
                if saved.is_set() or not secrets.compare_digest(given, token):
                    self.reply(403, DONE_PAGE.format(message="This link isn't valid any more."))
                    return
                try:
                    store(host, [site], sent.get("username", [""])[0].strip(), sent.get("password", [""])[0])
                except ValueError as e:
                    self.reply(
                        400, DONE_PAGE.format(message=f"Not saved: {html.escape(str(e))}. Go back and try again.")
                    )
                    return
                saved.set()
                self.reply(200, DONE_PAGE.format(message=f"Saved for {html.escape(host)}. TARS is signing in."))

        server = ThreadingHTTPServer((self.host, self.port), Page)
        threading.Thread(target=server.serve_forever, daemon=True, name="login-page").start()
        try:
            self.say(
                f"TARS needs to sign in to {host} {why}. Add the login here (the link works once, for "
                f"{LOGIN_WAIT_S // 60} minutes):\nhttp://{self.host}:{self.port}/login?token={token}"
            )
            if not saved.wait(LOGIN_WAIT_S):
                self.say(f"No login for {host} came in time: I stopped.")
                return False
            return True
        finally:
            server.shutdown()
            server.server_close()


def tailscale_address() -> str:
    """This machine's Tailscale address, where the person's phone can reach a TARS page; 127.0.0.1 without one."""
    import subprocess

    for command in (["tailscale", "ip", "-4"], ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "ip", "-4"]):
        try:
            found = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False).stdout.split()
        except OSError, subprocess.TimeoutExpired:
            continue
        if found:
            return found[0]
    return "127.0.0.1"
