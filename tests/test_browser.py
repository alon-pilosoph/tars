"""The browser agent, offline: the page as the model reads it (from Playwright's accessibility snapshot), the gate that
keeps a model from committing the person, the session's steps and the agent's turns, approvals and stops, with a
stand-in browser and a stand-in model."""

import json
import types

import pytest

from voice_assistant.browser import agent as agents
from voice_assistant.browser import page as pages
from voice_assistant.browser.agent import Agent, Settings
from voice_assistant.browser.gate import commits
from voice_assistant.browser.session import Outcome, Refused, Session
from voice_assistant.browser.tools import _total

CARD_PAGE = """- generic [ref=e1] [box=0,0,1200,700]:
  - banner [ref=e2] [box=0,0,1200,70]:
    - link "Wolt home" [ref=e3] [cursor=pointer] [box=8,1,120,68]:
      - /url: /en/discovery
    - textbox "Search in Wolt..." [ref=e4] [box=470,15,267,40]: pizza
    - button "Clear search" [disabled] [box=712,35,0,0]
  - main [ref=e5] [box=0,70,1200,600]:
    - heading "Search results" [level=1] [ref=e6] [box=32,80,300,40]
    - list [ref=e7] [box=32,130,700,400]:
      - listitem [ref=e8] [box=32,130,700,100]:
        - link "Pizza \\"Kikko\\" Wolt+" [ref=e9] [cursor=pointer] [box=32,130,300,30]:
          - /url: /en/isr/tel-aviv/restaurant/pizza-kikko
          - generic [ref=e10]: Pizza Kikko
        - generic [ref=e11] [box=32,170,100,20]: 25-35 min
        - generic [ref=e12] [box=140,170,100,20]:
          - img "Rating" [ref=e13] [box=140,170,16,16]
          - generic [ref=e14] [box=160,170,30,20]: "8.6"
"""

DIALOG_PAGE = """- generic [ref=e1] [box=0,0,1200,700]:
  - heading "Pulcinella" [level=1] [ref=e2] [box=32,260,400,48]
  - button "Order together" [ref=e3] [cursor=pointer] [box=900,376,160,36]
  - dialog [ref=e4] [box=380,24,520,694]:
    - button "Close the dialog" [ref=e5] [cursor=pointer] [box=844,40,40,40]
    - heading "Margherita" [level=2] [ref=e6] [box=396,330,200,40]
    - group "Choose a side" [ref=e7] [box=0,0,0,0]:
      - paragraph [ref=e8] [box=396,544,345,20]: Choose at least one item
      - generic [ref=e9] [box=396,579,488,24]:
        - checkbox "No, thanks" [ref=e10] [box=396,579,13,13]
        - generic [ref=e11] [box=436,579,448,24]: No, thanks
    - button "Add to order ₪66.00" [ref=e12] [cursor=pointer] [box=564,646,320,56]
"""


def long_page(n: int) -> str:
    rows = "\n".join(
        f"  - listitem [ref=e{100 + i}] [box=0,{i * 50},800,40]:\n"
        f'    - button "Item {i}" [ref=e{1000 + i}] [cursor=pointer] [box=0,{i * 50},200,40]\n'
        f"    - generic [ref=e{2000 + i}] [box=210,{i * 50},300,40]: {'words ' * 30}"
        + (f'\n  - heading "Section {i}" [level=2] [ref=e{3000 + i}] [box=0,{i * 50 + 45},400,5]' * (i % 20 == 0))
        for i in range(n)
    )
    return f"- list [ref=e1] [box=0,0,800,{n * 50}]:\n{rows}"


# The page as the model reads it


def test_a_snapshot_line_is_read_into_role_name_ref_state_and_value():
    root = pages.parse(CARD_PAGE)
    banner = root.children[0].children[0]
    link, box = banner.children[0], banner.children[1]
    assert (link.role, link.name, link.ref, link.pointer, link.url) == (
        "link", "Wolt home", "e3", True, "/en/discovery")  # fmt: skip
    assert (box.role, box.name, box.value, box.box) == ("textbox", "Search in Wolt...", "pizza", (470, 15, 267, 40))
    assert banner.children[2].states == ["disabled"]
    card = root.children[0].children[1].children[1].children[0]
    assert card.children[0].name == 'Pizza "Kikko" Wolt+'  # an escaped quote in a name
    assert card.children[2].children[1].value == "8.6"  # a quoted value


