"""The brain: skipping overheard chatter, memory, the send tool, failures, and Qwen handing turns to OpenAI."""

import json
import threading
import time
import types
from datetime import datetime

import httpx
import pytest
from openai import APIConnectionError, OpenAIError, RateLimitError

from voice_assistant import llm
from voice_assistant.config import LLMConfig
from voice_assistant.conversations import SentItem
from voice_assistant.events import EventLog
from voice_assistant.llm import (
    LOOK_UP,
    MAX_TOOL_ROUNDS,
    SKIP,
    OpenAIChat,
    QuickChat,
    QuickService,
    ReplyFailed,
    split_skip,
)
from voice_assistant.reminders import MESSAGE, TIMER, WEB, NewReminder, Reminders, ReminderTools

from .conftest import fake_openai_chat, raising


@pytest.mark.parametrize(
    "pieces",
    [["<sk", "ip>"], [" <skip>"], ["<skip>", ""], ["<", "s", "k", "i", "p", ">"]],
)
def test_split_skip_detects_skip_even_when_split_across_chunks(pieces):
    skipped, rest = split_skip(pieces)
    assert skipped
    assert "".join(rest) == ""


@pytest.mark.parametrize(
    "pieces",
    [["Sure", ", Paris."], ["<", "b>bold"], ["", " ", "Yes."], ["<ski"], []],
)
def test_split_skip_passes_normal_replies_through_untouched(pieces):
    skipped, rest = split_skip(pieces)
    assert not skipped
    assert "".join(rest) == "".join(pieces)


def test_split_skip_drains_the_stream_so_the_exchange_is_recorded():
    consumed = []

    def stream():
        for piece in [SKIP, " trailing"]:
            consumed.append(piece)
            yield piece

    assert split_skip(stream())[0]
    assert consumed == [SKIP, " trailing"]


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: now[0])
    return now


def seen_messages(brain, text):
    """Ask something and return how many non-system messages the model received."""
    return int("".join(brain.stream_reply(text)))


def test_memory_keeps_the_whole_conversation_until_it_goes_quiet(clock):
    brain = OpenAIChat(fake_openai_chat(lambda m: str(len(m) - 1)), LLMConfig(memory_minutes=10))
    assert seen_messages(brain, "first") == 1
    clock[0] += 60
    assert seen_messages(brain, "second") == 3
    clock[0] += 9 * 60
    assert seen_messages(brain, "after 9 minutes") == 5
    clock[0] += 11 * 60
    assert seen_messages(brain, "after 11 minutes") == 1


def test_forgotten_chatter_does_not_keep_memory_alive(clock):
    brain = OpenAIChat(fake_openai_chat(lambda m: str(len(m) - 1)), LLMConfig(memory_minutes=10))
    seen_messages(brain, "real question")
    clock[0] += 8 * 60
    seen_messages(brain, "overheard")
    brain.forget_last()
    clock[0] += 3 * 60  # 11 minutes since the last real exchange
    assert seen_messages(brain, "next real question") == 1


def asked(client):
    """What the brain sent as the conversation on its last request, by content."""
    return [m["content"] for m in client.requests[-1]["input"]]


def test_a_reply_forgotten_while_still_being_written_leaves_no_trace():
    client = fake_openai_chat(lambda messages: "An answer.")
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False))
    "".join(brain.stream_reply("first"))
    unfinished = brain.stream_reply("second")
    next(unfinished)
    brain.forget_last()
    "".join(brain.stream_reply("third"))
    assert asked(client) == ["first", "An answer.", "third"]


def test_forgetting_twice_forgets_only_once():
    client = fake_openai_chat(lambda messages: "An answer.")
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False))
    "".join(brain.stream_reply("kept"))
    "".join(brain.stream_reply("overheard"))
    brain.forget_last()
    brain.forget_last()
    "".join(brain.stream_reply("next"))
    assert asked(client) == ["kept", "An answer.", "next"]


def test_an_interrupted_reply_ends_with_an_error():
    brain = OpenAIChat(fake_openai_chat(lambda messages: "An answer."), LLMConfig(web_search=False, send=False))
    reply = brain.stream_reply("hi")
    brain.interrupt()
    with pytest.raises(ReplyFailed):
        list(reply)


def test_a_stopped_reply_that_finishes_late_leaves_the_next_one_alone():
    client = fake_openai_chat(lambda messages: "An answer.")
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False))
    stale = brain.stream_reply("half a question")
    next(stale)  # still waiting on the network when it's thrown away
    brain.interrupt()
    brain.forget_last()
    "".join(brain.stream_reply("the whole question"))
    with pytest.raises(ReplyFailed):
        list(stale)
    "".join(brain.stream_reply("next"))
    assert asked(client) == ["the whole question", "An answer.", "next"]


def test_the_humor_setting_goes_into_the_system_prompt():
    captured = {}

    def reply(messages):
        captured["system"] = messages[0]["content"]
        return "ok"

    brain = OpenAIChat(fake_openai_chat(reply), LLMConfig(system_prompt="Humor setting: {humor} percent.", humor=40))
    "".join(brain.stream_reply("hi"))
    assert captured["system"].startswith("Humor setting: 40 percent.")


def test_system_prompt_includes_the_follow_up_protocol():
    captured = {}

    def reply(messages):
        captured["system"] = messages[0]["content"]
        return "ok"

    brain = OpenAIChat(fake_openai_chat(reply), LLMConfig(system_prompt="You are TARS."))
    "".join(brain.stream_reply("hi"))
    assert captured["system"].startswith("You are TARS.")
    assert SKIP in captured["system"]


RECIPE = {
    "kind": "link",
    "title": "Pasta al pomodoro",
    "for": "person",
    "url": "https://example.com/pasta",
    "site": "Example Kitchen",
    "description": "Twenty minutes, five ingredients.",
    "body": None,
    "entries": None,
    "file_name": None,
    "file_text": None,
}


