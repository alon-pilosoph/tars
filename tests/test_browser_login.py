"""TARS's vault and its signing in: logins typed only into their own site, by code, and never into the trace."""

import types

import pytest

from voice_assistant.browser.login import Unattended, sign_in
from voice_assistant.browser.session import _blank
from voice_assistant.browser.tools import Tools
from voice_assistant.browser.vault import Vault, origin

pytest.importorskip("cryptography")

KSP = "https://www.ksp.co.il"


def field(ident, action=None, empty=True, label=""):
    return {"tag": ident, "empty": empty, "action": action, "kind": "other", "key": ident, "label": label}


def screen(url=KSP + "/login", username=None, password=None, code=None, signup=False, others=()):
    """A page of a sign-in, as FIELDS_JS reads it: its code box, or its form's fields, in order."""
    fields = [{**username, "kind": "username", "key": "username"}] if username else []
    fields += list(others)
    fields += [{**password, "kind": "password", "key": "password"}] if password else []
    return {"url": url, "found": {"code": {**code, "kind": "code"} if code else None, "fields": fields,
                                  "signup": signup}}  # fmt: skip


class SignInPage:
    """A site's sign-in, a screen at a time: Enter in a field goes on to the next screen."""

    def __init__(self, *screens):
        self.screens, self.at, self.typed = list(screens), 0, []
        self.frames = []  # none that takes payment

    @property
    def url(self):
        return self.screens[self.at]["url"]

    def evaluate(self, script):
        if "signup" in script:
            return self.screens[self.at]["found"]
        return ""  # the page's alerts: none

    def locator(self, selector):
        page, tag = self, "#" + selector.split('"')[1]  # the field by its tag: written as #tag here, for reading

        class Field:
            first = None

            def evaluate(self, script):
                return {"labels": ["Sign in"]}  # its form's submit button

            def fill(self, text):
                page.typed.append((tag, text))

            def press(self, key):
                page.at = min(page.at + 1, len(page.screens) - 1)

        found = Field()
        found.first = found
        return found

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, ms):
        return None

    def title(self):
        return "KSP"


class Asked:
    """The person, answering what they're asked."""

    def __init__(self, name="Alon", login=None, code=None):
        self.name, self.given, self.sent, self.asked = name, login, code, []

    def login(self, site, goal, fields):
        self.asked.append(("login", site, [f.key for f in fields]))
        return self.given

    def code(self, site, answer):
        self.asked.append(("code", site))
        return self.sent or answer.wait(2)


def test_a_login_is_for_its_exact_site_and_person_only(tmp_path):
    vault = Vault(tmp_path)
    vault.store("Alon", [KSP + "/web/"], {"username": "alon@example.com", "password": "hunter2-secret"})
    assert vault.for_page(KSP + "/login?next=/cart", "Alon").username == "alon@example.com"
    for elsewhere in ("http://www.ksp.co.il/login", "https://ksp.co.il/login", "https://www.ksp.co.il.evil.example/",
                      "https://www.ksp.co.il:8443/login"):  # fmt: skip
        assert vault.for_page(elsewhere, "Alon") is None, elsewhere
    assert vault.for_page(KSP + "/login", "Dana") is None
    assert "hunter2" not in repr(vault.logins("Alon")) and b"hunter2" not in (tmp_path / "vault.enc").read_bytes()
    assert (tmp_path / "vault.key").stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        vault.store("Alon", ["http://www.ksp.co.il"], {"username": "alon@example.com", "password": "x"})
    assert vault.remove("Alon", "www.ksp.co.il") and vault.logins("Alon") == []


def test_it_types_the_username_the_password_and_the_code_and_hides_the_secrets(tmp_path):
    vault = Vault(tmp_path)
    vault.store("Alon", [KSP], {"username": "alon@example.com", "password": "hunter2-secret"})
    page = SignInPage(screen(username=field("email")), screen(password=field("pw")), screen(code=field("otp")),
                      screen(url=KSP + "/account"))  # fmt: skip
    person, hidden = Asked(code="123456"), []
    said = sign_in(page, vault, person, hide=hidden.append)
    assert page.typed == [("#email", "alon@example.com"), ("#pw", "hunter2-secret"), ("#otp", "123456")]
    assert person.asked == [("code", KSP)]
    assert hidden == ["hunter2-secret", "123456"]
    assert "hunter2" not in said and "123456" not in said and "now at https://www.ksp.co.il" in said


def test_a_login_it_lacks_is_asked_for_the_site_the_browser_is_at_and_kept(tmp_path):
    vault = Vault(tmp_path)
    page = SignInPage(screen(username=field("email"), password=field("pw")), screen(url=KSP + "/account"))
    person = Asked(login={"username": "alon@example.com", "password": "hunter2-secret"})
    sign_in(page, vault, person, goal="order a cable")
    assert person.asked == [("login", KSP, ["username", "password"])]
    assert page.typed == [("#email", "alon@example.com"), ("#pw", "hunter2-secret")]
    assert vault.for_page(KSP, "Alon").origins == (KSP,)