def test_a_card_is_one_line_and_only_what_acts_has_a_ref():
    view = pages.view(CARD_PAGE, "Search Results | Wolt", "https://wolt.com/en/search?q=pizza", 700)
    assert view.whole and not view.dialog
    text = view.text
    assert '[e4 textbox "Search in Wolt..." = "pizza"]' in text
    assert "# Search results" in text
    card = next(line for line in text.splitlines() if "e9" in line)
    assert card.strip() == '- [e9 link "Pizza \\"Kikko\\" Wolt+"] 25-35 min (image: Rating) 8.6'
    assert set(view.refs) == {"e3", "e4", "e9"}  # not the wrappers, the image or the button of no size
    assert "Clear search" not in text


def test_an_open_dialog_is_the_view_with_what_a_wrapper_of_no_size_holds():
    view = pages.view(DIALOG_PAGE, "Margherita | Pulcinella", "https://wolt.com/x", 700)
    assert view.dialog
    text = view.text
    assert text.index("dialog:") < text.index("The page behind it")
    assert '[e10 checkbox "No, thanks"]' in text  # inside a fieldset whose box has no size
    assert text.count("No, thanks") == 1  # its label beside it isn't said again
    assert "Choose at least one item" in text
    behind = text.split("The page behind it", 1)[1]
    assert "# Pulcinella" in behind and "e3" not in behind  # only an outline: nothing to click behind the dialog


def test_a_long_page_is_shown_around_the_screen_and_find_searches_all_of_it():
    view = pages.view(long_page(300), "Long", "https://example.com", 700)
    assert not view.whole and len(view.text) <= pages.LIMIT + 60
    assert "[e1000 button" in view.text and "[e1299 button" not in view.text
    assert "Further down" in view.text and "## Section 280" in view.text  # what's below, by its headings
    found = view.find("item 299")
    assert "e1299" in found and found.startswith("1 line(s)")
    assert "nothing on this page" in view.find("no such words")


def test_a_long_line_is_broken_between_its_parts_not_cut():
    buttons = "\n".join(f'  - button "Option number {i}" [ref=e{i}] [box=0,0,100,20]' for i in range(1, 60))
    view = pages.view(f"- paragraph [ref=e999] [box=0,0,800,600]:\n{buttons}", "T", "U", 700)
    assert all(f"[e{i} button" in view.text for i in range(1, 60))
    assert max(len(line.strip()) for line in view.text.splitlines()) <= pages.LINE


# The gate


@pytest.mark.parametrize(
    "label",
    ["Place order", "Click to order", "Order ₪112.40", "Order · ₪80", "Pay now", "Complete booking", "Reserve",
     "Confirm reservation", "Buy now", "Request to book", "Continue to payment", "בצע הזמנה", "המשך לתשלום",
     "אישור הזמנה", "הזמן"],
)  # fmt: skip
def test_what_commits_is_stopped(label):
    assert commits(label)


@pytest.mark.parametrize(
    "label",
    ["Order again", "Your orders", "Orders", "Book a table", "Reserve a table", "Add to order ₪66.00", "Add to cart",
     "Go to checkout", "Checkout", "Next: Final details", "I'll reserve", "Order history", "הזמן שוב", "ההזמנות שלי",
     "אמצעי תשלום", "משלוח", "PayPal account"],
)  # fmt: skip
def test_what_only_shows_or_starts_something_is_not(label):
    assert not commits(label)


# The session's steps


class FakeLocator:
    def __init__(self, page, ref):
        self.page, self.ref, self.first = page, ref, self

    def count(self):
        return 1 if self.ref in self.page.elements else 0

    def is_visible(self):
        return not self.page.elements[self.ref].get("hidden")

    def click(self, timeout=None):
        self.page.done.append(("click", self.ref))
        if opens := self.page.elements[self.ref].get("opens"):
            self.page.shape = opens

    def fill(self, text, timeout=None):
        self.page.done.append(("fill", self.ref, text))

    def press(self, key):
        self.page.done.append(("press", self.ref, key))

    def evaluate(self, script):
        return self.page.elements[self.ref].get("submits", "")

    def inner_text(self, timeout=None):
        return self.page.elements[self.ref].get("text", "")

    def get_attribute(self, name):
        return None


