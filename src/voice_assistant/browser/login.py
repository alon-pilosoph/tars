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
from .login_page import Field
from .vault import Login, Vault, origin

STEPS = 6  # a sign-in is a few pages at most: the name, the password, a code, a link
MAIL_WAIT_S = 180  # how long a sign-in email may take to come
CODE_WAIT_S = 300  # how long the person has to send a code

# What the page is asking for: the fields of its sign-in form a person could see and reach, each with its label as
# the site shows it. In the page's own document only (not frames); not a field kept from people (aria-hidden, out of
# the tab order: Booking.com's decoy for password managers) or a search box; and only those in the same form (or
# dialog) as the password, else the username, so a newsletter's email box elsewhere on the page isn't one of them.
FIELDS_JS = """() => {
  const seen = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled &&
      !e.closest('[aria-hidden=true]') && e.tabIndex >= 0; };
  const all = [...document.querySelectorAll('input')].filter(seen);
  const words = (e) => [e.name, e.id, e.autocomplete, e.getAttribute('aria-label'), e.placeholder]
    .join(' ').toLowerCase();
  const labelOf = (e) => ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') ||
    e.placeholder || e.name || e.id || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
  const text = (e) => ['text', 'email', 'tel', 'number', ''].includes(e.type);
  const isCode = (e) => text(e) && (e.autocomplete === 'one-time-code' ||
    /otp|one.?time|verif|mfa|totp|(^|[^a-z])code([^a-z]|$)|קוד|אימות/.test(words(e)));
  const isUser = (e) => e.type === 'email' || e.type === 'tel' ||
    (text(e) && /email|mail|user|login|phone|mobile|account|טלפון|נייד|דוא"?ל|מייל/.test(words(e)));
  const isSearch = (e) => e.type === 'search' || !!e.closest('[role=search]') ||
    /search|query|חיפוש|(^|[^a-z])q([^a-z]|$)/.test(words(e));
  const own = (e) => (e.autocomplete && !['on', 'off'].includes(e.autocomplete)) ? e.autocomplete :
    (e.name || e.id || labelOf(e));
  const describe = (e, kind) => ({id: e.id, name: e.name, kind, label: labelOf(e), empty: !e.value,
    key: kind === 'other' ? own(e).toLowerCase() : kind, action: e.form ? e.form.getAttribute('action') : null});
  const code = all.find(isCode);
  let inputs = all.filter((e) => e !== code && (e.type === 'password' || text(e)) && !isSearch(e));
  const anchor = inputs.find((e) => e.type === 'password') || inputs.find(isUser);
  const scope = anchor && (anchor.form || anchor.closest('dialog, [role=dialog], [aria-modal=true]'));
  if (scope) inputs = inputs.filter((e) => scope.contains(e));
  let user = false;
  const fields = inputs.map((e) => {
    const kind = e.type === 'password' ? 'password' : (!user && isUser(e)) ? 'username' : 'other';
    user = user || kind === 'username';
    return describe(e, kind);
  });
  return {code: code ? describe(code, 'code') : null, fields,
    signup: all.some((e) => /first.?name|last.?name|full.?name|given.?name|family.?name/.test(words(e))) ||
      inputs.filter((e) => e.type === 'password').length > 1};
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

    def login(self, site: str, goal: str, fields: list[Field]) -> dict[str, str] | None:
        """What they sign in to `site` (an origin the browser is at) with, for each of `fields` (the site's sign-in
        fields TARS has nothing for), by key; None if they gave none."""
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

    def login(self, site: str, goal: str, fields: list[Field]) -> dict[str, str] | None:
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
    typed (a password, a code), for whatever keeps what the page sends to leave it out."""
    here = origin(page.url)
    login, boxes = vault.for_page(page.url, person.name), vault.mailboxes(person.name)
    if login is None:
        fields = page.evaluate(FIELDS_JS)["fields"]
        if not here.startswith("https://") or not fields:
            return f"no sign-in form here ({here}) and no login for it: go to the site's sign-in page first"
        kinds = {f["kind"] for f in fields}
        if kinds == {"username"} and boxes:  # a site that may sign in by email alone
            values = {"username": boxes[0].username}
        else:
            wanted = [_field(f) for f in fields]
            if "password" not in kinds:  # it may ask on the next page: one link, not two
                wanted.append(Field("password", "Password, if it asks for one next", secret=True, required=False))
            values = person.login(here, goal, wanted)
        if not values:
            return f"TARS has no login for {here} and none was given: call done and say it needs one"
        if origin(page.url) != here:
            return f"stopped: the page moved on from {here} while the login was being asked for"
        try:
            login = vault.store(person.name, [here], values)
        except ValueError as e:
            return f"the login given for {here} wasn't kept: {e}"
    done, sent, asked, last = [], set(), time.time(), ()  # what each page asked is given once: asked again, it stops
    for _ in range(STEPS):
        _settle(page)
        current = vault.for_page(page.url, person.name)
        if current is None or current.name != login.name:
            break  # off the site (signed in and sent on, or somewhere else): nothing more to type here
        found = page.evaluate(FIELDS_JS)
        if found["signup"]:
            return f"{origin(page.url)} is asking to sign up, which TARS doesn't do: call done and say so"
        try:
            if found["code"]:
                if "code" in sent:
                    return _again(page, done, "the code")
                sent.add("code")
                _check(page, login, found["code"])
                code = _code(page, person, boxes, asked, wait_for_mail)
                if not code:
                    return f"{origin(page.url)} wants the code it sent the person, and none came"
                hide(code)
                field = _locate(page, found["code"])
                field.fill(code)
                done.append("entered the code they were sent")
                last = ("code",)
            elif fields := [f for f in found["fields"] if f["empty"] or f["kind"] == "password"]:
                keys = tuple(sorted(f["key"] for f in fields))
                if keys in sent:
                    return _again(page, done, _labels(fields))
                sent.add(keys)
                missing = [f for f in fields if login.value(f["key"]) is None]
                if missing:  # a field TARS has nothing for, on a later page of the sign-in: asked for, and kept
                    given = person.login(origin(page.url), goal, [_field(f) for f in missing])
                    if not given or any(not given.get(f["key"]) for f in missing):
                        return f"{origin(page.url)} asks for {_labels(missing)}, and TARS's login has none"
                    login = vault.add(person.name, login.name, given)
                field = None
                for f in fields:
                    _check(page, login, f)
                for f in fields:
                    value = login.value(f["key"]) or ""
                    if f["kind"] != "username" and value:
                        hide(value)
                    field = _locate(page, f)
                    field.fill(value)
                done.append(f"entered {_labels(fields)}")
                last = tuple(f["kind"] for f in fields)
                field = next((_locate(page, f) for f in fields if f["kind"] == "password"), field)
            else:
                if last != ("username",) or not boxes or "link" in sent:
                    break  # nothing left to fill: signed in, or the site wants something only the person can do
                sent.add("link")  # "check your email": the site sent a sign-in link, or a code for a page to come
                code, link = wait_for_mail(boxes, page.url, asked, MAIL_WAIT_S)
                if link:
                    page.goto(link, wait_until="domcontentloaded", timeout=20_000)
                    done.append("opened the sign-in link from their email")
                    continue
                return (f"{origin(page.url)} wants something after the username"
                        + (", and no sign-in email came" if not code else " that its email's code has nowhere to go")
                        + _said(page))  # fmt: skip
            asked = time.time()
            field.press("Enter")
        except Refused as e:
            return f"refused to type the login: {e}"
    _settle(page)
    steps = ", ".join(done) or "nothing to fill"
    return f"{steps}; now at {origin(page.url)}, titled {page.title()!r}{_said(page)}"


def _field(f: dict) -> Field:
    """A sign-in field of the page's, as the person is asked for it: by the site's own label."""
    label = f["label"] or {"username": "Email or phone", "password": "Password"}.get(f["kind"], f["key"])
    return Field(f["key"], label, secret=f["kind"] == "password")


def _labels(fields: list[dict]) -> str:
    named = {"username": "the username", "password": "the password"}
    return " and ".join(named.get(f["kind"], f"the {f['label'] or f['key']}") for f in fields)


def _again(page: Page, done: list[str], what: str) -> str:
    """Asked again for what was just given: wrong, refused, or a check only a person passes."""
    return (f"stopped after {', '.join(done)}: {origin(page.url)} asked for {what} again{_said(page)}. "
            "Not retrying: tell the person.")  # fmt: skip


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
