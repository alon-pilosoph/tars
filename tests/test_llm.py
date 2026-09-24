import pytest

import voice_assistant.llm as llm
from voice_assistant.config import LLMConfig
from voice_assistant.llm import SKIP, OpenAIChat, split_skip

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


def test_system_prompt_includes_the_follow_up_protocol():
    captured = {}

    def reply(messages):
        captured["system"] = messages[0]["content"]
        return "ok"

    brain = OpenAIChat(fake_openai_chat(reply), LLMConfig(system_prompt="You are TARS."))
    "".join(brain.stream_reply("hi"))
    assert captured["system"].startswith("You are TARS.")
    assert SKIP in captured["system"]