class FakePage:
    url = "https://shop.example/checkout"

    def __init__(self, elements):
        self.elements, self.done = elements, []
        self.viewport_size = {"width": 1200, "height": 700}
        self.shape = ""

    def wait_for_timeout(self, ms):
        return None

    def is_closed(self):
        return False

    def on(self, event, handler):
        return None

    def locator(self, selector):
        return FakeLocator(self, selector.removeprefix("aria-ref="))

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def evaluate(self, script, arg=None):
        return self.shape if "aria-expanded" in script else 700

    def aria_snapshot(self, **kwargs):
        return '- button "Place order" [ref=e2] [box=0,0,100,40]'

    def title(self):
        return "Checkout"


def session_on(elements) -> tuple[Session, FakePage]:
    page = FakePage(elements)
    context = types.SimpleNamespace(pages=[page], on=lambda event, handler: None)
    return Session(context), page


def test_a_click_on_what_commits_is_refused_and_not_done():
    session, page = session_on({"e1": {"text": "Add to cart"}, "e2": {"text": "Place order"}})
    session.look()
    outcome = session.act([{"click": "e2"}])
    assert outcome.refused == "Place order" and outcome.failed
    assert ("click", "e2") not in page.done
    assert "request_approval" in str(outcome)


def test_enter_that_would_submit_a_commit_is_refused():
    session, page = session_on({"e1": {"submits": "Complete booking"}, "e3": {"submits": "Search"}})
    outcome = session.act([{"type": "e1", "text": "Alon", "submit": True}])
    assert outcome.refused == "Complete booking"
    assert ("press", "e1", "Enter") not in page.done
    session.act([{"type": "e3", "text": "athens", "submit": True}])
    assert ("press", "e3", "Enter") in page.done


def test_a_batch_stops_when_a_step_opens_something_and_a_hidden_target_fails_fast():
    session, page = session_on({"e1": {"opens": "listbox"}, "e2": {}, "e3": {"hidden": True}})
    outcome = session.act([{"click": "e1"}, {"click": "e2"}])
    assert page.done == [("click", "e1")] and not outcome.failed
    assert "1 step(s) after it weren't done" in str(outcome)
    outcome = session.act([{"click": "e3"}])
    assert outcome.failed and "hidden" in str(outcome) and ("click", "e3") not in page.done


def test_a_ref_that_isnt_on_the_page_fails_with_why():
    session, _ = session_on({})
    outcome = session.act([{"click": "e9"}, {"click": "e10"}])
    assert outcome.failed and len(outcome.said) == 1  # it stops at the first that fails
    assert "isn't on the page" in str(outcome)
    assert session.act([{"click": "javascript:alert(1)"}]).failed


# The agent


def call(name: str, **args) -> types.SimpleNamespace:
    return types.SimpleNamespace(type="function_call", call_id=f"c-{name}", name=name, arguments=json.dumps(args))


class FakeModel:
    """Answers each turn with the next of `calls`, and keeps what it was sent."""

    def __init__(self, *calls):
        self.calls, self.sent = list(calls), []
        self.responses = self

    def create(self, **request):
        self.sent.append(json.loads(json.dumps(request, default=str)))
        usage = types.SimpleNamespace(input_tokens=100, output_tokens=10, input_tokens_details=None)
        return types.SimpleNamespace(output=[self.calls.pop(0)], usage=usage)


