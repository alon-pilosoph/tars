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
