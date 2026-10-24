"""TARS's browser on real pages, in a real Chrome (or Debian's Chromium, as on the Pi): the gate, what the model is
shown, and signing in, where a fake page would only test the fake. Skipped where there's no browser."""

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright

from voice_assistant.browser.chrome import executable
from voice_assistant.browser.session import Session

try:
    CHROME = executable()
except RuntimeError:
    CHROME = None
pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome or Chromium here")
DONE = "document.title='DONE'"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        launched = p.chromium.launch(executable_path=CHROME, headless=True)
        yield launched
        launched.close()


@pytest.fixture
def on(browser):
    """A page with `html` on it, and its session: (session, page)."""
    context = browser.new_context()

    def make(html: str):
        page = context.new_page()
        page.set_content(html)
        page.wait_for_timeout(100)
        return Session(context, page), page

    yield make
    context.close()


def pressed(page) -> bool:
    return page.title() == "DONE"


COMMITS = {
    "text inside": (f'<button onclick="{DONE}">Place order <span>₪112.40</span></button>', {"text": "₪112.40"}),
    "a framework's div": (
        (
            '<div style="cursor:pointer" id=b><div>Place order</div><div>₪112.40</div></div>'
            f'<script>b.addEventListener("click", () => {{ {DONE} }})</script>'
        ),
        {"text": "₪112.40"},
    ),
    "a label's control": (
        (
            f'<form onsubmit="{DONE};return false"><label for=s>Next step</label>'
            '<button id=s type=submit style="display:none">Place order</button></form>'
        ),
        {"text": "Next step"},
    ),
    "confirm by a total": (
        f'<form onsubmit="{DONE};return false"><p>Total ₪98.65</p><button>Confirm</button></form>',
        {"text": "Confirm"},
    ),
    "a payment page": (
        f'<form onsubmit="{DONE};return false"><input autocomplete=cc-number><button>Continue</button></form>',
        {"text": "Continue"},
    ),
}
KEYS = {
    "space": (f'<button id=b onclick="{DONE}">שלמו</button><script>b.focus()</script>', "Space"),
    "enter": (f'<button id=b onclick="{DONE}">Place order</button><script>b.focus()</script>', "Enter"),
}


@pytest.mark.parametrize("case", COMMITS)
def test_what_commits_is_never_clicked_whichever_way_its_asked_for(on, case):
    html, click = COMMITS[case]
    session, page = on(html)
    assert session.act([{"click": click}]).refused and not pressed(page)


@pytest.mark.parametrize("case", KEYS)
def test_a_key_on_what_commits_is_refused(on, case):
    html, key = KEYS[case]
    session, page = on(html)
    assert session.act([{"press": key}]).refused and not pressed(page)


def test_a_key_in_a_frame_is_refused(on):
    session, page = on(f'<iframe srcdoc="<button onclick=&quot;top.{DONE}&quot;>שלם</button>"></iframe>')
    page.frame_locator("iframe").locator("button").focus()
    assert session.act([{"press": "Enter"}]).refused and not pressed(page)


def test_a_point_on_a_frame_is_refused(on):
    session, _ = on('<iframe srcdoc="<button>Pay</button>" style="width:300px;height:100px"></iframe>')
    assert session.act([{"click_at": [50, 30]}]).refused == "a frame"


def test_what_only_confirms_a_choice_is_pressed(on):
    session, page = on(f'<dialog open><p>Pick a time: 19:30</p><button onclick="{DONE}">Confirm</button></dialog>')
    assert not session.act([{"click": {"text": "Confirm"}}]).refused and pressed(page)


def test_a_password_on_the_page_is_never_shown_to_the_model(on):
    form = "<form><label>Email <input name=e></label><label>Password <input type=password name=p></label></form>"
    session, page = on(form)
    page.fill("input[name=p]", "hunter2-secret")
    page.fill("input[name=e]", "alon@example.com")
    assert "hunter2" not in session.look().text


def test_an_address_at_home_is_never_opened(on):
    session, page = on("<p>hello</p>")
    outcome = session.act([{"goto": "http://127.0.0.1:9/"}])
    assert outcome.failed and page.url == "about:blank"