class FakeSession:
    """A browser with one page that changes only when a step says so."""

    def __init__(self, text="Title: Shop\nAddress: https://shop.example/checkout\n\nTotal ₪98.65", refs=None):
        self.page = types.SimpleNamespace(url="https://shop.example/checkout")
        self.text, self.refs, self.acted, self.pressed = text, refs or {}, [], []
        self.current = None
        self.outcomes: list[Outcome] = []

    def look(self):
        self.current = types.SimpleNamespace(text=self.text, url=self.page.url, refs=self.refs,
                                             lines=self.text.splitlines() * 4, find=lambda q: f"found {q}")  # fmt: skip
        return self.current

    def act(self, steps):
        self.acted.append(steps)
        outcome = self.outcomes.pop(0) if self.outcomes else Outcome(said=["ok"], changed=True)
        self.look()
        return outcome

    def screenshot(self):
        return b"jpeg"

    def take_network(self):
        return []

    def click_approved(self, ref):
        self.pressed.append(ref)


class Person:
    def __init__(self, yes=True):
        self.yes, self.asked = yes, []

    def approve(self, draft, picture=None):
        self.asked.append(draft)
        return self.yes


def place_order_button():
    return {"e2": pages.Node("button", name="Place order", ref="e2", box=(0, 0, 100, 40))}


def test_the_goal_stays_in_the_instructions_and_old_pages_shrink_to_a_line(tmp_path):
    model = FakeModel(call("act", steps=[{"click": "e1"}]), call("act", steps=[{"click": "e2"}]),
                      call("done", success=True, answer="Done."))  # fmt: skip
    session = FakeSession()
    result = Agent(model, session, Person(), trace=tmp_path / "t.jsonl").run("Find the price")
    assert result.ended == "done" and result.answer == "Done." and result.turns == 3
    last = model.sent[-1]
    assert "Find the price" in last["instructions"]
    pages_sent = [i for i in last["input"] if i.get("role") == "user"]
    assert sum("Total ₪98.65" in json.dumps(p, ensure_ascii=False) for p in pages_sent) == 1  # the newest only
    assert [json.loads(line)["event"] for line in (tmp_path / "t.jsonl").read_text().splitlines()][-1] == "end"


def test_a_dry_run_approval_presses_nothing():
    model = FakeModel(call("request_approval", ref="e2", summary="1 pizza"), call("done", success=True, answer="ok"))
    session, person = FakeSession(refs=place_order_button()), Person()
    result = Agent(model, session, person).run("Order a pizza")
    assert session.pressed == [] and len(person.asked) == 1
    assert "₪98.65" in person.asked[0] and "Place order" in person.asked[0]
    assert "dry run" in model.sent[-1]["input"][-2]["output"]
    assert result.commits_asked == person.asked


def test_a_real_approval_presses_only_on_a_yes_with_the_same_total():
    for yes, pressed in ((True, ["e2"]), (False, [])):
        model = FakeModel(call("request_approval", ref="e2", summary="1 pizza"), call("done", success=True, answer=""))
        session = FakeSession(refs=place_order_button())
        Agent(model, session, Person(yes), Settings(really_commit=True)).run("Order a pizza")
        assert session.pressed == pressed


def test_a_total_that_changed_while_asking_is_not_pressed():
    model = FakeModel(call("request_approval", ref="e2", summary="1 pizza"), call("done", success=True, answer=""))
    session = FakeSession(refs=place_order_button())

    class Slow(Person):
        def approve(self, draft, picture=None):
            session.text = session.text.replace("98.65", "120.00")
            return True

    Agent(model, session, Slow(), Settings(really_commit=True)).run("Order a pizza")
    assert session.pressed == []
    assert "changed" in model.sent[-1]["input"][-2]["output"]


def test_prices_that_changed_while_asking_are_not_pressed_even_without_a_total():
    model = FakeModel(call("request_approval", ref="e2", summary="a room"), call("done", success=True, answer=""))
    page = "Title: Hotel\nAddress: https://booking.example\n\nDouble room ₪311"
    session = FakeSession(text=page, refs=place_order_button())

    class Slow(Person):
        def approve(self, draft, picture=None):
            session.text = session.text.replace("311", "390")
            return True

    Agent(model, session, Slow(), Settings(really_commit=True)).run("Book it")
    assert session.pressed == []
    assert "prices on the page changed" in model.sent[-1]["input"][-2]["output"]


