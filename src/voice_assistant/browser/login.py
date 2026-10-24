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

import threading
import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..homenet import at_home
from .gate import says_commit
from .login_page import Field
from .session import PAYING_JS, SUBMITS_JS
from .vault import Login, Vault, origin

STEPS = 6  # a sign-in is a few pages at most: the name, the password, a code, a link
MAIL_WAIT_S = 180  # how long a sign-in email may take to come

# What the page is asking for: the fields of its sign-in form a person could see and reach, each with its label as
# the site shows it. In the page's own document only (not frames); not a field kept from people (aria-hidden, out of
# the tab order: Booking.com's decoy for password managers) or a search box; and only those in the same form (or
# dialog) as the password, else the username, so a newsletter's email box elsewhere on the page isn't one of them.
# What a field is goes first by the standard's autocomplete tokens (one-time-code, username, email, tel,
# current-password, new-password, given-name...), as browsers and password managers read them, then by its type, and
# only then by the words of its label; a code in boxes of one character each is one code. A form with a new password
# or a name to fill in is a sign-up, which TARS doesn't do.
FIELDS_JS = r"""() => {
  const seen = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled &&
      !e.closest('[aria-hidden=true]') && e.tabIndex >= 0; };
  const all = [...document.querySelectorAll('input')].filter(seen);
  const tokens = (e) => String(e.getAttribute('autocomplete') || '').toLowerCase().split(' ').filter(Boolean);
  const has = (e, ...wanted) => tokens(e).some((t) => wanted.includes(t));
  const labelOf = (e) => String((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') ||
    e.placeholder || e.name || e.id || '').split(/\s+/).join(' ').trim().slice(0, 80);
  const words = (e) => [labelOf(e), e.name, e.id, e.placeholder, e.getAttribute('aria-label')].join(' ').toLowerCase();
  const text = (e) => ['text', 'email', 'tel', 'number', ''].includes(e.type);
  const NOT_CODE = /promo|coupon|voucher|gift|zip|postal|country|captcha|discount|קופון|שובר|מיקוד|הנחה/;
  const CODE = /one.?time|otp|verification|verify|sign.?in code|login code|security code|2fa|mfa|totp|\bcode\b|קוד/;
  const USER = /e.?mail|user|login|phone|mobile|account|identifier|טלפון|נייד|דוא["״]?ל|מייל|משתמש|זהות|ת\.?ז/;
  const NAME = /first.?name|last.?name|full.?name|given.?name|family.?name|surname|fname|lname|שם פרטי|שם משפחה|שם מלא/;
  const isCode = (e) => has(e, 'one-time-code') ||
    (text(e) && tokens(e).length === 0 && !NOT_CODE.test(words(e)) && CODE.test(words(e)));
  const isUser = (e) => has(e, 'username', 'email', 'tel', 'webauthn') || e.type === 'email' || e.type === 'tel' ||
    (text(e) && USER.test(words(e)));
  const isName = (e) => has(e, 'name', 'given-name', 'family-name', 'additional-name') || NAME.test(words(e));
  const isSearch = (e) => e.type === 'search' || !!e.closest('[role=search]') ||
    /search|query|חיפוש|(^|[^a-z])q([^a-z]|$)/.test(words(e));
  const own = (e) => tokens(e).find((t) => !['on', 'off'].includes(t)) || e.name || e.id || labelOf(e);
  document.querySelectorAll('[data-tars-field]').forEach((e) => e.removeAttribute('data-tars-field'));
  let tags = 0;
  const tag = (e) => { e.setAttribute('data-tars-field', String(++tags)); return String(tags); };
  const describe = (e, kind) => ({tag: tag(e), kind, label: labelOf(e), empty: !e.value,
    email: e.type === 'email' || has(e, 'email'), key: kind === 'other' ? own(e).toLowerCase() : kind,
    action: e.form ? e.form.getAttribute('action') : null});
  const codes = all.filter(isCode);
  let inputs = all.filter((e) => !codes.includes(e) && (e.type === 'password' || text(e)) && !isSearch(e));
  const anchor = inputs.find((e) => e.type === 'password') || inputs.find(isUser) || codes[0];
  const scope = anchor && (anchor.form || anchor.closest('dialog, [role=dialog], [aria-modal=true]'));
  const within = (e) => !scope || scope.contains(e);
  inputs = inputs.filter(within);
  let user = false;
  const fields = inputs.map((e) => {
    const kind = e.type === 'password' ? 'password' : (!user && isUser(e)) ? 'username' : 'other';
    user = user || kind === 'username';
    return describe(e, kind);
  });
  let code = null;
  if (codes.length) {
    code = describe(codes[0], 'code');
    const boxes = codes.length > 1 && codes.every((e) => e.maxLength === 1);
    code.boxes = boxes ? [code.tag, ...codes.slice(1).map(tag)] : [];
  }
  const scoped = all.filter(within);
  return {code, fields, signup: scoped.some((e) => has(e, 'new-password')) || scoped.some(isName) ||
    scoped.filter((e) => e.type === 'password').length > 1};
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


class WrongSite(Exception):
    """The page isn't the login's site, or its form would send elsewhere, or press something that commits."""


def _locate(page: Page, field: dict):
    return page.locator(f'[data-tars-field="{field["tag"]}"]').first


def _check(page: Page, login: Login, field: dict) -> None:
    """Raises WrongSite unless typing into `field` on `page` sends it to `login`'s site and nowhere else."""
    here = origin(page.url)
    if here not in login.origins or not here.startswith("https://"):
        raise WrongSite(f"the page is at {here}, not {', '.join(login.origins)}")
    action = field.get("action")
    if action and not action.startswith("javascript:"):
        sends_to = origin(urljoin(page.url, action))
        if sends_to not in login.origins:
            raise WrongSite(f"its form sends to {sends_to}, not {', '.join(login.origins)}")


