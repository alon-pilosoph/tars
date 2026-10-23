"""Signing TARS's browser in, by code (a prototype): the login comes from the vault (tools/browser_vault.py) and goes
only into a page that is that login's site, and a one-time code is asked for on Telegram. A model can ask for this,
and hears how it went, but never sees a password or a code.

Before anything is typed, the page must be at one of the login's origins exactly (as the browser reports its address,
not as the page says it is), over HTTPS; the field must be in the page itself, not in a frame from somewhere else; and
its form, if it has one, must send to that same origin. That's checked again before every step, since each step
loads a new page. A page that fails any of it gets nothing."""

import re
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


def sign_in(page: Page, ask_code) -> str:
    """Signs in on the page as it is, step by step; how it went, for the model. `ask_code(site)` gets a one-time code
    from the person (None if they didn't send one)."""
    login = for_page(page.url)
    if login is None:
        return f"no login is stored for {origin(page.url)}: the person has to add one (browser_vault.py add)"
    done = []
    for _ in range(STEPS):
        _settle(page)
        if for_page(page.url) is None or for_page(page.url).name != login.name:
            break  # off the site (signed in and sent on, or somewhere else): nothing more to type here
        fields = page.evaluate(FIELDS_JS)
        try:
            if fields["code"]:
                _check(page, login, fields["code"])
                code = ask_code(login.name)
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


def telegram_asker(person: str = ""):
    """ask_code for the person on Telegram ([telegram] people, the first if not named), from TARS's bot."""
    from voice_assistant.check import env_value
    from voice_assistant.config import load_config

    cfg = load_config(ROOT / "config.toml")
    people = cfg.telegram.people
    name = person or next(iter(people))
    chat = int(people[name])
    token = env_value(ROOT / ".env", "TELEGRAM_BOT_TOKEN")
    api = f"https://api.telegram.org/bot{token}"

    def ask(site: str) -> str | None:
        with httpx.Client(timeout=40) as http:
            latest = http.get(f"{api}/getUpdates", params={"offset": -1, "timeout": 0}).json().get("result", [])
            offset = latest[-1]["update_id"] + 1 if latest else 0
            asking = f"{site} is asking TARS's browser for a sign-in code. Reply with the code it sent you."
            http.post(f"{api}/sendMessage", json={"chat_id": chat, "text": asking})
            deadline = time.monotonic() + CODE_WAIT_S
            while time.monotonic() < deadline:
                updates = http.get(f"{api}/getUpdates", params={"offset": offset, "timeout": 25}).json()
                for u in updates.get("result", []):
                    offset = u["update_id"] + 1
                    message = u.get("message") or {}
                    if message.get("chat", {}).get("id") != chat:
                        continue
                    if found := re.search(r"\b\d{4,8}\b", message.get("text", "")):
                        http.get(f"{api}/getUpdates", params={"offset": offset, "timeout": 0})  # mark it read
                        http.post(f"{api}/sendMessage", json={"chat_id": chat, "text": "Got it, signing in."})
                        return found.group()
            http.post(f"{api}/sendMessage", json={"chat_id": chat, "text": "No code came in time: I stopped."})
        return None

    return ask
