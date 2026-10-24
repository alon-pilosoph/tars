"""The person's mail, read for a sign-in: the code or the link a site emails them when TARS's browser asks it to sign
them in (login.py). Read-only over IMAP (nothing is sent, deleted or marked read), with the mailbox's login from the
vault: for Gmail an app password (myaccount.google.com/apppasswords), which they can revoke there any time.

Only mail from the site's own domain that arrived after TARS asked counts, and a link is only used if it's on that
domain too. What an email says goes to no model: code takes the code or the link out of it."""

import email
import imaplib
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.policy import default
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urlsplit

from .vault import Login

PORT = 993
READ_BYTES = 400_000  # the start of an email: the text comes before the attachments
LOOK_EVERY_S = 4.0
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
NEWEST = 5  # the latest few from the site are enough: a sign-in email is the newest from it
# Second-level labels that are part of a country's suffix (ksp.co.il, bbc.co.uk): the site's domain is one more.
SUFFIXES = {"co", "com", "org", "net", "ac", "gov", "edu", "muni", "ltd", "plc", "ne", "or"}
CODE = re.compile(r"(?i)(?:code|קוד|verification|אימות)\D{0,40}?\b(\d{4,8})\b|\b(\d{4,8})\b\D{0,40}?(?:code|קוד)")
SIGN_IN_WORDS = re.compile(r"log.?in|sign.?in|magic|verify|auth|confirm|token|session", re.IGNORECASE)


def site_domain(url: str) -> str:
    """The domain a site's email comes from: wolt.com for https://wolt.com/he, ksp.co.il for https://www.ksp.co.il."""
    labels = (urlsplit(url).hostname or url).lower().split(".")
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SUFFIXES else 2
    return ".".join(labels[-keep:])


def on_domain(link: str, domain: str) -> bool:
    host = (urlsplit(link).hostname or "").lower()
    return link.startswith("https://") and (host == domain or host.endswith("." + domain))


def code_in(text: str) -> str | None:
    found = CODE.search(text)
    return (found.group(1) or found.group(2)) if found else None


def sign_in_link(links: list[str], domain: str) -> str | None:
    return next((link for link in links if on_domain(link, domain) and SIGN_IN_WORDS.search(link)), None)


def text_and_links(message: Message) -> tuple[str, list[str]]:
    """An email's text (its HTML as text too) and every https link in it."""
    texts, links = [], []
    for part in message.walk():
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        content = part.get_content()
        links += [unescape(link) for link in re.findall(r"""https://[^\s"'<>)]+""", content)]
        texts.append(_text_of(content) if part.get_content_type() == "text/html" else content)
    return "\n".join(texts), links


def _text_of(html_text: str) -> str:
    html_text = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", html_text)
    html_text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", html_text)
    return unescape(re.sub(r"<[^>]+>", " ", html_text)).replace("\xa0", " ")


class Mailbox:
    """One mailbox, read-only: Gmail's All Mail (searched with Gmail's own syntax), or another server's inbox."""

    def __init__(self, login: Login, connect: Callable[[str, int], imaplib.IMAP4] | None = None):
        host = urlsplit(login.origins[0]).hostname or ""
        self.address = login.username
        self.gmail = host == "imap.gmail.com"
        self.imap = (connect or (lambda h, p: imaplib.IMAP4_SSL(h, p, timeout=30)))(host, PORT)
        self.imap.login(login.username, login.password.replace(" ", ""))  # Gmail shows app passwords in fours
        self.imap.select('"[Gmail]/All Mail"' if self.gmail else "INBOX", readonly=True)  # never marks anything read

    def from_site(self, domain: str, since: float) -> list[Message]:
        """The newest emails from `domain` that came after `since` (epoch seconds), newest first."""
        self.imap.noop()  # what came in since the last look
        if self.gmail:
            query = f"from:{domain} newer_than:1d"
            status, data = self.imap.uid("SEARCH", "X-GM-RAW", '"' + query.replace('"', "") + '"')
        else:
            day = datetime.fromtimestamp(since, UTC) - timedelta(days=1)
            day = f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"  # IMAP's English months, whatever the locale
            status, data = self.imap.uid("SEARCH", "FROM", f'"{domain}"', "SINCE", day)
        if status != "OK" or not data or not data[0]:
            return []
        found = []
        for uid in data[0].split()[-NEWEST:][::-1]:
            _, parts = self.imap.uid("FETCH", uid, f"(BODY.PEEK[]<0.{READ_BYTES}>)")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if raw is None:
                continue
            message = email.message_from_bytes(raw, policy=default)
            try:
                sent = parsedate_to_datetime(str(message["date"])).timestamp()
            except TypeError, ValueError:
                continue
            if sent >= since - 60:  # a minute's grace for the two clocks
                found.append(message)
        return found

    def close(self) -> None:
        try:
            self.imap.logout()
        except OSError, imaplib.IMAP4.error:
            return


def wait_for_sign_in(
    logins: list[Login],
    url: str,
    since: float,
    wait_s: float,
    stop: threading.Event | None = None,
    connect: Callable[[str, int], imaplib.IMAP4] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str | None, str | None]:
    """(code, link) from the first sign-in email that `url`'s site sends to any of these mailboxes after `since`,
    waiting up to `wait_s` for one (or until `stop` is set); (None, None) if none came. A mailbox that can't be
    opened is left out."""
    domain = site_domain(url)
    boxes = []
    for login in logins:
        try:
            boxes.append(Mailbox(login, connect))
        except (OSError, imaplib.IMAP4.error) as e:
            print(f"(mail: couldn't open {login.username}: {type(e).__name__})")
    try:
        deadline = clock() + wait_s
        while boxes and clock() < deadline and not (stop and stop.is_set()):
            for box in boxes:
                try:
                    messages = box.from_site(domain, since)
                except (OSError, imaplib.IMAP4.error) as e:
                    print(f"(mail: couldn't read {box.address}: {type(e).__name__})")
                    continue
                for message in messages:
                    text, links = text_and_links(message)
                    code, link = code_in(text), sign_in_link(links, domain)
                    if code or link:
                        return code, link
            sleep(LOOK_EVERY_S)
        return None, None
    finally:
        for box in boxes:
            box.close()