def _submit(page: Page, field) -> None:
    """Enter in a sign-in field, unless its form is one that commits: a guest checkout's email field is in the
    checkout's form. A sign-in form's own button ("Confirm", "אישור", "Submit") is fine; one that pays, orders or
    books isn't, and nor is any on a page that takes payment."""
    try:
        sends = field.evaluate(SUBMITS_JS) or {}
    except PlaywrightError:
        sends = {}
    paying = any(_asks(frame, PAYING_JS) for frame in page.frames)
    if paying or any(says_commit(label) for label in sends.get("labels", [])):
        raise WrongSite("Enter there would submit a form that pays, orders or books: that's not a sign-in form")
    field.press("Enter")


def _asks(frame, script: str) -> bool:
    try:
        return bool(frame.evaluate(script))
    except PlaywrightError:
        return False


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
    wait_for_mail: Callable[..., tuple[str | None, str | None]] | None = None,
) -> str:
    """Signs `person` in on the page as it is; how it went, for the model. `hide` is told each secret before it's
    typed (a password, a code), for whatever keeps what the page sends to leave it out. `wait_for_mail`:
    mail.wait_for_sign_in with a reader, to take codes and links from the person's email; None, the person alone."""
    try:
        return SignIn(page, vault, person, goal, hide, wait_for_mail).run()
    except PlaywrightError as e:  # the page went away under it, or a field it found isn't there now
        return f"the sign-in stopped: {str(e).splitlines()[0][:160]}; look at the page as it is now"


class Stop(Exception):
    """A sign-in that can't go on, and what to tell the model."""