def test_send_runs_the_tool_then_speaks_the_confirmation():
    def calls_for(messages):  # only on the first request: the model sends, then talks
        return [] if messages[-1].get("type") == "function_call_output" else [("send", RECIPE)]

    client = fake_openai_chat(lambda m: "Sent it. It's on the TARS page." if m[-1].get("type") else "", calls_for)
    brain = OpenAIChat(client, LLMConfig())
    assert "".join(brain.stream_reply("send me a pasta recipe")) == "Sent it. It's on the TARS page."
    assert brain.sent == [
        SentItem(
            "link",
            "Pasta al pomodoro",
            "person",
            url="https://example.com/pasta",
            site="Example Kitchen",
            description="Twenty minutes, five ingredients.",
        )
    ]
    first, second = client.requests
    assert {"type": "web_search"} in first["tools"] and first["store"] is False
    assert second["input"][-1] == {"type": "function_call_output", "call_id": "call_1_0", "output": "sent"}
    "".join(brain.stream_reply("thanks"))
    assert client.requests[2]["input"][:3] == [  # the exchange is remembered, its tool calls aren't
        {"role": "user", "content": "send me a pasta recipe"},
        {"role": "assistant", "content": "Sent it. It's on the TARS page."},
        {"role": "user", "content": "thanks"},
    ]


def test_a_bad_send_is_explained_to_the_model_not_sent():
    bad = {**RECIPE, "url": "example.com/pasta"}
    client = fake_openai_chat(lambda m: "Found nothing.", lambda m: [] if m[-1].get("type") else [("send", bad)])
    brain = OpenAIChat(client, LLMConfig())
    "".join(brain.stream_reply("send me a recipe"))
    assert brain.sent == [] and client.requests[1]["input"][-1]["output"].startswith("error:")


@pytest.mark.parametrize(
    "args, ok",
    [
        ({"kind": "link", "title": "Pasta", "for": "person", "url": "http://"}, False),
        ({"kind": "list", "title": "Shopping", "for": "household", "entries": "milk"}, False),
        (["not", "an", "object"], False),
        ({"kind": "note", "title": "Wifi", "for": "household", "body": "**pw** hunter2"}, True),
        ({"kind": "note", "title": "Wifi", "for": "household", "body": " "}, False),
        ({"kind": "list", "title": "Shopping", "for": "household", "entries": ["milk", " ", "eggs"]}, True),
        ({"kind": "list", "title": "Shopping", "for": "household", "entries": []}, False),
        ({"kind": "file", "title": "Trip", "for": "person", "file_name": "trip.md", "file_text": "# Day 1"}, True),
        ({"kind": "file", "title": "Trip", "for": "person", "file_name": "trip.exe", "file_text": "MZ"}, False),
        ({"kind": "link", "title": "", "for": "person", "url": "https://example.com"}, False),
    ],
)
def test_send_accepts_complete_items_and_explains_the_rest(args, ok):
    item, result = llm.check_send(args)
    assert (item is not None) == ok and (result == "sent") == ok
    if item and item.kind == "list":
        assert item.entries == ["milk", "eggs"]
    if item and item.kind == "file":
        assert item.mime == "text/markdown" and item.file_bytes == b"# Day 1"


def test_tools_can_be_turned_off():
    client = fake_openai_chat(lambda m: "ok")
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False))
    "".join(brain.stream_reply("hi"))
    assert "tools" not in client.requests[0] and "send tool" not in client.requests[0]["instructions"]


def test_a_web_search_is_announced_instead_of_silence():
    searching = ["response.web_search_call.in_progress", "response.web_search_call.searching"]
    client = fake_openai_chat(lambda m: "Canberra.", search_for=lambda m: searching)
    brain = OpenAIChat(client, LLMConfig())
    assert "".join(brain.stream_reply("capital of Australia?")) == f"{llm.SEARCHING} Canberra."


def test_a_model_that_keeps_calling_tools_is_made_to_answer_on_the_last_round():
    bad = {**RECIPE, "url": "not a url"}  # it keeps retrying the same broken send
    client = fake_openai_chat(lambda m: "I couldn't find a recipe." if len(m) >= 6 else "", lambda m: [("send", bad)])
    brain = OpenAIChat(client, LLMConfig())
    assert "".join(brain.stream_reply("send me a recipe")) == "I couldn't find a recipe."
    assert len(client.requests) == MAX_TOOL_ROUNDS
    assert "tool_choice" not in client.requests[0] and client.requests[-1]["tool_choice"] == "none"


@pytest.mark.parametrize(
    "event",
    [
        types.SimpleNamespace(type="error", message="overloaded"),
        types.SimpleNamespace(
            type="response.failed",
            response=types.SimpleNamespace(
                error=types.SimpleNamespace(message="server error"), incomplete_details=None
            ),
        ),
        types.SimpleNamespace(
            type="response.incomplete",
            response=types.SimpleNamespace(
                error=None, incomplete_details=types.SimpleNamespace(reason="max_output_tokens")
            ),
        ),
    ],
)
def test_a_failed_reply_is_an_error_not_silence(event):
    client = types.SimpleNamespace(responses=types.SimpleNamespace(create=lambda **kw: [event]))
    brain = OpenAIChat(client, LLMConfig())
    with pytest.raises(ReplyFailed):
        "".join(brain.stream_reply("hi"))


@pytest.mark.parametrize("log_events, typed, sends", [(True, False, True), (False, False, False), (True, True, False)])
def test_tars_only_sends_things_when_they_can_be_kept(tmp_path, log_events, typed, sends):
    from voice_assistant.__main__ import brain_config
    from voice_assistant.config import load_config

    cfg = load_config(tmp_path / "none.toml")
    cfg.learning.log_events = log_events
    assert brain_config(cfg, typed).send is sends


def gone() -> APIConnectionError:
    return APIConnectionError(request=httpx.Request("POST", "https://api.groq.com"))


class FakeStream:
    """A streamed chat completion that can be closed, like the SDK's: reading on after that fails. `finish`: why the
    last piece ended it ("length": the token cap)."""

    def __init__(self, text, finish=None):
        self.pieces, self.closed, self.finish = [text[:3], text[3:]], False, finish

    def __iter__(self):
        for i, piece in enumerate(self.pieces):
            if self.closed:
                raise gone()
            finish = self.finish if i == len(self.pieces) - 1 else None
            delta = types.SimpleNamespace(content=piece)
            yield types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta, finish_reason=finish)])

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def fake_cerebras(reply_for):
    """A Chat Completions client: each request streams `reply_for(messages)`, or raises it if it's an error, or
    returns it if it's already a stream."""
    requests, streams = [], []

    def create(**kw):
        requests.append(kw)
        reply = reply_for(kw["messages"])
        if isinstance(reply, Exception):
            raise reply
        streams.append(reply if hasattr(reply, "close") else FakeStream(reply))
        return streams[-1]

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    client.requests, client.streams = requests, streams
    return client


