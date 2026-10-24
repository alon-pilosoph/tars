"""Errands in TARS's browser from a Telegram chat: the browse tool, approvals by button, sign-in codes sent back,
the one-time login page, and the sign-in codes and links read from the person's email."""

import dataclasses
import email
import threading
import time
import types
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from voice_assistant.config import BrowserConfig, LLMConfig
from voice_assistant.home import Request
from voice_assistant.llm import OpenAIChat

from .conftest import fake_openai_chat
from .test_telegram import ALON, FakeBot, ScriptedBrain, message, tars

pytest.importorskip("playwright")
pytest.importorskip("cryptography")

from voice_assistant.browser import login_page, mail
from voice_assistant.browser.errands import TELEGRAM, VOICE, Asking, Errands, Queue, profile_for
from voice_assistant.browser.login import Answer, sign_in
from voice_assistant.browser.vault import Login, Vault

from .test_browser_login import KSP, SignInPage, field, screen


class ButtonBot(FakeBot):
    """FakeBot, with buttons and photos."""

    def __init__(self):
        super().__init__()
        self.buttons, self.photos, self.dropped, self.taps_answered = [], [], [], []

    def send_buttons(self, chat_id, text, buttons):
        self.buttons.append((chat_id, text, buttons))
        return len(self.buttons)

    def drop_buttons(self, chat_id, message_id):
        self.dropped.append(message_id)

    def answer_tap(self, tap_id):
        self.taps_answered.append(tap_id)

    def send_photo(self, chat_id, data, caption=""):
        self.photos.append((chat_id, data))


def tap(data, from_id=ALON):
    return {"update_id": 9, "callback_query": {"id": "t1", "data": data, "from": {"id": from_id}}}


def until(check, wait_s=3.0):
    deadline = time.monotonic() + wait_s
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


# The browse tool


def test_the_brain_has_the_browse_tool_and_it_starts_an_errand_for_whoever_asked():
    started = []

    class FakeErrands:
        tool = types.MappingProxyType({"type": "function", "name": "browse", "parameters": {}})
        rules, can, needs = "BROWSE RULES", "do errands", "an errand"

        def call(self, args, request):
            started.append((args["errand"], request.speaker))
            return "started"

    client = fake_openai_chat(
        lambda m: "On it." if m[-1].get("type") == "function_call_output" else "",
        lambda m: [("browse", {"errand": "On Wolt, order a margherita"})] if m[-1].get("role") == "user" else [],
    )
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False), browser=FakeErrands())
    reply = "".join(brain.stream_reply("[Speaker: Alon] get me a pizza", Request(speaker="Alon")))
    assert reply == "On it." and started == [("On Wolt, order a margherita", "Alon")]
    assert "BROWSE RULES" in client.requests[0]["instructions"]
    assert any(t.get("name") == "browse" for t in client.requests[0]["tools"])


class Chats:
    """The person's chat, as an errand reaches it: what it was told."""

    def __init__(self):
        self.told = []

    def tell(self, person, text, picture=None):
        self.told.append((person, text))


def errands(tmp_path, run, people=("Alon", "Stacey"), store=None, say_aloud=None):
    chats = Chats()
    e = Errands(BrowserConfig(enabled=True), chats, list(people), queue=Queue(store), say_aloud=say_aloud, run=run,
                folder=tmp_path)  # fmt: skip
    return e, chats.told


def done(answer, aloud="", **more):
    return lambda goal, approver, **kw: {"ended": "done", "answer": answer, "aloud": aloud, "commits_asked": [], **more}