class SignIn:
    """One sign-in, a page at a time: what each page asks (a code, the login's fields, or nothing, after a username:
    a link by email) is given once; asked for again, it stops."""

    def __init__(self, page: Page, vault: Vault, person: Person, goal: str, hide, wait_for_mail):
        self.page, self.vault, self.person, self.goal = page, vault, person, goal
        self.hide, self.wait_for_mail = hide, wait_for_mail
        self.boxes = vault.mailboxes(person.name) if wait_for_mail else []
        self.done: list[str] = []
        self.sent: set = set()
        self.asked, self.last = time.time(), ()
        self.login: Login | None = None
        self.keep: dict[str, str] | None = None  # a login to keep once it has worked (a mailbox's address)

    def run(self) -> str:
        try:
            self._login()
            for _ in range(STEPS):
                _settle(self.page)
                if origin(self.page.url) not in self.login.origins:
                    break  # off the site (signed in and sent on, or somewhere else): nothing more to type here
                found = self.page.evaluate(FIELDS_JS)
                if found["signup"]:
                    raise Stop(f"{origin(self.page.url)} is asking to sign up, which TARS doesn't do: call done")
                fields = [f for f in found["fields"] if f["empty"] or f["kind"] == "password"]
                if found["code"]:
                    submit = self._code(found["code"])
                elif fields:
                    submit = self._fields(fields)
                elif self._mailed_link():
                    continue
                else:
                    break  # nothing left to fill: signed in, or the site wants something only the person can do
                self.asked = time.time()
                _submit(self.page, submit)
        except (Stop, WrongSite) as e:
            return f"refused to type the login: {e}" if isinstance(e, WrongSite) else str(e)
        if self.keep is not None:
            self.vault.store(self.person.name, list(self.login.origins), self.keep)
        _settle(self.page)
        steps = ", ".join(self.done) or "nothing to fill"
        return f"{steps}; now at {origin(self.page.url)}, titled {self.page.title()!r}{_said(self.page)}"

    def _login(self) -> None:
        """The login for the site the browser is at: the vault's, or what the person gives for its fields now."""
        here = origin(self.page.url)
        self.login = self.vault.for_page(self.page.url, self.person.name)
        if self.login is not None:
            return
        found = self.page.evaluate(FIELDS_JS)
        fields = found["fields"]
        if not here.startswith("https://") or not (fields or found["code"]):
            raise Stop(f"no sign-in form here ({here}) and no login for it: go to the site's sign-in page first")
        if self.boxes and fields and all(f["kind"] == "username" and f["email"] for f in fields):
            values = {"username": self.boxes[0].username}  # a site that signs in by email alone: kept once it works
            self.keep = values
        else:
            wanted = [_field(f) for f in fields]
            if fields and "password" not in {f["kind"] for f in fields}:  # it may ask next: one link, not two
                wanted.append(Field("password", "Password, if it asks for one next", secret=True, required=False))
            values = self.person.login(here, self.goal, wanted) if wanted else None
            if not values:
                raise Stop(f"TARS has no login for {here} and none was given: call done and say it needs one")
        if origin(self.page.url) != here:
            raise Stop(f"stopped: the page moved on from {here} while the login was being asked for")
        if self.keep is not None:
            self.login = Login(here.removeprefix("https://"), (here,), tuple(values.items()))
            return
        try:
            self.login = self.vault.store(self.person.name, [here], values)
        except ValueError as e:
            raise Stop(f"the login given for {here} wasn't kept: {e}") from None

    def _code(self, code_field: dict):
        """The code the site sent, typed into its box (or boxes); the field to submit."""
        if "code" in self.sent:
            raise Stop(_again(self.page, self.done, "the code"))
        self.sent.add("code")
        _check(self.page, self.login, code_field)
        code = _code(self.page, self.person, self.boxes, self.asked, self.wait_for_mail)
        if not code:
            raise Stop(f"{origin(self.page.url)} wants the code it sent the person, and none came")
        again = self.page.evaluate(FIELDS_JS)["code"]  # minutes may have passed: the page as it is now
        if again is None or origin(self.page.url) not in self.login.origins:
            raise Stop(f"the page moved on while the code was coming (now at {origin(self.page.url)}): not typed")
        self.hide(code)
        boxes = again.get("boxes") or []
        if len(boxes) == len(code):  # one box a character
            for tag, char in zip(boxes, code, strict=True):
                _fill(self.page, self.login, {**again, "tag": tag}, char)
        else:
            _fill(self.page, self.login, again, code)
        self.done.append("entered the code they were sent")
        self.last = ("code",)
        return _locate(self.page, {**again, "tag": boxes[-1] if len(boxes) == len(code) else again["tag"]})

    def _fields(self, fields: list[dict]):
        """The login's values typed into the page's fields, asking the person for any it lacks; the field to
        submit."""
        keys = tuple(sorted(f["key"] for f in fields))
        if keys in self.sent:
            raise Stop(_again(self.page, self.done, _labels(fields)))
        self.sent.add(keys)
        missing = [f for f in fields if self.login.value(f["key"]) is None]
        if missing:  # a field TARS has nothing for, on a later page of the sign-in: asked for, and kept
            given = self.person.login(origin(self.page.url), self.goal, [_field(f) for f in missing])
            if not given or any(not given.get(f["key"]) for f in missing):
                raise Stop(f"{origin(self.page.url)} asks for {_labels(missing)}, and TARS's login has none")
            if self.keep is not None:  # not kept yet: kept with these once it has worked
                self.keep |= given
                self.login = Login(self.login.name, self.login.origins, tuple(self.keep.items()))
            else:
                self.login = self.vault.add(self.person.name, self.login.name, given)
        for f in fields:
            value = self.login.value(f["key"]) or ""
            if f["kind"] != "username" and value:
                self.hide(value)
            _fill(self.page, self.login, f, value)
        self.done.append(f"entered {_labels(fields)}")
        self.last = tuple(f["kind"] for f in fields)
        return _locate(self.page, next((f for f in fields if f["kind"] == "password"), fields[-1]))

    def _mailed_link(self) -> bool:
        """After a username and nothing else to fill ("check your email"): the sign-in link the site emailed,
        opened; whether one was. A code it emailed for a page still to come has nowhere to go yet."""
        if self.last != ("username",) or not self.boxes or "link" in self.sent:
            return False
        self.sent.add("link")
        code, link = self.wait_for_mail(self.boxes, self.page.url, self.asked, MAIL_WAIT_S)
        if link and not at_home(link):
            self.page.goto(link, wait_until="domcontentloaded", timeout=20_000)
            self.done.append("opened the sign-in link from their email")
            return True
        raise Stop(f"{origin(self.page.url)} wants something after the username"
                   + (", and no sign-in email came" if not code else " that its email's code has nowhere to go")
                   + _said(self.page))  # fmt: skip


def _fill(page: Page, login: Login, field: dict, value: str) -> None:
    """`value` typed into `field`, with the page checked to be the login's site right before, on the very element
    found: a handle stops working if the page navigates, rather than finding the same tag on another page."""
    handle = _locate(page, field).element_handle(timeout=3_000)
    _check(page, login, field)
    handle.fill(value)


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
    if boxes and wait_for_mail:
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