def cerebras_brain(quick_reply, openai_reply="From OpenAI.", **cfg):
    openai, cerebras = fake_openai_chat(lambda m: openai_reply), fake_cerebras(quick_reply)
    brain = QuickChat(openai, LLMConfig(**cfg), [QuickService("Cerebras", cerebras, "qwen")])
    return brain, openai, cerebras


def seen_by_cerebras(cerebras) -> list[str]:
    """The conversation Cerebras was given on its last request, without the system prompt."""
    return [m["content"] for m in cerebras.requests[-1]["messages"][1:]]


def test_cerebras_answers_and_openai_is_never_asked():
    brain, openai, cerebras = cerebras_brain(lambda m: "Canberra.")
    assert "".join(brain.stream_reply("capital of Australia?")) == "Canberra."
    assert openai.requests == [] and cerebras.requests[0]["model"] == "qwen"
    "".join(brain.stream_reply("and of Canada?"))
    assert seen_by_cerebras(cerebras) == ["capital of Australia?", "Canberra.", "and of Canada?"]


def test_a_turn_that_needs_the_web_is_openais_with_its_tools_and_both_remember_it():
    brain, openai, cerebras = cerebras_brain(lambda m: LOOK_UP if "weather" in m[-1]["content"] else "Noted.")
    assert "".join(brain.stream_reply("what's the weather?")) == "From OpenAI."
    assert {"type": "web_search"} in openai.requests[0]["tools"] and cerebras.streams[0].closed
    "".join(brain.stream_reply("and tomorrow?"))
    # No <look-up> in the conversation.
    assert seen_by_cerebras(cerebras) == ["what's the weather?", "From OpenAI.", "and tomorrow?"]


def test_when_cerebras_fails_openai_answers():
    brain, _, _ = cerebras_brain(lambda m: OpenAIError("429 too many requests"))
    assert "".join(brain.stream_reply("hi")) == "From OpenAI."


def test_when_cerebras_says_nothing_openai_answers():
    brain, _, _ = cerebras_brain(lambda m: "")
    assert "".join(brain.stream_reply("hi")) == "From OpenAI."
    assert brain.answered_by == llm.FALLBACK


def test_after_cerebras_fails_openai_answers_alone_for_a_while(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: now[0])
    down = [True]
    brain, _, cerebras = cerebras_brain(lambda m: OpenAIError("timed out") if down[0] else "Quick.")
    assert "".join(brain.stream_reply("hi")) == "From OpenAI." and len(cerebras.requests) == 1
    down[0] = False
    now[0] += llm.QUICK_COOLDOWN_S - 1
    assert "".join(brain.stream_reply("again")) == "From OpenAI."
    assert len(cerebras.requests) == 1 and brain.answered_by == llm.FALLBACK  # no waiting out its timeout
    now[0] += 2
    assert "".join(brain.stream_reply("and now?")) == "Quick." and brain.answered_by == llm.QUICK


def too_many_requests(retry_after: str | None) -> RateLimitError:
    headers = {"retry-after": retry_after} if retry_after else {}
    response = httpx.Response(429, headers=headers, request=httpx.Request("POST", "https://api.groq.com"))
    return RateLimitError("429 too many requests", response=response, body=None)


def groq_then_cerebras(groq_reply, cerebras_reply=lambda m: "From Cerebras."):
    openai, groq, cerebras = (
        fake_openai_chat(lambda m: "From OpenAI."),
        fake_cerebras(groq_reply),
        fake_cerebras(cerebras_reply),
    )
    quick = [QuickService("Groq", groq, "qwen-g"), QuickService("Cerebras", cerebras, "qwen-c")]
    return QuickChat(openai, LLMConfig(), quick), groq, cerebras


def test_groq_answers_first_and_cerebras_is_never_asked():
    brain, groq, cerebras = groq_then_cerebras(lambda m: "From Groq.")
    assert "".join(brain.stream_reply("hi")) == "From Groq." and brain.answered_by == llm.QUICK
    assert groq.requests[0]["model"] == "qwen-g" and cerebras.requests == []
    assert groq.requests[0]["max_completion_tokens"] == llm.QUICK_MAX_TOKENS


def test_when_groq_is_out_of_tokens_cerebras_answers_until_groq_says_to_come_back(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: now[0])
    limited = [True]
    brain, groq, _ = groq_then_cerebras(lambda m: too_many_requests("30") if limited[0] else "From Groq.")
    assert "".join(brain.stream_reply("hi")) == "From Cerebras." and brain.answered_by == llm.QUICK
    limited[0] = False
    now[0] += 29
    assert "".join(brain.stream_reply("again")) == "From Cerebras." and len(groq.requests) == 1
    now[0] += 2
    assert "".join(brain.stream_reply("and now?")) == "From Groq."
    assert [m["content"] for m in groq.requests[-1]["messages"][1:]] == [
        "hi",
        "From Cerebras.",
        "again",
        "From Cerebras.",
        "and now?",
    ]


def test_a_failed_service_is_skipped_for_as_long_as_it_asks_or_its_limits_window():
    assert llm._cooldown(too_many_requests(None)) == llm.RATE_LIMITED_S
    assert llm._cooldown(OpenAIError("timed out")) == llm.QUICK_COOLDOWN_S
    assert llm._cooldown(too_many_requests("7")) == 7.0


def test_when_groq_and_cerebras_both_fail_openai_answers():
    brain, groq, cerebras = groq_then_cerebras(lambda m: too_many_requests("30"), lambda m: OpenAIError("down"))
    assert "".join(brain.stream_reply("hi")) == "From OpenAI." and brain.answered_by == llm.FALLBACK
    assert len(groq.requests) == len(cerebras.requests) == 1


def test_an_interrupted_reply_doesnt_count_as_cerebras_failing():
    brain, openai, _ = cerebras_brain(lambda m: "A long answer.")
    reply = brain.stream_reply("hi")
    next(reply)  # interrupted mid-reply, while reading Cerebras's stream
    brain.interrupt()
    with pytest.raises(ReplyFailed):
        list(reply)
    assert "".join(brain.stream_reply("hi again")) == "A long answer."
    assert openai.requests == [] and brain.answered_by == llm.QUICK