def test_an_errand_asked_in_a_chat_runs_on_its_own_and_its_answer_goes_to_the_chat(tmp_path):
    gate, ran = threading.Event(), []

    def run(goal, approver, **kw):
        ran.append((goal, approver.name, kw["profile"].name, kw["settings"].model))
        gate.wait(2)
        return {"ended": "done", "answer": "Your margherita is in the basket.", "commits_asked": []}

    e, told = errands(tmp_path, run)
    asking = e.asking(TELEGRAM)
    assert asking.call({"errand": "order a margherita on Wolt"}, Request(speaker="alon")).startswith("started")
    assert e.next() and not e.next()  # one at a time
    queued = asking.call({"errand": "book a table on Ontopo"}, Request(speaker="Stacey"))
    assert queued.startswith("queued") and "Alon" in queued
    gate.set()
    e.thread.join(2)
    assert told == [("Alon", "Your margherita is in the basket.")]
    assert ran == [("order a margherita on Wolt", "Alon", "profile", "gpt-6-luna")]
    assert e.next()  # Stacey's, next
    e.thread.join(2)
    assert ran[1][:3] == ("book a table on Ontopo", "Stacey", "profile-stacey")


def test_an_errand_needs_someone_known_asking_a_goal_and_a_turn_that_goes_ahead(tmp_path):
    e, _ = errands(tmp_path, run=done("x"))
    asking = e.asking(VOICE)
    assert asking.call({"errand": "order a pizza on Wolt"}, Request(by_voice=True)).startswith("error")
    assert asking.call({"errand": "order a pizza on Wolt"}, Request(speaker="Mallory")).startswith("error")
    assert asking.call({"errand": "pizza"}, Request(speaker="Alon")).startswith("error")
    withdrawn = Request(speaker="Alon", by_voice=True, may_act=lambda timeout: False)
    assert asking.call({"errand": "order a pizza on Wolt"}, withdrawn).startswith("error")
    assert e.queue.ahead() == []


def test_an_errand_asked_aloud_is_queued_for_the_telegram_service_and_its_answer_said_at_home(tmp_path):
    from voice_assistant.reminders import Reminders, line
    from voice_assistant.store import Store

    store = Store(tmp_path / "db")
    voice = Asking(Queue(store), ["Alon"], VOICE)  # in the voice's process
    said = voice.call({"errand": "On Wolt, order a margherita"}, Request(speaker="Alon", by_voice=True))
    assert said.startswith("started") and "phone" in said and "phone" in voice.rules
    reminders = Reminders(store)
    from voice_assistant.__main__ import errand_said_aloud

    e, told = errands(tmp_path, done("The checkout is ready: ₪62, approve it on your phone.", "It's ready: ₪62."),
                      store=store, say_aloud=errand_said_aloud(reminders))  # fmt: skip
    assert e.next()  # in the Telegram service's
    e.thread.join(2)
    assert told == [("Alon", "On it, as you asked at home: On Wolt, order a margherita"),
                    ("Alon", "The checkout is ready: ₪62, approve it on your phone.")]  # fmt: skip
    due = reminders.due(time.time() + 1)
    assert [line(r) for r in due] == ["Alon, a message: It's ready: ₪62."] and due[0]["max_tries"] == 1
    assert store.row("SELECT status, answer FROM errands")["status"] == "done"


def test_the_answer_isnt_said_at_home_to_someone_out(tmp_path):
    from voice_assistant.__main__ import errand_said_aloud
    from voice_assistant.reminders import Reminders
    from voice_assistant.store import Store

    store = Store(tmp_path / "db")
    reminders = Reminders(store)
    out = types.SimpleNamespace(board=types.SimpleNamespace(is_out=lambda person: True))
    e, told = errands(tmp_path, done("Booked."), store=store, say_aloud=errand_said_aloud(reminders, out))
    Asking(e.queue, ["Alon"], VOICE).call({"errand": "book a table on Ontopo"}, Request(speaker="Alon"))
    e.next()
    e.thread.join(2)
    assert told[-1] == ("Alon", "Booked.") and reminders.due(time.time() + 1) == []


def test_an_errand_that_breaks_is_said_so_and_one_left_running_is_failed(tmp_path):
    from voice_assistant.store import Store

    def run(*args, **kwargs):
        raise RuntimeError("Chrome went away")

    store = Store(tmp_path / "db")
    e, told = errands(tmp_path, run, store=store)
    e.asking().call({"errand": "order a margherita on Wolt"}, Request(speaker="Alon"))
    e.next()
    e.thread.join(2)
    assert told[0][0] == "Alon" and "broke" in told[0][1] and "Nothing was ordered" in told[0][1]
    e.queue.add("Alon", "a stranded errand", TELEGRAM)
    e.queue.claim()  # running when the service stopped
    assert e.queue.left_running() == 1 and e.queue.ahead() == []