def test_nothing_is_typed_into_a_form_that_sends_elsewhere_or_a_sign_up(tmp_path):
    vault = Vault(tmp_path)
    vault.store("Alon", [KSP], {"username": "alon@example.com", "password": "hunter2-secret"})
    page = SignInPage(screen(password=field("pw", action="https://collect.evil.example/steal")))
    assert "refused" in sign_in(page, vault, Asked()) and page.typed == []
    page = SignInPage(screen(username=field("email"), signup=True))
    assert "sign up" in sign_in(page, vault, Asked()) and page.typed == []


def test_a_site_asking_again_is_not_retried(tmp_path):
    vault = Vault(tmp_path)
    vault.store("Alon", [KSP], {"username": "alon@example.com", "password": "wrong"})
    page = SignInPage(screen(password=field("pw")), screen(password=field("pw")))
    said = sign_in(page, vault, Asked())
    assert "asked for the password again" in said and len(page.typed) == 1


def test_with_no_one_to_ask_a_missing_login_is_said_so(tmp_path):
    page = SignInPage(screen(username=field("email"), password=field("pw")))
    said = sign_in(page, Vault(tmp_path), Unattended("Alon"))
    assert "no login" in said and page.typed == []


def test_what_a_sign_in_sent_is_blanked_out_of_the_trace():
    body = "email=alon%40example.com&password=p%40ss+word%21&otp=123456"
    assert _blank(body, {"p@ss word!", "123456"}) == "email=alon%40example.com&password=•••&otp=•••"
    assert _blank('{"password": "p\\"ss"}', {'p"ss'}) == '{"password": "•••"}'


def test_the_sign_in_call_keeps_its_requests_out_of_the_trace():
    session = types.SimpleNamespace(network=["the login POST"], look=lambda: None)
    tools = Tools(session, approver=None, sign_in=lambda s: "entered the password")
    did = tools.call("sign_in", {})
    assert did.said == "entered the password" and session.network == []
    assert tools.call("sign_in", {}).trouble  # once a task


def test_origin_is_what_pages_are_compared_by():
    assert origin("HTTPS://WWW.KSP.CO.IL:443/web/cart?x=1") == KSP
    assert origin("https://www.ksp.co.il:8443/") == "https://www.ksp.co.il:8443"


def test_a_field_the_site_asks_later_is_asked_for_by_its_own_label_and_kept(tmp_path):
    vault = Vault(tmp_path)
    vault.store("Alon", [KSP], {"username": "alon@example.com", "password": "hunter2-secret"})
    page = SignInPage(screen(username=field("email"), password=field("pw")),
                      screen(others=[field("tz", label="מספר תעודת זהות")]), screen(url=KSP + "/account"))  # fmt: skip
    person, hidden = Asked(login={"tz": "012345678"}), []
    sign_in(page, vault, person, hide=hidden.append)
    assert person.asked == [("login", KSP, ["tz"])]
    assert page.typed == [("#email", "alon@example.com"), ("#pw", "hunter2-secret"), ("#tz", "012345678")]
    assert hidden == ["hunter2-secret", "012345678"]
    assert vault.for_page(KSP, "Alon").value("tz") == "012345678"
    page = SignInPage(screen(username=field("email"), password=field("pw")), screen(others=[field("tz")]),
                      screen(url=KSP + "/account"))  # fmt: skip
    sign_in(page, vault, Asked())  # next time, nothing's asked
    assert page.typed[-1] == ("#tz", "012345678")


def test_enter_in_a_form_whose_button_commits_is_never_pressed(tmp_path):
    vault = Vault(tmp_path)
    vault.store("Alon", [KSP], {"username": "alon@example.com"})
    page = SignInPage(screen(username=field("email")))
    original = page.locator

    def checkout_form(selector):
        found = original(selector)
        found.evaluate = lambda script: {"labels": ["Place order"]}  # a guest checkout: the field submits the order
        found.press = lambda key: pytest.fail("Enter pressed in a checkout form")
        return found

    page.locator = checkout_form
    assert "not a sign-in form" in sign_in(page, vault, Asked())


def test_a_code_is_not_typed_if_the_page_moved_on_while_it_came(tmp_path):
    vault = Vault(tmp_path)
    vault.store("Alon", [KSP], {"username": "alon@example.com"})
    page = SignInPage(screen(code=field("otp")), screen(url="https://elsewhere.example/login", code=field("otp")))

    def code(site, answer):
        page.at = 1  # the site sent the browser elsewhere meanwhile
        return "123456"

    said = sign_in(page, vault, types.SimpleNamespace(name="Alon", code=code))
    assert "moved on" in said and page.typed == []