@pytest.mark.parametrize("label", ["1 Go to checkout ₪73.00", "מעבר לעגלת הקניות", "View basket"])
def test_going_to_the_checkout_needs_no_approval(label):
    refs = {"e5": pages.Node("button", name=label, ref="e5", box=(0, 0, 100, 40))}
    model = FakeModel(call("request_approval", ref="e5", summary="checkout"), call("done", success=False, answer=""))
    person = Person()
    Agent(model, FakeSession(refs=refs), person).run("Order a pizza")
    assert person.asked == []
    assert "press it yourself" in model.sent[-1]["input"][-2]["output"]


def test_a_step_that_failed_isnt_tried_again_and_a_run_that_changes_nothing_stops():
    model = FakeModel(*[call("act", steps=[{"click": "e7"}])] * 6)
    session = FakeSession()
    session.outcomes = [Outcome(said=["failed: e7"], failed=True, changed=False)] + [
        Outcome(said=["ok"], changed=False) for _ in range(5)
    ]
    result = Agent(model, session, Person(), Settings(stuck_after=4)).run("Click it")
    assert len(session.acted) == 1  # the same failed click wasn't done again
    assert result.ended == "stuck" and result.turns == 4
    assert result.screenshots >= 2  # a picture after the failure and with each refusal


def test_a_follow_up_carries_on_in_the_same_conversation():
    model = FakeModel(call("done", success=True, answer="ordered"), call("done", success=True, answer="tidied"))
    agent = Agent(model, FakeSession(), Person())
    agent.run("Order a pizza")
    result = agent.follow_up("Take out what you put in")
    assert result.answer == "tidied"
    assert "Take out what you put in" in json.dumps(model.sent[-1]["input"])


def test_the_total_is_read_off_the_page():
    assert _total("Subtotal ₪73.00\nDelivery ₪12.00\nTotal\n₪85.00") == "₪85.00"
    assert _total('סה"כ לתשלום 113.40 ₪') == "113.40 ₪"
    assert _total("Nothing to pay here") is None


def test_the_tools_say_what_they_take():
    names = {t["name"] for t in agents.TOOLS}
    assert names == {"act", "find", "look", "request_approval", "sign_in", "done"}
    assert "{goal}" in agents.HEADER and "{language}" in agents.HEADER and "{{" not in agents.RULES


def test_refused_carries_the_label():
    assert Refused("Pay now", "no").label == "Pay now"


# What it learns about a site


def test_a_site_is_its_domain_under_a_country_too():
    from voice_assistant.browser.notes import site

    assert site("https://wolt.com/en/isr/tel-aviv") == "wolt.com"
    assert site("https://www.ksp.co.il/web/cart") == "ksp.co.il"
    assert site("https://secure.booking.com/book.html") == "booking.com"
    assert site("about:blank") == ""


def test_notes_are_written_from_a_run_and_read_on_the_next_one(tmp_path):
    from voice_assistant.browser.notes import Notes

    notes = Notes(tmp_path)
    urls = ["https://wolt.com/a", "https://wolt.com/b", "https://other.com/", "https://wolt.com/c"]
    turns = [{"turn": i, "call": "act", "args": {}, "said": "ok", "url": url} for i, url in enumerate(urls)]
    learner = types.SimpleNamespace(
        responses=types.SimpleNamespace(
            create=lambda **request: types.SimpleNamespace(
                output_text="- search: https://wolt.com/en/search?q=<words>", usage=None
            )
        )
    )
    written, _, _ = notes.learn(learner, "m", "Order a pizza", "done", turns)
    assert notes.read("wolt.com") == written.strip() and "search?q=" in written
    assert notes.learn(learner, "m", "x", "done", turns[:2])[0] == ""  # too short to learn from
    assert notes.named_in("Order a pizza on Wolt") == ["wolt.com"] and notes.named_in("Order a pizza") == []
    with pytest.raises(ValueError):
        notes.read("../secrets")

    model = FakeModel(call("done", success=True, answer="ok"))
    agent = Agent(model, FakeSession(), Person(), notes=notes)
    agent.run("Order a pizza on Wolt")
    first = json.dumps(model.sent[0]["input"][0], ensure_ascii=False)
    assert "What TARS noted on wolt.com" in first and "search?q=" in first


