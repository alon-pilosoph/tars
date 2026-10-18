"""TARS on Telegram: who it answers, what it writes back, what it sends them, and where reminders set there go."""

import json
import time

import httpx
import pytest

from voice_assistant.config import LLMConfig
from voice_assistant.conversations import FILE, LIST, NOTE, OTHER, SentItem
from voice_assistant.llm import SEARCHING, THINKING, OpenAIChat
from voice_assistant.reminders import (
    ADD,
    MESSAGE,
    REMINDER,
    SAID,
    SCHEDULED,
    TELEGRAM,
    TIMER,
    VOICE,
    WEB,
    Change,
    NewReminder,
    Reminders,
    ReminderTools,
)
from voice_assistant.telegram import (
    CHAT_REMIND_TOOLS,
    COMMANDS,
    FRESH,
    HELLO,
    ONLY_TEXT,
    SORRY,
    STALE_S,
    Bot,
    TelegramError,
    TelegramTars,
    channel_rules,
    split_text,
    tidy,
)

from .conftest import fake_openai_chat

ALON, STACEY = 111, 222
PEOPLE = {"Alon": ALON}


class FakeBot:
    """Telegram, as far as TARS can tell: what it was sent, by chat. `failing`: how many sends fail first."""

    def __init__(self, failing: int = 0):
        self.messages: list[tuple[int, str]] = []
        self.files: list[tuple[int, str, bytes, str, str]] = []
        self.failing = failing

    def _maybe_fail(self):
        if self.failing:
            self.failing -= 1
            raise TelegramError("sendMessage: ConnectError")

    def send(self, chat_id, text):
        self._maybe_fail()
        self.messages.append((chat_id, text))

    def send_file(self, chat_id, name, data, mime, caption=""):
        self._maybe_fail()
        self.files.append((chat_id, name, data, mime, caption))

    def typing(self, chat_id):
        pass

    def me(self):
        return {"username": "hey_tars_bot"}

    def set_commands(self, commands):
        self.commands = commands

    def said(self, chat_id=ALON) -> list[str]:
        return [text for to, text in self.messages if to == chat_id]


class ScriptedBrain:
    """Replies with the next of `replies`; one that's an exception is raised instead. `sent` and `changes`, what
    the next reply does, are set by the test."""

    answered_by, quick_service = "quick", "Groq"

    def __init__(self, replies=()):
        self.replies, self.asked, self.forgotten = list(replies), [], 0
        self.remembered, self.started_over = [], 0
        self.sent, self.changes = [], []
        self.next_sent, self.next_changes = [], []

    def stream_reply(self, text):
        self.asked.append(text)
        self.sent, self.changes = self.next_sent, self.next_changes
        self.next_sent, self.next_changes = [], []
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return iter([reply])

    def forget_last(self):
        self.forgotten += 1

    def remember_said(self, text):
        self.remembered.append(text)

    def start_over(self):
        self.started_over += 1


def message(text, chat_id=ALON, sent_at=None, kind="private", update_id=1):
    chat = {"id": chat_id, "type": kind}
    return {
        "update_id": update_id,
        "message": {"chat": chat, "from": {"first_name": "X"}, "date": sent_at or time.time(), "text": text},
    }


def tars(bot, brains, convos=None, tools=None, people=None, **kw) -> TelegramTars:
    """A TelegramTars whose people get the given brains, in order."""
    made = iter(brains)
    kw.setdefault("sleep", lambda s: None)
    return TelegramTars(bot, people or PEOPLE, lambda: next(made), convos, tools, typing=False, **kw)


def test_a_message_from_someone_in_people_is_answered_with_their_name_on_it():
    bot, brain = FakeBot(), ScriptedBrain(["Nine o'clock."])
    tars(bot, [brain]).handle(message("what time is it?"))
    assert brain.asked == ["[Speaker: Alon] what time is it?"]
    assert bot.said() == ["Nine o'clock."]


