"""Signing TARS's browser in, by code: the model calls sign_in, and this types the person's login from the vault
(vault.py) into the page, step by step (the username, the password, a code the site sends them). The model hears
how it went and never sees a password or a code.

Before anything is typed, the page must be at one of the login's origins exactly (as the browser reports its address,
not as the page says it is), over HTTPS; the field must be in the page itself, not in a frame from somewhere else; and
its form, if it has one, must send to that same origin. That's checked again before every step, since each step loads
a new page. A page that fails any of it gets nothing.

A login TARS doesn't have is asked of the person (Person.login) for the site the browser is at, and kept in the vault
for that site only. A site that signs in by email alone (Wolt) gets the address of the person's mailbox TARS reads.
A code the site sends is looked for in that mail (mail.py) and asked of the person at once, and whichever comes first
is typed; a sign-in link it emails is opened, if it's on the site's own domain. TARS signs in to accounts the person
has: a page that wants a name to sign up gets nothing."""

import re
import threading
import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from . import mail
from .vault import Login, Vault, origin

STEPS = 6  # a sign-in is a few pages at most: the name, the password, a code, a link
MAIL_WAIT_S = 180  # how long a sign-in email may take to come
CODE_WAIT_S = 300  # how long the person has to send a code

# What the page is asking for, from the fields a person could see and reach: in the page's own document only (not
# frames), and not one kept from people (aria-hidden, out of the tab order: Booking.com's decoy for password managers).
FIELDS_JS = """() => {
  const seen = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled &&
      !e.closest('[aria-hidden=true]') && e.tabIndex >= 0; };
  const all = [...document.querySelectorAll('input')].filter(seen);
  const label = (e) => [e.name, e.id, e.autocomplete, e.getAttribute('aria-label'), e.placeholder]
    .join(' ').toLowerCase();
  const pick = (test) => { const e = all.find(test); if (!e) return null;
    const form = e.form; return {id: e.id, name: e.name, empty: !e.value,
      action: form ? form.getAttribute('action') : null}; };
  const text = (e) => ['text', 'email', 'tel', 'number', ''].includes(e.type);
  return {
    code: pick((e) => text(e) && (e.autocomplete === 'one-time-code' ||
                     /otp|one.?time|verif|mfa|totp|(^|[^a-z])code([^a-z]|$)|קוד|אימות/.test(label(e)))),
    password: pick((e) => e.type === 'password'),
    username: pick((e) => e.type === 'email' || e.type === 'tel' ||
                     (text(e) && /email|mail|user|login|phone|mobile|account|טלפון|נייד|דוא"?ל|מייל/.test(label(e)))),
    signup: all.some((e) => /first.?name|last.?name|full.?name|given.?name|family.?name/.test(label(e))),
  };
}"""

# What the page itself says went wrong: its alerts and error boxes, briefly.
ALERTS_JS = """() => [...document.querySelectorAll('[role=alert], .error, [class*=error-message]')]
  .filter((e) => e.offsetWidth > 0).map((e) => e.innerText.replace(/\\s+/g, ' ').trim()).filter(Boolean)
  .slice(0, 3).join(' | ').slice(0, 300)"""


class Answer:
    """A one-time code, from whichever gives it first: the person, or their email (`mail`: it's being looked for)."""

    def __init__(self, mail: bool = False):
        self.mail, self.code, self.came_from = mail, None, ""
        self.done = threading.Event()
        self._lock = threading.Lock()

    def give(self, code: str, came_from: str) -> bool:
        """True if this is the answer; False if one came first."""
        with self._lock:
            if self.done.is_set() or not code:
                return False
            self.code, self.came_from = code, came_from
            self.done.set()
            return True

    def wait(self, timeout: float) -> str | None:
        self.done.wait(timeout)
        return self.code


class Person(Protocol):
    """Whose logins and mail these are, and how they're asked for what TARS lacks."""

    name: str

    def login(self, site: str, goal: str) -> tuple[str, str] | None:
        """Their username and password for `site` (an origin the browser is at), or None if they gave none."""
        ...

    def code(self, site: str, answer: Answer) -> str | None:
        """The one-time code `site` just sent them: what they give, or `answer` meanwhile (from their email); None if
        neither came."""
        ...


class Unattended:
    """A person nobody can ask (an eval, a run with no one at it): the vault's logins and mail are used, and nothing
    is added."""

    def __init__(self, name: str):
        self.name = name

    def login(self, site: str, goal: str) -> tuple[str, str] | None:
        return None

    def code(self, site: str, answer: Answer) -> str | None:
        return answer.wait(MAIL_WAIT_S) if answer.mail else None


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
    except PlaywrightError:  # looked at as it is
        return