def test_a_menu_that_just_opened_is_shown_first_once():
    menu = (
        "  - menu [ref=e90] [box=900,40,200,100]:\n"
        '    - menuitem "Price (lowest first)" [ref=e91] [box=900,40,200,20]\n'
    )
    snapshot = long_page(300) + "\n" + menu.replace("  - menu", "- menu", 1).replace("    - menuitem", "  - menuitem")
    first = pages.view(snapshot, "T", "U", 700)
    assert first.text.index("Just opened") < first.text.index("[e1000 button")
    assert '[e91 menuitem "Price (lowest first)"]' in first.text and first.popups == {"e90"}
    again = pages.view(snapshot, "T", "U", 700, first.popups)
    assert "Just opened" not in again.text


def test_the_same_steps_again_and_again_are_stopped():
    model = FakeModel(*[call("act", steps=[{"click": "e7"}])] * 3, call("done", success=False, answer=""))
    session = FakeSession()
    Agent(model, session, Person()).run("Sort it")
    assert len(session.acted) == 2
    assert "times in a row" in json.dumps(model.sent[-1]["input"])


def test_a_click_by_text_or_at_a_point_is_gated_too():
    session, page = session_on({"e1": {}})
    page.get_by_text = lambda text, exact: types.SimpleNamespace(all=lambda: [FakeLocator(page, "e1")])
    assert not session.act([{"click": {"text": "Tel Aviv"}}]).failed and ("click", "e1") in page.done
    outcome = session.act([{"click": {"text": "Place order"}}])
    assert outcome.refused == "Place order"
    page.evaluate = lambda script, arg=None: "Pay now" if "elementFromPoint" in script else 700
    page.mouse = types.SimpleNamespace(click=lambda x, y: page.done.append(("click_at", x, y)))
    assert session.act([{"click_at": [10, 20]}]).refused == "Pay now"
    assert not any(d[0] == "click_at" for d in page.done)


@pytest.mark.parametrize(
    ("written", "done"),
    [
        ({"action": "click", "ref": "e4"}, {"click": "e4"}),
        ({"action": "click", "text": "Tel Aviv"}, {"click": {"text": "Tel Aviv"}}),
        ({"action": "click", "ref": "e63", "text": "Friday, 30 October"}, {"click": {"text": "Friday, 30 October"}}),
        ({"action": "click", "ref": "e63", "text": ""}, {"click": "e63"}),
        ({"action": "click", "text": "20:00", "near": "ESO"}, {"click": {"text": "20:00", "near": "ESO"}}),
        (
            {"action": "type", "ref": "e4", "text": "pizza", "submit": True},
            {"type": "e4", "text": "pizza", "submit": True},
        ),
        ({"type": "fill", "ref": "e10", "text": "Sony"}, {"type": "e10", "text": "Sony"}),
        ({"action": "scroll", "direction": "down", "ref": "e20"}, {"scroll": "down", "in": "e20"}),
        ({"action": "scroll", "ref": "e88"}, {"scroll": "e88"}),
        ({"action": "press", "key": "Escape"}, {"press": "Escape"}),
        ({"action": "goto", "url": "https://x.com"}, {"goto": "https://x.com"}),
        ({"action": "click_at", "x": 5, "y": 6}, {"click_at": [5, 6]}),
        ({"click": "e4"}, {"click": "e4"}),
        ({"type": "e4", "text": "x"}, {"type": "e4", "text": "x"}),
    ],
)
def test_a_step_as_written_is_read_into_what_is_done(written, done):
    from voice_assistant.browser.session import normal

    assert normal(written) == done


def test_a_run_that_may_have_filled_a_basket_is_tidied():
    from voice_assistant.browser.run import _filled

    def act(said):
        return {"call": "act", "said": said}

    assert _filled([act("ok: clicked e1 'Add to order ₪66.00'")])
    assert _filled([act("ok: clicked e9 'Order again'")])
    assert _filled([act("ok: clicked e3 'הוסף לסל'")])
    assert _filled([act("ok: clicked e5570 'להוסיף לעגלת כבל USB-C ל-USB-C'")])
    assert not _filled([act("ok: clicked e2 'Search'"), {"call": "find", "said": "Add to cart"}])