@pytest.mark.parametrize(
    "update",
    [
        message("hi", chat_id=999),  # a stranger who found the bot
        message("hi", kind="group"),  # a group someone added it to
        {"update_id": 1, "edited_message": {"text": "hi"}},
    ],
)
def test_anyone_else_and_anything_else_is_ignored_without_a_word(update):
    bot, brain = FakeBot(), ScriptedBrain()
    tars(bot, [brain]).handle(update)
    assert bot.messages == [] and brain.asked == []


def test_a_stranger_is_logged_once_with_their_chat_id_so_they_can_be_added(capsys):
    t = tars(FakeBot(), [ScriptedBrain()])
    t.handle(message("hi", chat_id=999))
    t.handle(message("hello?", chat_id=999))
    assert capsys.readouterr().out.count("chat 999") == 1


def test_each_person_has_their_own_conversation():
    bot = FakeBot()
    alons, staceys = ScriptedBrain(["A."]), ScriptedBrain(["S."])
    t = tars(bot, [alons, staceys], people={"Alon": ALON, "Stacey": STACEY})
    t.handle(message("hi", chat_id=STACEY))
    assert staceys.asked == ["[Speaker: Stacey] hi"] and alons.asked == []
    assert bot.said(STACEY) == ["S."]


def test_start_says_hello_without_asking_the_brain():
    bot, brain = FakeBot(), ScriptedBrain()
    tars(bot, [brain]).handle(message("/start"))
    assert bot.said() == [HELLO.format(name="Alon")] and brain.asked == []


def test_new_starts_a_new_conversation(convos):
    # A chat is one long thread in Telegram: /new is how to change the subject without waiting for it to go quiet.
    bot, brain = FakeBot(), ScriptedBrain(["A.", "B."])
    t = tars(bot, [brain], convos)
    t.handle(message("one"))
    t.handle(message("/new"))
    t.handle(message("two"))
    assert brain.started_over == 1 and brain.asked == ["[Speaker: Alon] one", "[Speaker: Alon] two"]
    assert bot.said() == ["A.", FRESH, "B."]
    first, second = sorted(convos.conversations(), key=lambda c: c["id"])
    assert [t["text"] for t in first["turns"]] == ["one", "A."] and first["ended"] is not None
    assert [t["text"] for t in second["turns"]] == ["two", "B."]