def test_interrupting_groq_neither_benches_it_nor_asks_cerebras():
    brain, _, cerebras = groq_then_cerebras(lambda m: "From Groq, at length.")
    reply = brain.stream_reply("hi")
    next(reply)
    brain.interrupt()
    with pytest.raises(ReplyFailed):
        list(reply)
    assert cerebras.requests == []
    assert "".join(brain.stream_reply("hi again")) == "From Groq, at length." and brain.quick_service == "Groq"


def test_a_reply_cut_off_at_the_token_cap_is_carried_on_by_openai_without_benching_groq():
    brain, groq, cerebras = groq_then_cerebras(lambda m: FakeStream("From Groq.", finish="length"))
    said = "".join(brain.stream_reply("tell me everything"))
    assert said.startswith("From Groq.") and said.endswith("From OpenAI.")
    assert brain.answered_by == llm.FALLBACK and brain.quick_service == "Groq" and cerebras.requests == []
    "".join(brain.stream_reply("again"))
    assert len(groq.requests) == 2


def test_without_openai_qwen_answers_alone_and_a_failure_is_an_error(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: now[0])
    down = [False]
    groq = fake_cerebras(lambda m: OpenAIError("down") if down[0] else "From Groq.")
    cfg = LLMConfig(web_search=False, send=False, think_effort="")
    brain = QuickChat(None, cfg, [QuickService("Groq", groq, "qwen")])
    OpenAIChat.warm(brain)
    assert "".join(brain.stream_reply("hi")) == "From Groq."
    assert LOOK_UP not in groq.requests[0]["messages"][0]["content"]
    down[0] = True
    with pytest.raises(OpenAIError):
        "".join(brain.stream_reply("again"))


def test_a_service_that_ends_without_a_word_doesnt_win_over_one_that_answers():
    brain, _, _ = groq_then_cerebras(lambda m: "")
    assert "".join(brain.stream_reply("hi")) == "From Cerebras." and brain.quick_service == "Cerebras"


@pytest.mark.parametrize(
    "quick, by", [("Canberra.", llm.QUICK), (LOOK_UP, llm.LOOKED_UP), (OpenAIError("down"), llm.FALLBACK)]
)
def test_the_brain_says_who_answered(quick, by):
    brain, _, _ = cerebras_brain(lambda m: quick)
    "".join(brain.stream_reply("hi"))
    assert brain.answered_by == by
    alone = OpenAIChat(fake_openai_chat(lambda m: "Hi."), LLMConfig(web_search=False, send=False))
    "".join(alone.stream_reply("hi"))
    assert alone.answered_by == llm.OPENAI


class BreaksAfterAWord(FakeStream):
    def __iter__(self):
        yield from list(super().__iter__())[:1]
        raise OpenAIError("connection reset")


def test_cerebras_failing_after_it_started_talking_is_an_error_not_a_second_answer():
    brain, openai, cerebras = cerebras_brain(lambda m: "Canberra is the capital.")
    cerebras.chat.completions.create = lambda **kw: BreaksAfterAWord("Canberra is the capital.")
    reply = brain.stream_reply("capital of Australia?")
    assert next(reply) == "Can"
    with pytest.raises(OpenAIError, match="connection reset"):
        list(reply)
    assert openai.requests == []  # half an answer isn't followed by a different whole one
    assert brain.quick_service == "Cerebras"


def test_an_empty_reply_is_an_error_not_silence():
    brain = OpenAIChat(fake_openai_chat(lambda m: ""), LLMConfig(web_search=False, send=False))
    with pytest.raises(ReplyFailed):
        "".join(brain.stream_reply("hi"))


def test_cerebras_is_only_told_to_hand_off_what_openai_can_do():
    brain, _, cerebras = cerebras_brain(lambda m: "ok", web_search=False, send=False)
    "".join(brain.stream_reply("hi"))
    assert LOOK_UP not in cerebras.requests[0]["messages"][0]["content"]
    brain, _, cerebras = cerebras_brain(lambda m: "ok", web_search=False)
    "".join(brain.stream_reply("hi"))
    system = cerebras.requests[0]["messages"][0]["content"]
    assert LOOK_UP in system and "TARS page" in system and "weather" not in system


def test_an_interrupted_cerebras_reply_stays_interrupted():
    brain, openai, _ = cerebras_brain(lambda m: "A long answer.")
    reply = brain.stream_reply("hi")
    brain.interrupt()
    with pytest.raises(ReplyFailed):
        list(reply)
    assert openai.requests == []


def test_a_hand_off_after_a_sentence_says_the_sentence_then_openai_answers_and_both_are_remembered():
    brain, openai, cerebras = cerebras_brain(lambda m: f"I can't check that. {LOOK_UP}" if len(m) == 2 else "Sure.")
    assert "".join(brain.stream_reply("who won last night?")) == "I can't check that. From OpenAI."
    assert asked(openai) == ["who won last night?", "I can't check that."]  # it carries on from what was said
    "".join(brain.stream_reply("thanks"))
    assert seen_by_cerebras(cerebras) == ["who won last night?", "I can't check that. From OpenAI.", "thanks"]


@pytest.mark.parametrize("text", ["3 < 5, obviously.", "It ends in <", "<look", "A <b>bold</b> claim."])
def test_text_that_only_looks_like_the_start_of_a_hand_off_is_said_whole(text):
    assert "".join(llm._UpTo(iter(text), (LOOK_UP, llm.PONDER))) == text  # one character at a time


def test_both_models_are_told_the_date_and_time_here(monkeypatch):
    now = "It's Tuesday, September 29, 2026, 11:26 PM here (IDT, UTC+03:00)."
    monkeypatch.setattr(llm, "local_time", lambda: now)
    brain, openai, cerebras = cerebras_brain(lambda m: LOOK_UP)
    "".join(brain.stream_reply("what's on tonight?"))
    assert cerebras.requests[0]["messages"][0]["content"].endswith(now)
    assert openai.requests[0]["instructions"].endswith(now)