def test_each_person_has_a_browser_profile_of_their_own():
    first = profile_for("Alon", "Alon")
    assert profile_for("alon", "Alon") == first
    assert profile_for("Stacey Ann", "Alon") == first.with_name("profile-stacey-ann")


# The chat: approving, a code, the answer


def test_an_order_is_approved_only_by_a_tap_on_order_in_the_persons_own_chat():
    bot = ButtonBot()
    t = tars(bot, [ScriptedBrain()])
    answers = []
    asking = threading.Thread(target=lambda: answers.append(t.approve("Alon", "Order a margherita, ₪62?", b"jpg")))
    asking.start()
    until(lambda: bot.buttons)
    chat_id, _, buttons = bot.buttons[0]
    nonce = buttons[0][1].split(":")[1]
    t.handle(tap(f"order:{nonce}", from_id=999))  # someone else's tap
    t.handle(tap("order:0000"))  # an old message's
    assert asking.is_alive()
    t.handle(tap(f"order:{nonce}"))
    asking.join(2)
    assert answers == [True] and chat_id == ALON and bot.photos == [(ALON, b"jpg")]
    assert bot.dropped == [1] and "Ordering." in bot.said()


def test_cancel_says_nothing_was_ordered():
    bot = ButtonBot()
    t = tars(bot, [ScriptedBrain()])
    answers = []
    asking = threading.Thread(target=lambda: answers.append(t.approve("Alon", "Book ESO for 2?", None)))
    asking.start()
    until(lambda: bot.buttons)
    t.handle(tap(bot.buttons[0][2][1][1]))
    asking.join(2)
    assert answers == [False] and "Cancelled: nothing ordered." in bot.said()


def test_a_code_sent_back_goes_to_the_sign_in_and_not_to_the_brain():
    bot, brain = ButtonBot(), ScriptedBrain(["Hi!"])
    t = tars(bot, [brain])
    answer, got = Answer(), []
    asking = threading.Thread(target=lambda: got.append(t.code("Alon", KSP, answer)))
    asking.start()
    until(lambda: any("sign-in code" in m for m in bot.said()))
    t.handle(message("123 456"))
    asking.join(2)
    assert got == ["123456"] and brain.asked == []
    t.handle(message("hello"))  # no code waiting: the brain answers
    assert brain.asked == ["[Speaker: Alon] hello"]


def test_a_code_found_in_the_email_first_is_said_so():
    bot = ButtonBot()
    t = tars(bot, [ScriptedBrain()])
    answer = Answer(mail=True)
    threading.Timer(0.05, lambda: answer.give("777111", "email")).start()
    assert t.code("Alon", KSP, answer) == "777111"
    assert "email too" in bot.said()[0] and "Found it in your email" in bot.said()[1]


def test_an_errands_answer_is_posted_and_remembered():
    bot, brain = ButtonBot(), ScriptedBrain()
    t = tars(bot, [brain])
    t.tell("Alon", "Your order is in the basket.")
    assert bot.said() == ["Your order is in the basket."] and brain.remembered == ["Your order is in the basket."]


# The one-time login page