def test_the_commands_menu_is_set_and_a_failure_to_is_no_matter(monkeypatch):
    bot = FakeBot()
    bot.updates = lambda offset: (_ for _ in ()).throw(KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        tars(bot, [ScriptedBrain()]).run_forever()
    assert bot.commands == COMMANDS

    def refuse(commands):
        raise TelegramError("setMyCommands: Bad Request", 400)

    bot.set_commands = refuse
    with pytest.raises(KeyboardInterrupt):  # still went on to read messages
        tars(bot, [ScriptedBrain()]).run_forever()


def test_a_photo_or_voice_note_gets_a_plain_answer():
    bot = FakeBot()
    update = message("")
    del update["message"]["text"]
    tars(bot, [ScriptedBrain()]).handle(update)
    assert bot.said() == [ONLY_TEXT]


def test_a_message_that_came_while_tars_was_off_is_not_answered_as_if_new():
    # "Remind me in ten minutes", read an hour late, would be set for the wrong time.
    bot, brain = FakeBot(), ScriptedBrain(["Set."])
    tars(bot, [brain]).handle(message("remind me in ten minutes", sent_at=time.time() - STALE_S - 60))
    assert brain.asked == [] and "I was off when this came in" in bot.said()[0]


def test_when_the_brain_fails_they_hear_so_and_it_forgets_the_question(convos):
    bot, brain = FakeBot(), ScriptedBrain([RuntimeError("down")])
    tars(bot, [brain], convos).handle(message("hi"))
    assert bot.said() == [SORRY] and brain.forgotten == 1
    (conversation,) = convos.conversations()
    person, failed = conversation["turns"]
    assert person["text"] == "hi" and failed["failed_at"] == "llm" and "down" in failed["error"]


def test_a_reply_that_cant_be_delivered_does_nothing_it_asked_for(convos):
    # As by voice: what a reply does happens only once it's reached them, so a reminder they never saw confirmed
    # isn't set behind their back.
    bot, brain = FakeBot(failing=99), ScriptedBrain(["Set for nine."])
    tools = ReminderTools(Reminders(convos.store))
    brain.next_changes = [Change(ADD, NewReminder(REMINDER, "call mum", due=time.time() + 600))]
    tars(bot, [brain], convos, tools).handle(message("remind me to call mum"))
    assert tools.reminders.active() == [] and brain.forgotten == 1
    assert convos.conversations()[0]["turns"][-1]["failed_at"] == OTHER


def test_a_reply_is_sent_once_telegram_answers_again():
    bot = FakeBot(failing=1)
    tars(bot, [ScriptedBrain(["Nine."])]).handle(message("time?"))
    assert bot.said() == ["Nine."]


def test_the_chat_is_kept_for_the_web_ui_as_one_conversation_on_telegram(convos):
    bot = FakeBot()
    t = tars(bot, [ScriptedBrain(["Nine.", "Ten."])], convos)
    t.handle(message("time?"))
    t.handle(message("and later?"))
    (conversation,) = convos.conversations()
    assert conversation["via"] == TELEGRAM
    texts = [(turn["role"], turn["text"]) for turn in conversation["turns"]]
    assert texts == [("person", "time?"), ("tars", "Nine."), ("person", "and later?"), ("tars", "Ten.")]
    assert conversation["turns"][1]["answered_by"] == "quick"


def test_a_chat_that_went_quiet_starts_a_new_conversation(convos):
    now = [1_800_000_000.0]
    t = tars(FakeBot(), [ScriptedBrain(["A.", "B."])], convos, memory_s=600, clock=lambda: now[0])
    t.handle(message("one", sent_at=now[0]))
    now[0] += 601
    t.handle(message("two", sent_at=now[0]))
    assert len(convos.conversations()) == 2


def test_a_conversation_deleted_in_the_web_ui_is_not_written_to_again(convos):
    t = tars(FakeBot(), [ScriptedBrain(["A.", "B."])], convos)
    t.handle(message("one"))
    convos.delete(convos.conversations()[0]["id"])
    t.handle(message("two"))
    (conversation,) = convos.conversations()
    assert [turn["text"] for turn in conversation["turns"]] == ["two", "B."]


def test_what_a_reply_sends_is_posted_in_the_chat_and_kept_on_the_tars_page(convos):
    bot, brain = FakeBot(), ScriptedBrain(["It's here."])
    brain.next_sent = [
        SentItem(LIST, "Shopping", entries=["eggs", "milk"]),
        SentItem(FILE, "Dinner", file_name="dinner.ics", file_bytes=b"BEGIN:VCALENDAR", mime="text/calendar"),
    ]
    tars(bot, [brain], convos).handle(message("send me the shopping list and an invite"))
    assert bot.said() == ["It's here.", "Shopping\n• eggs\n• milk"]
    assert [(f[1], f[2], f[4]) for f in bot.files] == [("dinner.ics", b"BEGIN:VCALENDAR", "Dinner")]
    items = convos.store.rows("SELECT title, for_name, delivered FROM items ORDER BY id")
    assert [(i["title"], i["for_name"]) for i in items] == [("Shopping", "Alon"), ("Dinner", "Alon")]
    assert all(i["delivered"] for i in items)
    assert convos.undelivered(since=0) == []  # not posted again by the delivery loop


def test_what_tars_sends_at_home_for_someone_comes_to_their_chat_once(convos):
    bot = FakeBot()
    t = tars(bot, [ScriptedBrain()], convos)
    convos.add_item(None, None, SentItem(NOTE, "Pasta", body="Boil water."), for_name="alon")
    convos.add_item(None, None, SentItem(NOTE, "For the house", scope="household", body="x"))
    convos.add_item(None, None, SentItem(NOTE, "Stacey's", body="x"), for_name="stacey")  # no chat
    t.deliver()
    t.deliver()
    assert bot.said() == ["Pasta\n\nBoil water."] and bot.messages == [(ALON, "Pasta\n\nBoil water.")]


def test_a_file_sent_at_home_is_posted_as_a_file(convos):
    bot = FakeBot()
    item = SentItem(FILE, "Trip", file_name="trip.md", file_bytes=b"# Rome", mime="text/markdown")
    convos.add_item(None, None, item, for_name="Alon")
    tars(bot, [ScriptedBrain()], convos).deliver()
    assert [(f[1], f[2]) for f in bot.files] == [("trip.md", b"# Rome")]


def test_a_list_posted_later_leaves_out_what_was_ticked_off(convos):
    bot = FakeBot()
    item_id = convos.add_item(None, None, SentItem(LIST, "Shopping", entries=["eggs", "milk"]), for_name="Alon")
    convos.tick(item_id, 0, True)
    tars(bot, [ScriptedBrain()], convos).deliver()
    assert bot.said() == ["Shopping\n• milk"]


def test_old_items_and_turning_send_items_off_post_nothing(convos):
    bot = FakeBot()
    convos.add_item(None, None, SentItem(NOTE, "Old", body="x"), for_name="Alon", ts=time.time() - 2 * 3600)
    tars(bot, [ScriptedBrain()], convos).deliver()
    convos.add_item(None, None, SentItem(NOTE, "New", body="x"), for_name="Alon")
    tars(bot, [ScriptedBrain()], convos, send_items=False).deliver()
    assert bot.messages == []


def test_an_item_telegram_wont_take_is_tried_again_next_time(convos):
    bot = FakeBot(failing=1)
    t = tars(bot, [ScriptedBrain()], convos)
    convos.add_item(None, None, SentItem(NOTE, "Pasta", body="Boil water."), for_name="Alon")
    with pytest.raises(TelegramError):
        t.deliver()
    t.deliver()
    assert bot.said() == ["Pasta\n\nBoil water."]


def tools_with(convos):
    return ReminderTools(Reminders(convos.store))


@pytest.mark.parametrize(
    "for_name, channel",
    [
        (None, TELEGRAM),  # "set a timer": for whoever set it, who's on Telegram
        ("alon", TELEGRAM),  # "remind me"
        ("Stacey", VOICE),  # no chat: said aloud at home
    ],
)
def test_a_reminder_set_on_telegram_is_said_where_its_person_is(convos, for_name, channel):
    bot, brain = FakeBot(), ScriptedBrain(["Set."])
    tools = tools_with(convos)
    brain.next_changes = [Change(ADD, NewReminder(REMINDER, "call mum", for_name, due=time.time() + 600))]
    tars(bot, [brain], convos, tools).handle(message("remind ..."))
    (r,) = tools.reminders.active()
    assert (r["channel"], r["set_via"], r["from_name"]) == (channel, TELEGRAM, "Alon")
    assert r["conversation_id"] == convos.conversations()[0]["id"]


def test_a_message_that_waits_until_someone_is_back_is_always_said_aloud(convos):
    t = tars(FakeBot(), [ScriptedBrain()], convos, tools_with(convos))
    change = Change(ADD, NewReminder(MESSAGE, "the plumber called", "alon", due=None))
    assert t.channel(change, "Alon") == VOICE


def test_a_reminder_due_on_telegram_is_posted_once_and_never_said_aloud(convos):
    bot = FakeBot()
    tools = tools_with(convos)
    reminders = tools.reminders
    now = time.time()
    new = NewReminder(TIMER, "pasta", from_name="Alon", due=now + 60)
    rid = reminders.add(new, TELEGRAM, channel=TELEGRAM, now=now)
    brain = ScriptedBrain()
    t = tars(bot, [brain], convos, tools, clock=lambda: now + 61)
    assert reminders.due(now + 61) == []  # the assistant at home doesn't say it
    t.deliver()
    t.deliver()
    assert bot.said() == ["Your pasta timer is done."]
    assert reminders.get(rid)["status"] == SAID
    assert brain.remembered == ["Your pasta timer is done."]  # so "again in five minutes" knows which
    (conversation,) = convos.conversations()  # kept, so the web UI shows it was sent
    assert conversation["turns"][0]["text"] == "Your pasta timer is done."


def test_a_reminder_posted_late_says_when_it_was_due(convos):
    bot, tools, now = FakeBot(), tools_with(convos), time.time()
    tools.reminders.add(NewReminder(REMINDER, "call mum", "alon", due=now + 60), TELEGRAM, channel=TELEGRAM, now=now)
    tars(bot, [ScriptedBrain()], convos, tools, clock=lambda: now + 3600).deliver()
    assert bot.said()[0].startswith("Alon, a reminder: call mum. This was due at")


def test_a_reminder_for_someone_whose_chat_was_removed_is_said_aloud_instead(convos):
    bot, tools, now = FakeBot(), tools_with(convos), time.time()
    rid = tools.reminders.add(
        NewReminder(REMINDER, "call mum", "stacey", due=now + 60), TELEGRAM, channel=TELEGRAM, now=now
    )
    tars(bot, [ScriptedBrain()], convos, tools, clock=lambda: now + 61).deliver()
    assert bot.messages == [] and tools.reminders.get(rid)["channel"] == VOICE
    assert [r["id"] for r in tools.reminders.due(now + 61)] == [rid]


def test_got_it_on_telegram_acknowledges_a_reminder_said_aloud(convos):
    bot, tools, now = FakeBot(), tools_with(convos), time.time()
    rid = tools.reminders.add(NewReminder(REMINDER, "take the pills", "alon", due=now - 1), WEB, now=now - 2)
    tools.reminders.said(rid)
    tars(bot, [ScriptedBrain([f"<ack {rid}> Noted."])], convos, tools).handle(message("I took the pills"))
    r = tools.reminders.get(rid)
    assert (r["status"], r["acked_by"], r["acked_via"]) == ("acknowledged", "Alon", TELEGRAM)
    assert bot.said() == ["Noted."]


def test_the_real_brain_sets_a_reminder_and_sends_a_note_from_a_chat(convos):
    # OpenAIChat over a fake OpenAI: the tools and the rules for writing, end to end.
    def calls(messages):
        if messages[-1].get("role") != "user":
            return []
        remind = {
            "kind": "reminder", "text": "call mum", "for": "Alon", "in_minutes": 30, "day": None, "time": None,
            "when_back": False, "wait_for_ack": True,
        }  # fmt: skip
        note = {
            "kind": NOTE, "title": "Mum", "for": "person", "url": None, "site": None, "description": None,
            "body": "Call her.", "entries": None, "file_name": None, "file_text": None,
        }  # fmt: skip
        return [("remind", remind), ("send", note)]

    client = fake_openai_chat(lambda m: "Done, and it's here." if m[-1].get("type") else "", calls)
    tools = tools_with(convos)
    brain = OpenAIChat(client, LLMConfig(), tools, channel_rules=channel_rules(PEOPLE))
    bot = FakeBot()
    tars(bot, [brain], convos, tools).handle(message("remind me to call mum in half an hour and send me a note"))
    assert bot.said() == ["Done, and it's here.", "Mum\n\nCall her."]
    (r,) = tools.reminders.active()
    assert (r["text"], r["channel"], r["status"]) == ("call mum", TELEGRAM, SCHEDULED)
    instructions = client.requests[0]["instructions"]
    assert instructions.index("on Telegram, in writing") > instructions.index("send tool")  # last, so it wins
    assert "(Alon)" in instructions


def test_tidy_takes_out_the_lines_said_while_waiting():
    assert tidy(f"{SEARCHING} It's 24 degrees.") == "It's 24 degrees."
    assert tidy(f"Timer set. {THINKING} Rome first.") == "Timer set. Rome first."
    assert tidy("Line one.\n\nLine two.") == "Line one.\n\nLine two."


def test_long_text_is_split_where_it_reads_well():
    text = "a" * 30 + "\n\n" + "b" * 30
    assert split_text(text, limit=40) == ["a" * 30, "b" * 30]
    assert split_text("word " * 20, limit=22) == ["word word word word", *["word word word word"] * 4]
    assert split_text("x" * 50, limit=20) == ["x" * 20, "x" * 20, "x" * 10]
    assert all(len(p) <= 4096 for p in split_text("y" * 10_000))


def bot_answering(handler):
    return Bot("123:SECRET", httpx.Client(transport=httpx.MockTransport(handler)))


def test_the_bot_calls_telegram_and_returns_the_result():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    bot_answering(handler).send(ALON, "hi")
    assert seen == [("/bot123:SECRET/sendMessage", {"chat_id": ALON, "text": "hi"})]


def test_telegram_saying_no_is_an_error_with_its_reason_and_wait():
    def handler(request):
        body = {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 7}}
        return httpx.Response(429, json=body)

    with pytest.raises(TelegramError) as e:
        bot_answering(handler).send(ALON, "hi")
    assert (e.value.status, e.value.retry_after) == (429, 7) and "Too Many Requests" in str(e.value)