def _said(page: Page) -> str:
    try:
        said = page.evaluate(ALERTS_JS)
    except PlaywrightError:
        return ""
    return f"; the page says: {said}" if said else ""


def sign_in(
    page: Page,
    vault: Vault,
    person: Person,
    goal: str = "",
    hide: Callable[[str], None] = lambda s: None,
    wait_for_mail: Callable[..., tuple[str | None, str | None]] = mail.wait_for_sign_in,
) -> str:
    """Signs `person` in on the page as it is; how it went, for the model. `hide` is told each secret before it's
    typed (the password, a code), for whatever keeps what the page sends to leave it out."""
    here = origin(page.url)
    login, boxes = vault.for_page(page.url, person.name), vault.mailboxes(person.name)
    if login is None:
        fields = page.evaluate(FIELDS_JS)
        if not here.startswith("https://") or not (fields["username"] or fields["password"]):
            return f"no sign-in form here ({here}) and no login for it: go to the site's sign-in page first"
        if fields["username"] and not fields["password"] and boxes:  # a site that may sign in by email alone
            given = (boxes[0].username, "")
        else:
            given = person.login(here, goal)
        if not given:
            return f"TARS has no login for {here} and none was given: call done and say it needs one"
        if origin(page.url) != here:
            return f"stopped: the page moved on from {here} while the login was being asked for"
        try:
            login = vault.store(person.name, [here], *given)
        except ValueError as e:
            return f"the login given for {here} wasn't kept: {e}"
    done, sent, asked = [], set(), time.time()  # each kind of field is filled once: asked again, it's not retried
    for _ in range(STEPS):
        _settle(page)
        current = vault.for_page(page.url, person.name)
        if current is None or current.name != login.name:
            break  # off the site (signed in and sent on, or somewhere else): nothing more to type here
        fields = page.evaluate(FIELDS_JS)
        if fields["signup"]:
            return f"{origin(page.url)} is asking to sign up, which TARS doesn't do: call done and say so"
        wanted = (
            "code" if fields["code"] else "password" if fields["password"] else "username" if fields["username"] else ""
        )
        if not wanted:
            if done[-1:] != ["entered the username"] or not boxes or "link" in sent:
                break  # nothing left to fill: signed in, or the site wants something only the person can do
            sent.add("link")  # "check your email": the site sent a sign-in link, or a code for a page still to come
            code, link = wait_for_mail(boxes, page.url, asked, MAIL_WAIT_S)
            if link:
                page.goto(link, wait_until="domcontentloaded", timeout=20_000)
                done.append("opened the sign-in link from their email")
                continue
            return (
                f"{origin(page.url)} wants something after the username"
                + (", and no sign-in email came" if not code else " that its email's code has nowhere to go")
                + _said(page)
            )
        if wanted in sent:  # asked again for what was just given: wrong, refused, or a check only a person passes
            return (f"stopped after {', '.join(done)}: {origin(page.url)} asked for the {wanted} again"
                    f"{_said(page)}. Not retrying: tell the person.")  # fmt: skip
        sent.add(wanted)
        try:
            _check(page, login, fields[wanted])
            if wanted == "code":
                code = _code(page, person, boxes, asked, wait_for_mail)
                if not code:
                    return f"{origin(page.url)} wants the code it sent the person, and none came"
                hide(code)
                field = _locate(page, fields["code"])
                field.fill(code)
                done.append("entered the code they were sent")
            elif wanted == "password":
                if not login.password:
                    return f"{origin(page.url)} wants a password, and TARS's login for it has none: tell the person"
                if fields["username"] and fields["username"]["empty"]:
                    _check(page, login, fields["username"])
                    _locate(page, fields["username"]).fill(login.username)
                hide(login.password)
                field = _locate(page, fields["password"])
                field.fill(login.password)
                done.append("entered the password")
            else:
                field = _locate(page, fields["username"])
                field.fill(login.username)
                done.append("entered the username")
            asked = time.time()
            field.press("Enter")
        except Refused as e:
            return f"refused to type the login: {e}"
    _settle(page)
    steps = ", ".join(done) or "nothing to fill"
    return f"{steps}; now at {origin(page.url)}, titled {page.title()!r}{_said(page)}"


def _code(page: Page, person: Person, boxes: list[Login], asked: float, wait_for_mail) -> str | None:
    """The code the site sent: from the person, or meanwhile from their email, whichever is first."""
    answer = Answer(mail=bool(boxes))
    if boxes:
        url = page.url

        def look() -> None:
            code, _ = wait_for_mail(boxes, url, asked, MAIL_WAIT_S, stop=answer.done)
            if code:
                answer.give(code, "email")

        threading.Thread(target=look, daemon=True, name="sign-in-mail").start()
    try:
        return person.code(origin(page.url), answer)
    finally:
        answer.done.set()  # whoever answered, the mail stops being looked at
