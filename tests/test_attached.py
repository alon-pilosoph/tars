"""Photos and files in front of the brain (attached.py, llm.py)."""

import base64

from voice_assistant.attached import (
    IMAGE,
    KEEP_TURNS,
    PDF,
    PHOTO_NAME,
    TEXT,
    Attached,
    ask_for_a_photo,
    kind_of,
)
from voice_assistant.config import LLMConfig
from voice_assistant.home import Request
from voice_assistant.llm import OpenAIChat

from .test_llm import cerebras_brain, fake_openai_chat

PHOTO = Attached(IMAGE, PHOTO_NAME, "image/jpeg", b"jpeg")


def with_photo(text: str = "what is this?") -> Request:
    return Request(speaker="Alon", by_chat=True, heard=text, attached=(PHOTO,))


def test_each_kind_goes_to_the_model_as_it_takes_it():
    assert PHOTO.part() == {
        "type": "input_image",
        "image_url": f"data:image/jpeg;base64,{base64.b64encode(b'jpeg').decode()}",
        "detail": "high",
    }
    pdf = Attached(PDF, "notice.pdf", "application/pdf", b"%PDF").part()
    assert pdf["type"] == "input_file" and pdf["filename"] == "notice.pdf"
    assert pdf["file_data"].startswith("data:application/pdf;base64,")
    text = Attached(TEXT, "list.txt", "text/plain", b"milk\neggs").part()
    assert text["type"] == "input_text" and "list.txt" in text["text"] and "milk\neggs" in text["text"]
    assert PHOTO.tag == "[Photo]" and Attached(IMAGE, "scan.png", "image/png", b"").tag == "[File: scan.png]"


def test_what_the_model_can_take_is_told_by_its_type_or_its_name():
    assert kind_of("image/png", "x.png") == IMAGE and kind_of("application/pdf", "") == PDF
    assert kind_of("", "Notes.PDF") == PDF and kind_of("text/csv", "a.csv") == TEXT
    assert kind_of("application/octet-stream", "event.ics") == TEXT
    assert kind_of("image/heic", "IMG.HEIC") is None and kind_of("application/msword", "a.doc") is None


def test_a_photo_goes_with_its_words_and_the_reply_changes_nothing_in_the_house():
    client = fake_openai_chat(lambda m: "A rash.")
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False))
    assert "".join(brain.stream_reply("[Speaker: Alon] [Photo] what is this?", with_photo())) == "A rash."
    (message,) = client.requests[-1]["input"]
    assert message["content"] == [{"type": "input_text", "text": "[Speaker: Alon] [Photo] what is this?"}, PHOTO.part()]
    request = brain._writing.request
    assert request.read_the_web and request.held_for_web is None  # held back for good: no writing it again without
    "".join(brain.stream_reply("and now?", Request(speaker="Alon")))
    assert not brain._writing.request.read_the_web  # the next message, on its own, may change things


def test_a_photo_stays_in_front_of_the_model_for_a_few_turns_then_only_its_words():
    client = fake_openai_chat(lambda m: "Okay.")
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False, memory_minutes=60))
    "".join(brain.stream_reply("[Photo] what is this?", with_photo()))
    for turn in range(KEEP_TURNS):
        "".join(brain.stream_reply(f"more {turn}"))
        assert isinstance(client.requests[-1]["input"][0]["content"], list)
    "".join(brain.stream_reply("one more"))
    put_away = "[Photo] what is this? (what came with it is no longer at hand)"
    assert client.requests[-1]["input"][0]["content"] == put_away


def test_qwen_never_answers_while_a_photo_is_in_front_of_the_conversation():
    brain, _openai, cerebras = cerebras_brain(lambda m: "From Qwen.")
    assert "".join(brain.stream_reply("[Photo] what is this?", with_photo())) == "From OpenAI."
    assert cerebras.requests == [] and brain.answered_by == "openai"
    for turn in range(KEEP_TURNS):
        assert "".join(brain.stream_reply(f"and {turn}?")) == "From OpenAI."
    assert cerebras.requests == []
    assert "".join(brain.stream_reply("thanks")) == "From Qwen."  # put away: only words now, which Qwen reads


def test_the_voice_is_told_to_ask_for_a_photo_only_with_someone_to_send_it():
    assert "Alon, Stacey" in ask_for_a_photo(["Alon", "Stacey"]) and ask_for_a_photo([]) == ""