# Signing in, on pages served as a site of their own


SITE = "https://login.example"


@pytest.fixture
def site(browser):
    """A page served at SITE with `html` on it (each next page by the path a form posts to), and its session."""
    context = browser.new_context()

    def make(pages: dict[str, str]):
        def serve(route):
            path = route.request.url.removeprefix(SITE).split("?")[0] or "/"
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=pages.get(path, "<p>signed in</p>"))

        context.route(f"{SITE}/**", serve)
        page = context.new_page()
        page.goto(f"{SITE}/")
        return Session(context, page), page

    yield make
    context.close()


class Asked:
    def __init__(self, name="Alon", login=None, code=None):
        self.name, self.given, self.sent, self.asked = name, login, code, []

    def login(self, site, goal, fields):
        self.asked.append([f.label for f in fields])
        return self.given

    def code(self, site, answer):
        return self.sent


def vault_with(tmp_path, **values):
    from voice_assistant.browser.vault import Vault

    vault = Vault(tmp_path)
    if values:
        vault.store("Alon", [SITE], values)
    return vault


def test_a_login_without_ids_or_names_is_typed_and_its_confirm_button_is_fine(site, tmp_path):
    from voice_assistant.browser.login import sign_in

    _, page = site({"/": '<form action="/next" method=post><label>דוא״ל <input type=email></label>'
                         '<label>סיסמה <input type=password></label><button>אישור</button></form>'})  # fmt: skip
    vault = vault_with(tmp_path, username="alon@example.com", password="hunter2-secret")
    said = sign_in(page, vault, Asked())
    assert "entered the username and the password" in said and page.url == f"{SITE}/next"


def test_a_code_in_six_boxes_is_typed_a_character_a_box(site, tmp_path):
    from voice_assistant.browser.login import sign_in

    boxes = "".join(f"<input inputmode=numeric maxlength=1 autocomplete=one-time-code name=d{i}>" for i in range(6))
    _, page = site({"/": f'<form action="/done" method=post>{boxes}<button>Verify</button></form>'})
    sign_in(page, vault_with(tmp_path, username="alon@example.com"), Asked(code="482913"))
    assert page.url == f"{SITE}/done"


def test_a_promo_code_is_no_sign_in_code_and_a_sign_up_gets_nothing(site, tmp_path):
    from voice_assistant.browser.login import FIELDS_JS, sign_in

    _, page = site({"/": "<form><label>Promo code <input name=promo></label><label>Email <input type=email></label>"
                         "</form>"})  # fmt: skip
    assert page.evaluate(FIELDS_JS)["code"] is None
    page.set_content('<form><input type=email><input type=password autocomplete="new-password"></form>')
    said = sign_in(page, vault_with(tmp_path, username="alon@example.com"), Asked())
    assert "sign up" in said or "no sign-in form" in said


def test_a_mailboxs_address_never_goes_into_a_phone_field(site, tmp_path):
    from voice_assistant.browser.login import sign_in

    _, page = site({"/": '<form action="/next"><label>Phone <input type=tel></label><button>Send</button></form>'})
    vault = vault_with(tmp_path)
    vault.store_mailbox("Alon", "alon@gmail.com", "abcd efgh")
    person = Asked(login={"username": "0541234567"})
    sign_in(page, vault, person, wait_for_mail=lambda *a, **k: (None, None))
    assert person.asked == [["Phone", "Password, if it asks for one next"]]


def test_a_password_in_a_shadow_root_or_shown_as_text_is_never_shown_either(on):
    session, page = on("<div id=host></div><input type=password id=p><button id=show>show</button><script>"
                       "host.attachShadow({mode: 'open'}).innerHTML = '<input type=password id=s>';"
                       "show.onclick = () => { p.type = 'text'; };</script>")  # fmt: skip
    page.locator("#host").locator("#s").fill("shadow-secret")
    page.fill("#p", "toggled-secret")
    session.look()  # marks the password fields, while they are
    page.click("#show")
    seen = session.look().text
    assert "shadow-secret" not in seen and "toggled-secret" not in seen
