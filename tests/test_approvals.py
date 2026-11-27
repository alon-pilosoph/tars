"""What a phone call asks for that waits for a tap (approvals.py)."""

import dataclasses
import time
import types

import pytest

from voice_assistant import approvals
from voice_assistant.approvals import ASKED, NO_CHAT, TAP, TOO_LONG, Approvals, for_other, safe, shown, tap_listener
from voice_assistant.home import Request
from voice_assistant.toolbox import CHANGES, COLLECTS, ITS_OWN, READS, Tool, Toolbox, Toolset, Turn


def until(check, wait_s=5.0):
    deadline = time.monotonic() + wait_s
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


class Bot:
    def __init__(self):
        self.sent, self.dropped = [], []

    def answer_tap(self, tap):
        pass

    def drop_buttons(self, chat, message):
        self.dropped.append((chat, message))

    def send(self, chat, text):
        self.sent.append((chat, text))


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "POLL_S", 0.02)
    posts = []
    made = Approvals(tmp_path, {"robin": 7}.get, lambda chat, text, buttons: posts.append((chat, text, buttons)))
    bot = Bot()
    take = tap_listener(tmp_path, bot)

    def tap(verdict="yes", chat=7, n=0):
        data = posts[n][2][0 if verdict == "yes" else 1][1]
        return take({"callback_query": {"id": "1", "data": data, "from": {"id": chat}, "message": {"message_id": 5}}})

    return types.SimpleNamespace(asks=made, posts=posts, tap=tap, bot=bot, take=take, folder=tmp_path / "approvals")


def a_tool(name, does, done, answer="done"):
    spec = {"name": name}
    return Tool(spec, does, lambda args, turn: done.append((args, turn.request)) or answer)


def call(desk, tool, args, person="robin", **request):
    request = Request(person, by_web=True, heard="do it", **request)
    request = dataclasses.replace(request, approve=desk.asks.check(person, "do it"))
    box = Toolbox([Toolset([tool])])
    return box.call(tool, args, Turn(request))


def test_what_is_safe_is_done_at_once(desk):
    done = []
    assert call(desk, a_tool("home", ITS_OWN, done), {"device": "lamp", "action": "on"}) == "done"
    assert call(desk, a_tool("tars", ITS_OWN, done), {"action": "about"}) == "done"
    assert call(desk, a_tool("weather", READS, done), {}) == "done"
    assert len(done) == 3 and not desk.posts


@pytest.mark.parametrize(
    ("name", "does", "args"),
    [
        ("tars", ITS_OWN, {"action": "set", "query": "wake.threshold", "value": "0.1"}),
        ("home_assistant", ITS_OWN, {"action": "call", "service": "lock.unlock"}),
        ("skills", ITS_OWN, {"action": "save", "name": "x"}),
        ("memory", ITS_OWN, {"action": "forget", "id": 3}),
        ("memory", ITS_OWN, "not an object"),
        ("something_new", CHANGES, {}),
        ("something_newer", ITS_OWN, {"action": "list"}),
    ],
)
def test_anything_else_waits_for_a_tap_and_is_then_done_once(desk, name, does, args):
    done = []
    assert call(desk, a_tool(name, does, done), args) == ASKED
    time.sleep(0.1)
    assert done == []
    ((chat, text, buttons),) = desk.posts
    assert chat == 7 and [b[0] for b in buttons] == ["Approve", "Cancel"]
    assert name.replace("_", " ") in text and "do it" in text
    assert desk.tap() and desk.tap()
    until(lambda: len(desk.posts) == 2)
    assert len(done) == 1 and desk.posts[1][1] == "done"
    assert done[0][1].approve is None and done[0][1].by_web  # still never at home
    assert not list(desk.folder.iterdir())


def test_cancel_and_waiting_too_long_do_nothing(desk, monkeypatch):
    done = []
    call(desk, a_tool("skills", ITS_OWN, done), {"action": "delete"})
    desk.tap("no")
    until(lambda: len(desk.posts) == 2)
    desk.asks._wait_s = 0.1
    call(desk, a_tool("skills", ITS_OWN, done), {"action": "delete"})
    until(lambda: len(desk.posts) == 4)
    assert done == [] and "too long" in desk.posts[3][1]
    desk.tap(n=2)  # late
    assert done == [] and "isn't waiting" in desk.bot.sent[-1][1]


def test_only_the_chat_that_was_asked_may_answer(desk):
    done = []
    call(desk, a_tool("skills", ITS_OWN, done), {"action": "delete"})
    assert desk.tap(chat=8)
    time.sleep(0.15)
    assert done == [] and desk.bot.dropped == []
    desk.tap()
    until(lambda: done)