@pytest.mark.parametrize("send", [True, False])
def test_both_models_are_told_what_they_cant_do_so_they_never_pretend(send):
    brain, openai, cerebras = cerebras_brain(lambda m: LOOK_UP, send=send)
    "".join(brain.stream_reply("set a timer for ten minutes"))
    for instructions in (cerebras.requests[0]["messages"][0]["content"], openai.requests[0]["instructions"]):
        assert f"You can't {llm.CANT_REMIND}, " in instructions and llm.CANT in instructions
        assert "Never say you did something you can't do." in instructions


@pytest.mark.parametrize(
    "reply, ids, said",
    [
        ("<ack>", [], llm.GOT_IT),
        ("  <ack>  ", [], llm.GOT_IT),
        ("<ack 12> Noted.", [12], "Noted."),
        ("<ack 12, 14>Both done.", [12, 14], "Both done."),
        ("A <ack> later on isn't one.", None, "A <ack> later on isn't one."),
        ("<acknowledged>", None, "<acknowledged>"),
        ("Okay.", None, "Okay."),
    ],
)
def test_an_ack_is_read_off_the_start_of_a_reply(reply, ids, said):
    acked, rest = llm.split_ack(iter(reply))  # one character at a time
    assert acked == ids and "".join(rest) == said


def reminder_tools(tmp_path, voices=()):
    return ReminderTools(Reminders(EventLog(tmp_path / "events").store), voices=lambda: voices)


def test_with_reminders_both_models_know_whats_set_and_how_to_ack_and_only_openai_sets_them(tmp_path):
    tools = reminder_tools(tmp_path)
    rid = tools.reminders.add(NewReminder(MESSAGE, "dinner's at eight", "stacey", "alon", due=time.time()), WEB)
    tools.reminders.said(rid)
    openai, cerebras = fake_openai_chat(lambda m: "ok"), fake_cerebras(lambda m: LOOK_UP)
    brain = QuickChat(openai, LLMConfig(), [QuickService("Cerebras", cerebras, "qwen")], reminders=tools)
    "".join(brain.stream_reply("I got the message"))
    quick, full = cerebras.requests[0]["messages"][0]["content"], openai.requests[0]["instructions"]
    for instructions in (quick, full):
        assert llm.ACK_RULES in instructions and llm.CANT in instructions and llm.CANT_REMIND not in instructions
        assert f"[{rid}] " in instructions and "Stacey, a message from Alon: dinner's at eight." in instructions
        assert "<ack N>" in instructions
    assert llm.NEEDS_REMINDING in quick and llm.REMIND_RULES not in quick
    assert llm.REMIND_RULES in full
    assert {t["name"] for t in openai.requests[0]["tools"] if "name" in t} >= {"remind", "cancel_reminder"}


def test_without_reminders_tars_says_it_cant_set_timers(tmp_path):
    brain, openai, cerebras = cerebras_brain(lambda m: LOOK_UP)
    "".join(brain.stream_reply("hi"))
    quick, full = cerebras.requests[0]["messages"][0]["content"], openai.requests[0]["instructions"]
    assert llm.CANT_REMIND in quick and llm.CANT_REMIND in full and "<ack" not in quick + full
    assert llm.NEEDS_REMINDING not in quick and "remind" not in str(openai.requests[0].get("tools"))


def test_a_reminder_list_that_cant_be_read_never_costs_a_reply():
    tools = types.SimpleNamespace(note=raising(OSError("disk")))
    brain = OpenAIChat(fake_openai_chat(lambda m: "Hi."), LLMConfig(), reminders=tools)
    assert "".join(brain.stream_reply("hi")) == "Hi."


REMIND = {"kind": "timer", "text": "pasta", "for": None, "in_minutes": 12, "day": None, "time": None,
          "when_back": False, "wait_for_ack": True}  # fmt: skip


def test_setting_one_by_voice_is_held_until_the_turn_is_kept(tmp_path):
    tools = reminder_tools(tmp_path)

    def calls_for(messages):
        return [] if messages[-1].get("type") == "function_call_output" else [("remind", REMIND)]

    client = fake_openai_chat(lambda m: "Twelve minutes." if m[-1].get("type") else "", calls_for)
    brain = OpenAIChat(client, LLMConfig(), reminders=tools)
    assert "".join(brain.stream_reply("pasta timer, twelve minutes")) == "Twelve minutes."
    result = client.requests[1]["input"][-1]["output"]
    assert result.startswith("set for ") and 'TARS will say: "Your pasta timer is done."' in result
    assert tools.reminders.active() == []  # nothing yet: the assistant makes it once the turn is kept
    (change,) = brain.changes
    tools.apply(change, from_name="alon")
    (r,) = tools.reminders.active()
    assert (r["kind"], r["text"], r["from_name"], r["set_via"]) == ("timer", "pasta", "alon", "voice")
    assert abs(r["due"] - (time.time() + 720)) < 5
    brain.forget_last()
    assert brain.changes == []


@pytest.mark.parametrize(
    "args, error",
    [
        ({**REMIND, "in_minutes": None}, "give in_minutes, a time (and day), or when_back"),
        ({**REMIND, "kind": "message", "text": "hi"}, "needs someone"),
        ({**REMIND, "in_minutes": None, "day": "2020-01-01", "time": "09:00"}, "already passed"),
        ({**REMIND, "in_minutes": None, "time": "nine-ish"}, "time must be HH:MM"),
        ({**REMIND, "in_minutes": None, "day": "someday", "time": "09:00"}, "day must be"),
        ({**REMIND, "for": "stacey", "kind": "message", "text": "hi", "when_back": True}, "doesn't know stacey"),
    ],
)
def test_what_cant_be_set_goes_back_to_the_model_to_fix(tmp_path, args, error):
    change, result = reminder_tools(tmp_path).call("remind", args)
    assert change is None and result.startswith("error:") and error in result


def test_a_clock_time_is_local_and_waiting_until_theyre_back_needs_their_voice(tmp_path):
    tools = reminder_tools(tmp_path, voices=["Stacey"])
    at = datetime.fromtimestamp(time.time() + 3600).astimezone()
    change, _ = tools.call("remind", {**REMIND, "in_minutes": None, "day": f"{at:%Y-%m-%d}", "time": f"{at:%H:%M}"})
    assert abs(change.reminder.due - at.replace(second=0, microsecond=0).timestamp()) < 1  # local, as asked
    back = {**REMIND, "kind": "message", "text": "the plumber called", "for": "stacey", "in_minutes": None,
            "when_back": True}  # fmt: skip
    change, result = tools.call("remind", back)
    assert change.reminder.due is None and "when Stacey is next heard" in result