def test_a_network_error_never_puts_the_token_in_the_logs():
    def handler(request):
        raise httpx.ConnectError(f"can't reach {request.url}")

    with pytest.raises(TelegramError) as e:
        bot_answering(handler).me()
    assert "SECRET" not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


class BlockedBot(FakeBot):
    def send(self, chat_id, text):
        raise TelegramError("sendMessage: Forbidden: bot was blocked by the user", 403)

    send_file = send


def test_a_chat_that_blocked_the_bot_gets_its_reminders_aloud_and_isnt_tried_forever(convos):
    tools, now = tools_with(convos), time.time()
    rid = tools.reminders.add(
        NewReminder(REMINDER, "call mum", "alon", due=now + 60), TELEGRAM, channel=TELEGRAM, now=now
    )
    convos.add_item(None, None, SentItem(NOTE, "Pasta", body="x"), for_name="Alon")
    t = tars(BlockedBot(), [ScriptedBrain()], convos, tools, clock=lambda: now + 61)
    t.deliver()  # doesn't raise: nothing to try again
    assert tools.reminders.get(rid)["channel"] == VOICE and convos.undelivered(since=0) == []


def test_a_chat_can_have_a_message_said_aloud_at_home_for_everyone(convos):
    # "Tell the house dinner's ready": the real brain over a fake OpenAI, with the chat's own remind tool.
    def calls(messages):
        if messages[-1].get("role") != "user":
            return []
        args = {
            "kind": "message", "text": "dinner's ready", "for": "everyone", "in_minutes": 0, "day": None,
            "time": None, "when_back": False, "wait_for_ack": True, "aloud": True,
        }  # fmt: skip
        return [("remind", args)]

    client = fake_openai_chat(lambda m: "I'll say it at home." if m[-1].get("type") else "", calls)
    tools = tools_with(convos)
    brain = OpenAIChat(client, LLMConfig(), tools, channel_rules=channel_rules(PEOPLE), remind_tools=CHAT_REMIND_TOOLS)
    tars(FakeBot(), [brain], convos, tools).handle(message("tell the house dinner's ready"))
    (r,) = tools.reminders.active()
    assert (r["channel"], r["set_via"], r["for_name"], r["from_name"]) == (VOICE, TELEGRAM, "everyone", "Alon")
    assert [d["id"] for d in tools.reminders.due(time.time() + 1)] == [r["id"]]  # said at home at once
    (remind,) = [t for t in client.requests[0]["tools"] if t.get("name") == "remind"]
    assert "aloud" in remind["parameters"]["required"]
    assert "aloud at home" in client.requests[1]["input"][-1]["output"]


