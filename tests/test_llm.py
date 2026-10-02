"""The brain: skipping overheard chatter, memory, the send tool, failures, and Cerebras handing turns to OpenAI."""

import types

import pytest
from openai import OpenAIError

from voice_assistant import llm
from voice_assistant.config import LLMConfig
from voice_assistant.conversations import SentItem
from voice_assistant.llm import (
    LOOK_UP,
    MAX_TOOL_ROUNDS,
    SKIP,
    CerebrasChat,
    OpenAIChat,
    ReplyFailed,
    split_skip,
)

from .conftest import fake_openai_chat


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


class FakeStream:
    """A streamed chat completion that can be closed, like the SDK's."""

    def __init__(self, text):
        self.pieces, self.closed = [text[:3], text[3:]], False

    def __iter__(self):
        for piece in self.pieces:
            yield types.SimpleNamespace(choices=[types.SimpleNamespace(delta=types.SimpleNamespace(content=piece))])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


def fake_cerebras(reply_for):
    """A Chat Completions client: each request streams `reply_for(messages)`, or raises it if it's an error."""
    requests, streams = [], []

    def create(**kw):
        requests.append(kw)
        reply = reply_for(kw["messages"])
        if isinstance(reply, Exception):
            raise reply
        streams.append(FakeStream(reply))
        return streams[-1]

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    client.requests, client.streams = requests, streams
    return client


def cerebras_brain(quick_reply, openai_reply="From OpenAI.", **cfg):
    openai, cerebras = fake_openai_chat(lambda m: openai_reply), fake_cerebras(quick_reply)
    brain = CerebrasChat(openai, LLMConfig(cerebras_model="qwen", **cfg), cerebras)
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
    assert "".join(llm._UpTo(iter(text), LOOK_UP)) == text  # one character at a time


def test_both_models_are_told_the_date_and_time_here(monkeypatch):
    now = "It's Tuesday, September 29, 2026, 11:26 PM here (IDT, UTC+03:00)."
    monkeypatch.setattr(llm, "local_time", lambda: now)
    brain, openai, cerebras = cerebras_brain(lambda m: LOOK_UP)
    "".join(brain.stream_reply("what's on tonight?"))
    assert cerebras.requests[0]["messages"][0]["content"].endswith(now)
    assert openai.requests[0]["instructions"].endswith(now)