def test_cancelling_and_snoozing_by_number_wait_for_the_turn_too(tmp_path):
    tools = reminder_tools(tmp_path)
    rid = tools.reminders.add(NewReminder(TIMER, "pasta", due=time.time() + 60), WEB)
    change, result = tools.call("cancel_reminder", {"id": rid})
    assert result == "cancelled: Your pasta timer is done." and tools.reminders.get(rid)["status"] == "scheduled"
    tools.apply(change, from_name=None)
    assert tools.reminders.get(rid)["status"] == "cancelled"
    assert tools.call("snooze_reminder", {"id": rid, "minutes": 5})[1].startswith("error: there's no active")
    rid = tools.reminders.add(NewReminder(TIMER, due=time.time() + 60), WEB)
    change, result = tools.call("snooze_reminder", {"id": rid, "minutes": 10})
    assert result.startswith("snoozed until ")
    tools.apply(change, from_name=None)
    assert tools.reminders.get(rid)["next_at"] > time.time() + 590


def test_cancelling_a_missed_one_by_voice_is_refused_not_confirmed(tmp_path):
    tools = reminder_tools(tmp_path)
    rid = tools.reminders.add(NewReminder(TIMER, due=time.time() + 60), WEB)
    tools.reminders.store.write("UPDATE reminders SET status='missed' WHERE id=?", (rid,))
    change, result = tools.call("cancel_reminder", {"id": rid})
    assert change is None and result.startswith("error:")
    assert tools.call("snooze_reminder", {"id": rid, "minutes": 5})[0] is not None


def test_a_reminder_due_now_is_still_set_when_the_turn_is_kept_a_while_later(tmp_path):
    tools = reminder_tools(tmp_path)
    asked = time.time() - 300  # the model asked five minutes ago (a long web search)
    change, _ = tools.call("remind", {**REMIND, "in_minutes": 0}, now=asked)
    tools.apply(change, from_name="alon")
    assert len(tools.reminders.active()) == 1


class ToolCallStream(FakeStream):
    """A streamed chat completion that calls tools, the arguments arriving in two pieces, as Cerebras streams them."""

    def __init__(self, calls):
        super().__init__("")
        self.calls = calls

    def __iter__(self):
        for i, (name, args) in enumerate(self.calls):
            text = json.dumps(args)
            for n, piece in enumerate([text[:5], text[5:]]):
                function = types.SimpleNamespace(name=name if n == 0 else None, arguments=piece)
                part = types.SimpleNamespace(index=i, id=f"qc_{i}" if n == 0 else None, function=function)
                delta = types.SimpleNamespace(content=None, tool_calls=[part])
                yield types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)])


def quick_with_tools(tmp_path, calls, then="Pasta timer, twelve minutes.", **cfg):
    """A brain whose Qwen has its own tools: it makes `calls` first, then says `then` once it has their results."""
    openai, tools = fake_openai_chat(lambda m: "From OpenAI."), reminder_tools(tmp_path)
    cerebras = fake_cerebras(lambda m: then)
    plain = cerebras.chat.completions.create

    def create(**kw):
        if kw["messages"][-1]["role"] == "tool" or kw.get("tool_choice") == "none":
            return plain(**kw)
        cerebras.requests.append(kw)
        return ToolCallStream(calls)

    cerebras.chat.completions.create = create
    config = LLMConfig(quick_tools=True, **cfg)
    brain = QuickChat(openai, config, [QuickService("Cerebras", cerebras, "qwen")], reminders=tools)
    return brain, openai, cerebras, tools


def test_with_its_own_tools_qwen_sets_a_reminder_and_openai_is_never_asked(tmp_path):
    brain, openai, cerebras, tools = quick_with_tools(tmp_path, [("remind", REMIND)])
    assert "".join(brain.stream_reply("pasta timer, twelve minutes")) == "Pasta timer, twelve minutes."
    assert openai.requests == [] and brain.answered_by == llm.QUICK
    first, second = cerebras.requests
    assert {t["function"]["name"] for t in first["tools"]} == {"remind", "cancel_reminder", "snooze_reminder", "send"}
    asked_for, result = second["messages"][-2:]
    assert json.loads(asked_for["tool_calls"][0]["function"]["arguments"]) == REMIND  # put back together whole
    assert result["role"] == "tool" and result["content"].startswith("set for ")
    (change,) = brain.changes
    tools.apply(change, from_name="alon")
    assert [r["text"] for r in tools.reminders.active()] == ["pasta"]


def test_with_its_own_tools_qwen_is_told_it_can_and_hands_over_only_the_web(tmp_path):
    brain, _, cerebras, _ = quick_with_tools(tmp_path, [])
    "".join(brain.stream_reply("hi"))
    system = cerebras.requests[0]["messages"][0]["content"]
    assert llm.CAN_REMIND in system and llm.CAN_SEND_NO_LINKS in system and llm.QUICK_SEND_RULES in system
    assert llm.REMIND_RULES in system and llm.NEEDS_THE_WEB in system
    assert llm.NEEDS_REMINDING not in system and llm.NEEDS_SENDING not in system and "to-do list" not in system


def test_without_its_own_tools_qwen_gets_none_and_hands_reminders_over():
    brain, _, cerebras = cerebras_brain(lambda m: "ok")
    "".join(brain.stream_reply("hi"))
    assert "tools" not in cerebras.requests[0]
    assert llm.NEEDS_SENDING in cerebras.requests[0]["messages"][0]["content"]


def test_qwen_cant_send_a_link_it_didnt_find_on_the_web(tmp_path):
    link = {"kind": "link", "title": "Rome", "for": "person", "url": "https://made.up/rome"}
    brain, _, cerebras, _ = quick_with_tools(tmp_path, [("send", link)], then="Sent.")
    "".join(brain.stream_reply("send me a link about Rome"))
    assert cerebras.requests[-1]["messages"][-1]["content"].startswith("error: a link needs web search")
    assert brain.sent == []
    send = next(t for t in cerebras.requests[0]["tools"] if t["function"]["name"] == "send")["function"]
    assert (
        "link" not in send["parameters"]["properties"]["kind"]["enum"] and "url" not in send["parameters"]["required"]
    )