def test_the_login_page_takes_one_login_with_its_token_only(unused_port):
    links, got = [], []
    asking_for = login_page.Asking(KSP, "to order a cable", FIELDS)
    asking = threading.Thread(
        target=lambda: got.append(login_page.ask(asking_for, links.append, "127.0.0.1", unused_port, wait_s=3))
    )
    asking.start()
    until(lambda: links)
    link = links[0]
    assert link.startswith(f"http://127.0.0.1:{unused_port}/login?token=")
    token = link.split("token=")[1]
    page = httpx.get(link)
    assert page.status_code == 200 and "www.ksp.co.il" in page.text and page.headers["cache-control"] == "no-store"
    assert "מספר תעודת זהות" in page.text and 'name="teudat_zehut"' in page.text and 'type="password"' in page.text
    assert httpx.get(link.replace(token, "wrong")).status_code == 404
    form = {"token": token, "username": " alon@example.com ", "teudat_zehut": "012345678", "password": "hunter2"}
    missing = {**form, "teudat_zehut": ""}
    assert httpx.post(f"http://127.0.0.1:{unused_port}/login", data=missing).status_code == 400
    assert httpx.post(f"http://127.0.0.1:{unused_port}/login", data={**form, "token": "x"}).status_code == 404
    assert httpx.post(f"http://127.0.0.1:{unused_port}/login", data=form).status_code == 200
    asking.join(3)
    assert got == [{"username": "alon@example.com", "teudat_zehut": "012345678", "password": "hunter2"}]


def test_a_site_shown_on_the_page_is_escaped():
    html_page = login_page.page(
        login_page.Asking("https://x.example/<script>", "why <b>", (login_page.Field('a"b', "<i>"),)), "tok"
    )
    assert "<script>" not in html_page and "&lt;script&gt;" in html_page and "&lt;b&gt;" in html_page
    assert "<i>" not in html_page and 'name="a&quot;b"' in html_page


FIELDS = (
    login_page.Field("username", "Email or phone"),
    login_page.Field("teudat_zehut", "מספר תעודת זהות"),
    login_page.Field("password", "Password", secret=True),
)


@pytest.fixture
def unused_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Mail


def sign_in_email(sender, text, sent=None, html=None):
    m = EmailMessage()
    m["From"], m["Subject"] = sender, "Your sign-in"
    m["Date"] = format_datetime(datetime.fromtimestamp(sent or time.time(), UTC))
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
    return m.as_bytes()


class FakeImap:
    """An IMAP server with one mailbox's emails, as (raw bytes): searched by sender's domain."""

    def __init__(self, mails):
        self.mails, self.selected, self.logged_in = mails, None, None

    def login(self, user, password):
        self.logged_in = (user, password)

    def select(self, box, readonly=False):
        assert readonly
        self.selected = box

    def noop(self):
        return "OK", []

    def uid(self, command, *args):
        if command == "SEARCH":
            domain = args[1].strip('"').split()[0].removeprefix("from:")
            found = [str(i).encode() for i, raw in enumerate(self.mails, 1) if domain.encode() in raw.split(b"\n")[0]]
            return "OK", [b" ".join(found)]
        raw = self.mails[int(args[0]) - 1]
        return "OK", [(b"1 (UID " + args[0] + b" BODY[]", raw)]

    def logout(self):
        pass


GMAIL_LOGIN = Login(
    "imap.gmail.com alon@gmail.com",
    ("imaps://imap.gmail.com",),
    (("username", "alon@gmail.com"), ("password", "abcd efgh")),
)


def test_a_sites_own_domain_is_its_last_labels_with_a_country_suffix():
    assert mail.site_domain("https://www.ksp.co.il/web/") == "ksp.co.il"
    assert mail.site_domain("https://wolt.com/he/isr") == "wolt.com"
    assert mail.site_domain("https://account.booking.com/sign-in") == "booking.com"
    assert mail.on_domain("https://auth.wolt.com/magic?t=1", "wolt.com")
    assert not mail.on_domain("https://wolt.com.evil.example/magic", "wolt.com")
    assert not mail.on_domain("http://wolt.com/magic", "wolt.com")


def test_the_code_or_link_comes_from_the_sites_own_newest_mail():
    old = sign_in_email("Wolt <no-reply@wolt.com>", "Your code is 111111", sent=time.time() - 3600)
    other = sign_in_email("Evil <x@evil.example>", "Your code is 999999. https://evil.example/login")
    new = sign_in_email(
        "Wolt <no-reply@wolt.com>", "Sign in", html='<a href="https://wolt.com/magic-login?t=abc&amp;x=1">Sign in</a>'
    )
    imap = FakeImap([old, other, new])
    code, link = mail.wait_for_sign_in([GMAIL_LOGIN], "https://wolt.com/he", time.time() - 30, 1,
                                       connect=lambda h, p: imap, sleep=lambda s: None)  # fmt: skip
    assert (code, link) == (None, "https://wolt.com/magic-login?t=abc&x=1")
    assert imap.logged_in == ("alon@gmail.com", "abcdefgh") and imap.selected == '"[Gmail]/All Mail"'