def test_a_tidying_run_is_told_what_the_run_clicked():
    from voice_assistant.browser.run import tidy_goal

    turns = [
        {
            "call": "act",
            "url": "https://wolt.com/x",
            "said": "ok: clicked e5 'Pizza Har Sinai'\nok: clicked e9 "
            + json.dumps("Add to order ₪66.00", ensure_ascii=False),
        },
        {"call": "act", "url": "https://wolt.com/x", "said": "ok: clicked the text 'Tel Aviv'"},
        {"call": "find", "url": "https://wolt.com/x", "said": "clicked e1 'not a click'"},
    ]
    goal = tidy_goal(turns)
    assert "Empty the basket or cart on wolt.com" in goal
    assert "'Pizza Har Sinai'; 'Add to order ₪66.00'; 'Tel Aviv'" in goal
    assert "not a click" not in goal


def test_what_a_step_added_to_the_same_page_is_said_first():
    before = pages.view(CARD_PAGE, "T", "U", 700)
    picker = (
        CARD_PAGE
        + "  - generic [ref=e50] [box=0,600,300,40]:\n    - generic [ref=e51] [box=0,600,300,20]: Friday 30 October\n"
    )
    after = pages.view(picker, "T", "U", 700).with_new([ln.text for ln in before.lines])
    new = after.text.split("New on the page since your last step:\n", 1)[1].split("\n\n", 1)[0]
    assert new.strip() == "Friday 30 October"
    assert after.text.index("New on the page") < after.text.index("# Search results")
    assert pages.view(CARD_PAGE, "T", "U", 700).with_new([ln.text for ln in before.lines]).text == before.text


def test_text_is_quoted_for_xpath_whatever_its_quotes():
    from voice_assistant.browser.session import _xpath

    assert _xpath("20:00") == "'20:00'"
    assert _xpath("Bob's") == '"Bob\'s"'
    assert _xpath("""a'b"c""") == "concat('a', \"'\", 'b\"c')"


# Claude Code as the brain


def test_claude_code_gets_only_tars_browser_and_fenced_notes(tmp_path, monkeypatch):
    from voice_assistant.browser import claude

    monkeypatch.setattr(claude, "HOME", tmp_path / "home")
    monkeypatch.setattr(claude, "NOTES", tmp_path / "home/notes")
    ran = []

    def fake_run(command, **kwargs):
        ran.append((command, kwargs))
        return types.SimpleNamespace(stdout='{"type": "result", "usage": {}, "result": "ok"}', stderr="", returncode=0)

    monkeypatch.setattr(claude.subprocess, "run", fake_run)

    def flag(command, name):
        i = command.index(name)
        return command[i + 1 : next((j for j in range(i + 1, len(command)) if command[j].startswith("--")), None)]

    claude.run_claude("Order a pizza on Wolt", tmp_path / "t.jsonl")
    command, kwargs = ran[-1]
    assert flag(command, "--tools") == [""] and flag(command, "--allowedTools") == ["mcp__tars-browser"]
    assert flag(command, "--permission-mode") == ["dontAsk"] and "--strict-mcp-config" in command
    assert flag(command, "--setting-sources") == ["project"] and kwargs["cwd"] == tmp_path / "home"
    written = (tmp_path / "home/CLAUDE.md").read_text()
    assert agents.RULES in written and "Notes on sites" not in written
    assert "Order a pizza on Wolt" in command[command.index("-p") + 1]

    (tmp_path / "home/notes").mkdir(parents=True)
    (tmp_path / "home/notes/wolt.com.md").write_text("- search: /en/search?q=<words>")
    claude.run_claude("Order a pizza on Wolt", tmp_path / "t2.jsonl", notes=True)
    command, _ = ran[-1]
    assert flag(command, "--tools") == ["Read,Edit,Write"]
    assert set(flag(command, "--allowedTools")) == {
        "mcp__tars-browser", "Read(./notes/**)", "Edit(./notes/**)", "Write(./notes/**)"}  # fmt: skip
    assert "Notes on sites" in (tmp_path / "home/CLAUDE.md").read_text()
    assert "notes/wolt.com.md" in command[command.index("-p") + 1]