def test_a_refused_link_isnt_passed_on_with_the_hand_off_that_follows_it(tmp_path):
    link = {"kind": "link", "title": "Rome", "for": "person", "url": "https://made.up/rome"}
    brain, openai, _, _ = quick_with_tools(tmp_path, [("send", link)], then=LOOK_UP)
    assert "".join(brain.stream_reply("send me a link about Rome")) == "From OpenAI."
    assert all(m.get("type") is None for m in openai.requests[0]["input"])
    assert LOOK_UP not in json.dumps(openai.requests[0]["input"])


def test_what_qwens_tools_did_goes_with_a_hand_off_so_it_isnt_done_twice(tmp_path):
    brain, openai, _, _ = quick_with_tools(tmp_path, [("remind", REMIND)], then=LOOK_UP)
    assert "".join(brain.stream_reply("pasta timer, and what's the weather?")) == "From OpenAI."
    kinds = [m.get("type") for m in openai.requests[0]["input"]]
    assert kinds == [None, "function_call", "function_call_output"] and len(brain.changes) == 1


def test_a_hard_question_goes_to_the_thinking_model_which_takes_its_time():
    brain, openai, _ = cerebras_brain(lambda m: llm.PONDER, think_model="o-big", think_effort="high")
    said = "".join(brain.stream_reply("plan three days in Rome"))
    assert said == f"{llm.THINKING} From OpenAI." and brain.answered_by == llm.PONDERED
    (request,) = openai.requests
    assert request["model"] == "o-big" and request["reasoning"] == {"effort": "high"}
    assert llm.THINK_RULES in request["instructions"]
    assert asked(openai) == ["plan three days in Rome", llm.THINKING]  # it knows what TARS already said


def test_a_question_for_the_web_doesnt_go_to_the_thinking_model():
    brain, openai, _ = cerebras_brain(lambda m: LOOK_UP, think_model="o-big", reasoning_effort="none")
    "".join(brain.stream_reply("weather?"))
    assert openai.requests[0]["model"] == "gpt-4.1-mini" and openai.requests[0]["reasoning"] == {"effort": "none"}
    assert llm.THINK_RULES not in openai.requests[0]["instructions"]


def test_qwen_is_told_when_to_hand_over_a_hard_question_unless_thats_off():
    brain, openai, cerebras = cerebras_brain(lambda m: LOOK_UP)
    "".join(brain.stream_reply("hi"))
    assert llm.PONDER in cerebras.requests[0]["messages"][0]["content"]
    assert llm.PONDER not in openai.requests[0]["instructions"]  # OpenAI never hands over
    brain, _, cerebras = cerebras_brain(lambda m: "ok", think_effort="")
    "".join(brain.stream_reply("hi"))
    assert llm.PONDER not in cerebras.requests[0]["messages"][0]["content"]


def test_the_thinking_model_is_given_a_time_limit():
    brain, openai, _ = cerebras_brain(lambda m: llm.PONDER, think_timeout_s=0.05)

    def slow(**kw):
        for _ in range(100):
            time.sleep(0.01)
            yield types.SimpleNamespace(type="response.in_progress")

    openai.responses.create = slow
    reply = brain.stream_reply("plan three days in Rome")
    assert next(reply) == f"{llm.THINKING} "
    with pytest.raises(ReplyFailed, match="no answer within 0 s"):
        list(reply)


def test_qwen_can_think_more_than_openai_without_slowing_openai_down():
    brain, openai, cerebras = cerebras_brain(lambda m: LOOK_UP, reasoning_effort="none", quick_reasoning_effort="low")
    "".join(brain.stream_reply("weather?"))
    assert cerebras.requests[0]["reasoning_effort"] == "low"
    assert openai.requests[0]["reasoning"] == {"effort": "none"}
    brain, _, cerebras = cerebras_brain(lambda m: "ok", reasoning_effort="none")
    "".join(brain.stream_reply("hi"))
    assert cerebras.requests[0]["reasoning_effort"] == "none"  # unset, it's the same as OpenAI's


class FakeClaudeStream:
    """A Messages API stream: text events, then the final message, with any tool calls in it."""

    def __init__(self, text="", uses=()):
        self.text, self.uses, self.closed = text, list(uses), False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def __iter__(self):
        for piece in [self.text[:3], self.text[3:]] if self.text else []:
            yield types.SimpleNamespace(type="text", text=piece)

    def close(self):
        self.closed = True

    def get_final_message(self):
        blocks = [types.SimpleNamespace(type="text", text=self.text)] if self.text else []
        for i, (name, args) in enumerate(self.uses):
            blocks.append(types.SimpleNamespace(type="tool_use", id=f"toolu_{i}", name=name, input=args))
        return types.SimpleNamespace(stop_reason="tool_use" if self.uses else "end_turn", content=blocks)


def claude_brain(tmp_path, reply_for, **cfg):
    """A brain whose quick model is Claude: `reply_for(messages)` gives each request's FakeClaudeStream, or an error."""
    openai, requests = fake_openai_chat(lambda m: "From OpenAI."), []

    def stream(**kw):
        requests.append(kw)
        reply = reply_for(kw["messages"])
        if isinstance(reply, Exception):
            raise reply
        return reply

    claude = types.SimpleNamespace(messages=types.SimpleNamespace(stream=stream))
    config = LLMConfig(quick_tools=True, **cfg)
    quick = [QuickService("Claude", claude, "claude-haiku-5-5")]
    return llm.ClaudeQuickChat(openai, config, quick, reminder_tools(tmp_path)), openai, requests


def test_with_claude_as_the_quick_model_it_sets_a_reminder_itself(tmp_path):
    def reply_for(messages):
        if messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
            return FakeClaudeStream("Twelve minutes.")
        return FakeClaudeStream(uses=[("remind", REMIND)])

    brain, openai, requests = claude_brain(tmp_path, reply_for, quick_reasoning_effort="none")
    assert "".join(brain.stream_reply("pasta timer, twelve minutes")) == "Twelve minutes."
    assert openai.requests == [] and brain.answered_by == llm.QUICK and len(brain.changes) == 1
    first, second = requests
    assert first["output_config"] == {"effort": "low"}  # Claude's thinking can't be off: low is the least
    assert {t["name"] for t in first["tools"]} >= {"remind", "send"} and all(t["strict"] for t in first["tools"])
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}  # the instructions, cached
    (result,) = second["messages"][-1]["content"]
    assert result["tool_use_id"] == "toolu_0" and result["content"].startswith("set for ")