def test_a_code_in_a_hebrew_email_is_found():
    raw = sign_in_email("KSP <info@ksp.co.il>", "קוד האימות שלך: 482913")
    code, _ = mail.wait_for_sign_in([GMAIL_LOGIN], KSP, time.time() - 30, 1,
                                    connect=lambda h, p: FakeImap([raw]), sleep=lambda s: None)  # fmt: skip
    assert code == "482913"


def test_no_mail_in_time_is_none():
    clock = iter(range(100)).__next__
    found = mail.wait_for_sign_in([GMAIL_LOGIN], KSP, time.time(), 5, connect=lambda h, p: FakeImap([]),
                                  sleep=lambda s: None, clock=clock)  # fmt: skip
    assert found == (None, None)


# Signing in by email


def test_a_site_that_signs_in_by_email_gets_the_mailboxs_address_and_the_link_from_it(tmp_path):
    vault = Vault(tmp_path)
    vault.store_mailbox("Alon", "alon@gmail.com", "abcd efgh")
    page = SignInPage(screen(url="https://wolt.com/he/login", username=field("email")),
                      screen(url="https://wolt.com/he/check-your-email"))  # fmt: skip
    opened = []
    page.goto = lambda link, **kw: opened.append(link)

    def mailed(boxes, url, since, wait_s, stop=None):
        assert [b.username for b in boxes] == ["alon@gmail.com"]
        return None, "https://wolt.com/magic-login?t=abc"

    asked = types.SimpleNamespace(name="Alon", login=lambda *a: pytest.fail("no login is asked for"))
    said = sign_in(page, vault, asked, wait_for_mail=mailed)
    assert page.typed == [("#email", "alon@gmail.com")] and opened == ["https://wolt.com/magic-login?t=abc"]
    assert "opened the sign-in link" in said
    assert vault.for_page("https://wolt.com/x", "Alon").username == "alon@gmail.com"


def test_a_code_from_the_email_is_typed_when_it_comes_first(tmp_path):
    vault = Vault(tmp_path)
    vault.store("Alon", [KSP], {"username": "alon@example.com"})
    vault.store_mailbox("Alon", "alon@gmail.com", "abcd efgh")
    page = SignInPage(screen(username=field("email")), screen(code=field("otp")), screen(url=KSP + "/account"))

    def mailed(boxes, url, since, wait_s, stop=None):
        return "482913", None

    person = types.SimpleNamespace(name="Alon", code=lambda site, answer: answer.wait(2))
    sign_in(page, vault, person, wait_for_mail=mailed)
    assert page.typed == [("#email", "alon@example.com"), ("#otp", "482913")]


def test_a_mailbox_is_never_a_login_for_a_page(tmp_path):
    vault = Vault(tmp_path)
    vault.store_mailbox("Alon", "alon@gmail.com", "abcd efgh")
    assert vault.for_page("https://imap.gmail.com/", "Alon") is None
    with pytest.raises(ValueError):
        vault.store_mailbox("Alon", "alon@gmail.com", "")
    assert email  # the module the fakes build on


def test_the_telegram_service_makes_errands_only_with_browser_on():
    from voice_assistant.__main__ import make_errands
    from voice_assistant.config import ConfigError, load_config

    cfg = load_config(Path("config.toml"), local=False)
    assert make_errands(cfg, has_openai=True) is None
    on = dataclasses.replace(cfg, browser=BrowserConfig(enabled=True))
    with pytest.raises(ConfigError):
        make_errands(on, has_openai=False)
    made = make_errands(on, has_openai=True)
    assert isinstance(made, Errands) and made.chats is None and made.queue.store is None