def test_aloud_wins_even_for_someone_with_a_chat(convos):
    t = tars(FakeBot(), [ScriptedBrain()], convos, tools_with(convos))
    soon = time.time() + 60
    assert t.channel(Change(ADD, NewReminder(REMINDER, "x", "alon", due=soon), aloud=True), "Alon") == VOICE
    assert t.channel(Change(ADD, NewReminder(REMINDER, "x", "alon", due=soon)), "Alon") == TELEGRAM


def test_the_voice_brains_remind_tool_has_no_aloud_choice():
    from voice_assistant.llm import REMIND_TOOLS

    assert "aloud" not in REMIND_TOOLS[0]["parameters"]["properties"]
    assert CHAT_REMIND_TOOLS[1:] == REMIND_TOOLS[1:]


def said_at_home(convos, needs_ack=True):
    """A message set in Alon's chat, said aloud at home just now."""
    tools, now = tools_with(convos), time.time()
    new = NewReminder(MESSAGE, "dinner's ready", "everyone", "Alon", due=now, needs_ack=needs_ack)
    rid = tools.reminders.add(new, TELEGRAM, now=now)
    tools.reminders.due(now + 1)
    tools.reminders.said(rid, now=now + 1)
    return tools, rid


def test_the_chat_hears_when_its_message_was_said_at_home_and_who_got_it(convos):
    bot = FakeBot()
    tools, rid = said_at_home(convos)
    t = tars(bot, [ScriptedBrain()], convos, tools)
    t.deliver()
    t.deliver()  # once only
    tools.reminders.ack(rid, "stacey", VOICE)
    t.deliver()
    first, second = bot.said()
    assert first.startswith("Said at home at") and "Everyone, a message from Alon: dinner's ready." in first
    assert "Waiting for someone to say they got it." in first
    assert second.startswith("Stacey said they got it at")
    assert len(bot.said()) == 2