def test_a_made_up_tap_reaches_nothing(desk):
    for data in (f"{TAP}yes:../../x", f"{TAP}yes:{'a' * 32}", f"{TAP}maybe:{'a' * 32}", TAP):
        assert desk.take({"callback_query": {"id": "1", "data": data, "from": {"id": 7}}})
    assert not desk.folder.exists() or not list(desk.folder.iterdir())
    assert not desk.take({"callback_query": {"id": "1", "data": "update:abc"}}) and not desk.take({"message": {}})


def test_with_no_chat_it_is_refused(desk):
    done = []
    assert call(desk, a_tool("skills", ITS_OWN, done), {"action": "save"}, person="guest") == NO_CHAT
    assert call(desk, a_tool("skills", ITS_OWN, done), {"action": "save"}, person=None) == NO_CHAT
    assert call(desk, a_tool("remind", COLLECTS, done), {"for": "Sam"}, person="guest") == NO_CHAT
    assert done == [] and desk.posts == []


def test_reminders_are_collected_as_ever(desk):
    done = []
    assert call(desk, a_tool("remind", COLLECTS, done), {"for": "Sam"}) == "done"
    assert call(desk, a_tool("remind", COLLECTS, done), {"for": None}, person="guest") == "done"
    assert for_other("Sam", "robin") and not for_other("Robin ", "robin") and not for_other(None, "robin")


def test_a_draft_thrown_away_asks_nothing_and_so_does_one_that_read_the_web(desk):
    done = []
    tool = a_tool("skills", ITS_OWN, done)
    assert call(desk, tool, {"action": "save"}, may_act=lambda s: False).startswith("error: the request was withdrawn")
    assert call(desk, tool, {"action": "save"}, read_the_web=True).startswith("refused")
    assert desk.posts == []


def test_what_is_approved_is_shown_whole_or_refused(desk):
    done = []
    args = {"action": "call", "data": {"a": [1, 2]}, "query": None, "service": "x.y"}
    assert shown("home_assistant", args) == 'home assistant\naction: call\ndata: {"a": [1, 2]}\nservice: x.y'
    assert call(desk, a_tool("skills", ITS_OWN, done), {"action": "save", "instructions": "x" * 4000}) == TOO_LONG
    assert desk.posts == []


def test_no_more_than_a_few_wait_at_once_and_a_chat_out_of_reach_is_said(desk, monkeypatch):
    done = []
    tool = a_tool("skills", ITS_OWN, done)
    for _ in range(approvals.MAX_WAITING):
        assert call(desk, tool, {"action": "save"}) == ASKED
    assert call(desk, tool, {"action": "save"}).startswith("refused: too many")
    desk.tap("no")
    until(lambda: desk.asks._waiting < approvals.MAX_WAITING)

    def down(*_):
        raise OSError("down")

    monkeypatch.setattr(desk.asks, "_post", down)
    assert call(desk, tool, {"action": "save"}).startswith("error: couldn't reach")
    assert desk.asks._waiting == approvals.MAX_WAITING - 1


def test_what_fails_is_said_and_what_waited_is_forgotten_at_a_restart(desk, tmp_path):
    def fails(args, turn):
        raise RuntimeError("no")

    call(desk, Tool({"name": "skills"}, ITS_OWN, fails), {"action": "save"})
    desk.tap()
    until(lambda: len(desk.posts) == 2)
    assert desk.posts[1][1].startswith("That failed")
    call(desk, a_tool("skills", ITS_OWN, []), {"action": "save"})
    Approvals(tmp_path, {}.get, lambda *a: None)
    assert not list(desk.folder.iterdir())
    desk.tap(n=2)
    assert "isn't waiting" in desk.bot.sent[-1][1]


def test_every_tool_tars_has_is_named_as_it_is():
    """SAFE goes by names: one that's renamed would quietly start waiting, and these are the ones meant to."""
    from voice_assistant import feeds, ha_tool, llm, memory, myself, skills

    specs = [feeds.FEEDS_TOOL, ha_tool.TOOL, memory.MEMORY_TOOL, myself.TOOL, skills.SKILLS_TOOL, llm.SEND_TOOL,
             *llm.REMIND_TOOLS]  # fmt: skip
    named = {s["name"]: s for s in specs}
    for name, actions in approvals.SAFE.items():
        if name in named and actions:
            assert actions <= set(named[name]["parameters"]["properties"]["action"]["enum"])
    assert set(approvals.SAFE) - set(named) == {"home", "read_page", "browse"}
    assert not safe(a_tool("remind_all", CHANGES, []), {})


def test_a_tap_on_what_expired_with_nobody_left_waiting_is_said_not_taken(desk):
    nonce = "a" * 32
    desk.folder.mkdir(exist_ok=True)
    (desk.folder / f"{nonce}.json").write_text('{"chat": 7, "until": 1}')
    assert desk.take({"callback_query": {"id": "1", "data": f"{TAP}yes:{nonce}", "from": {"id": 7}, "message": {}}})
    assert "isn't waiting" in desk.bot.sent[0][1] and not (desk.folder / f"{nonce}.answer").exists()