def test_claude_hands_over_like_qwen_and_fails_over_to_openai(tmp_path):
    brain, _, _ = claude_brain(tmp_path, lambda m: FakeClaudeStream(LOOK_UP))
    assert "".join(brain.stream_reply("weather?")) == "From OpenAI." and brain.answered_by == llm.LOOKED_UP
    import anthropic
    import httpx2

    down = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    brain, _, _ = claude_brain(tmp_path, lambda m: down)
    assert "".join(brain.stream_reply("hi")) == "From OpenAI." and brain.answered_by == llm.FALLBACK


@pytest.mark.parametrize(
    "body, refused",
    [
        ("BBC weather: https://www.bbc.co.uk/weather", True),
        ("It's at bbc.com/weather", True),
        ("Look up www.metoffice.gov.uk", True),
        ("Pancakes: 200 g flour, 2 eggs, 300 ml milk. Whisk, rest, fry.", False),
    ],
)
def test_qwen_cant_slip_a_link_into_a_note_either(tmp_path, body, refused):
    note = {"kind": "note", "title": "Weather", "for": "person", "body": body}
    brain, _, cerebras, _ = quick_with_tools(tmp_path, [("send", note)], then="Done.")
    "".join(brain.stream_reply("send me that"))
    result = cerebras.requests[-1]["messages"][-1]["content"]
    assert result.startswith("error: a link needs web search") == refused and (brain.sent == []) == refused


def responses_brain(tmp_path, quick, **cfg):
    """A brain whose quick model is an OpenAI model through the Responses API (`quick`: a fake_openai_chat)."""
    openai = fake_openai_chat(lambda m: "From OpenAI.")
    config = LLMConfig(quick_tools=True, **cfg)
    services = [QuickService("OpenAI", quick, "gpt-6-luna")]
    return llm.ResponsesQuickChat(openai, config, services, reminder_tools(tmp_path)), openai


def test_luna_as_the_quick_model_reasons_and_sets_a_reminder_on_its_own_tier(tmp_path):
    def calls_for(messages):
        return [] if messages[-1].get("type") == "function_call_output" else [("remind", REMIND)]

    quick = fake_openai_chat(lambda m: "Twelve minutes." if m[-1].get("type") else "", calls_for)
    brain, openai = responses_brain(tmp_path, quick, quick_reasoning_effort="low", service_tier="fast")
    assert "".join(brain.stream_reply("pasta timer, twelve minutes")) == "Twelve minutes."
    assert openai.requests == [] and brain.answered_by == llm.QUICK and len(brain.changes) == 1
    first, second = quick.requests
    assert (
        first["model"] == "gpt-6-luna" and first["reasoning"] == {"effort": "low"} and first["service_tier"] == "fast"
    )
    assert {t["name"] for t in first["tools"]} >= {"remind", "send"} and first["store"] is False
    assert second["input"][-1]["output"].startswith("set for ")


def test_luna_as_the_quick_model_hands_over_like_qwen(tmp_path):
    brain, _ = responses_brain(tmp_path, fake_openai_chat(lambda m: LOOK_UP))
    assert "".join(brain.stream_reply("weather?")) == "From OpenAI." and brain.answered_by == llm.LOOKED_UP


def test_a_blank_line_before_the_hand_off_still_gets_looking_it_up_said():
    openai = fake_openai_chat(lambda m: "Sunny.", search_for=lambda m: ["response.web_search_call.in_progress"])
    cerebras = fake_cerebras(lambda m: f"\n\n{LOOK_UP}")  # Qwen sometimes writes a blank line first
    brain = QuickChat(openai, LLMConfig(), [QuickService("Cerebras", cerebras, "qwen")])
    assert "".join(brain.stream_reply("weather?")).strip() == f"{llm.SEARCHING} Sunny."


class Silent(FakeStream):
    """A stream that says nothing until it's closed, and then fails, as the SDK's does."""

    def __init__(self):
        super().__init__("")
        self.shut = threading.Event()

    def __iter__(self):
        self.shut.wait(5)
        raise gone()
        yield

    def close(self):
        super().close()
        self.shut.set()


def test_when_groq_is_slow_to_start_cerebras_is_asked_too_and_the_first_to_answer_is_kept(monkeypatch):
    monkeypatch.setattr(llm, "HEDGE_S", 0.05)
    brain, groq, _ = groq_then_cerebras(lambda m: Silent())
    started = time.monotonic()
    assert "".join(brain.stream_reply("hi")) == "From Cerebras." and time.monotonic() - started < 2
    assert brain.quick_service == "Cerebras" and brain.answered_by == llm.QUICK
    assert groq.streams[0].closed


def test_when_groq_starts_in_time_cerebras_is_never_asked(monkeypatch):
    monkeypatch.setattr(llm, "HEDGE_S", 5)
    brain, _, cerebras = groq_then_cerebras(lambda m: "From Groq.")
    assert "".join(brain.stream_reply("hi")) == "From Groq." and brain.quick_service == "Groq"
    assert cerebras.requests == []


def test_when_groq_fails_cerebras_is_asked_at_once_without_waiting_for_the_hedge(monkeypatch):
    monkeypatch.setattr(llm, "HEDGE_S", 5)
    brain, _, _ = groq_then_cerebras(lambda m: OpenAIError("down"))
    started = time.monotonic()
    assert "".join(brain.stream_reply("hi")) == "From Cerebras." and time.monotonic() - started < 2
    assert brain.quick_service == "Cerebras"


def test_interrupting_while_both_are_starting_closes_both(monkeypatch):
    monkeypatch.setattr(llm, "HEDGE_S", 0.01)
    brain, groq, cerebras = groq_then_cerebras(lambda m: Silent(), lambda m: Silent())
    reply = brain.stream_reply("hi")
    threading.Timer(0.2, brain.interrupt).start()
    with pytest.raises(ReplyFailed):
        list(reply)
    assert groq.streams[0].closed and cerebras.streams[0].closed