def test_the_chat_hears_when_nobody_got_it(convos):
    bot = FakeBot()
    tools, rid = said_at_home(convos)
    convos.store.write("UPDATE reminders SET status='missed', tries=4 WHERE id=?", (rid,))
    tars(bot, [ScriptedBrain()], convos, tools).deliver()
    assert bot.said() == [
        "Nobody said they got it (said 4 times at home): “Everyone, a message from Alon: dinner's ready.”"
    ]


def test_one_said_once_is_reported_once_and_kept_in_the_chat(convos):
    bot = FakeBot()
    tools, _ = said_at_home(convos, needs_ack=False)
    tars(bot, [ScriptedBrain()], convos, tools).deliver()
    (said,) = bot.said()
    assert said.startswith("Said at home at") and "Waiting" not in said
    assert convos.conversations()[0]["turns"][0]["text"] == said


def test_reminders_set_at_home_are_never_reported_to_a_chat(convos):
    bot, tools, now = FakeBot(), tools_with(convos), time.time()
    rid = tools.reminders.add(NewReminder(REMINDER, "x", "alon", "alon", due=now), VOICE, now=now)
    tools.reminders.said(rid, now=now + 1)
    tars(bot, [ScriptedBrain()], convos, tools).deliver()
    assert bot.messages == []


def test_chats_go_to_openai_on_the_standard_tier_unless_told_otherwise(tmp_path):
    import dataclasses
    from pathlib import Path

    from voice_assistant.__main__ import chat_brain_config
    from voice_assistant.config import load_config

    # As shipped: Qwen on Groq and Cerebras, Luna at "fast".
    cfg = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    llm = chat_brain_config(cfg, has_openai=True)
    assert (llm.groq_model, llm.cerebras_model, llm.service_tier, llm.reasoning_effort) == ("", "", "", "low")
    assert llm.model == cfg.llm.model and llm.send == cfg.llm.send
    assert chat_brain_config(cfg, has_openai=False).groq_model == cfg.llm.groq_model  # no OpenAI: Qwen answers
    quick = dataclasses.replace(cfg, telegram=dataclasses.replace(cfg.telegram, brain="quick"))
    assert chat_brain_config(quick, has_openai=True).service_tier == cfg.llm.service_tier
    # A written reply often comes back later than a spoken one: the chat remembers for longer, whoever answers.
    assert cfg.telegram.memory_minutes > cfg.llm.memory_minutes
    for has_openai in (True, False):
        assert chat_brain_config(cfg, has_openai).memory_minutes == cfg.telegram.memory_minutes
