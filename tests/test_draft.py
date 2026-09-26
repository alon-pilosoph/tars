"""Start early, speak late: an answer prepared in a pause is used when they're done, and thrown away without a
trace if they carry on talking."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from voice_assistant.config import LLMConfig
from voice_assistant.draft import Draft
from voice_assistant.llm import OpenAIChat

from .conftest import fake_openai_chat, make_assistant, speech


def played_by(speaker):
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    return played


def test_an_answer_started_in_a_pause_is_the_one_played(speaker):
    assistant, brain = make_assistant(speaker, [["pause", speech()]], ["what time is it"], replies=["It's noon."])
    assistant.voice.stream = lambda text: iter([text.encode()])
    played = played_by(speaker)
    assistant.converse(follow_up_s=0)
    assert brain.asked == ["what time is it"] and played == [b"It's noon."] and brain.forgotten == 0


def test_talking_again_throws_the_early_answer_away(speaker):
    assistant, brain = make_assistant(
        speaker,
        [["pause", "resume", "pause", speech()]],
        ["what time", "what time is it in Tokyo"],
        replies=["Early.", "It's 3 AM."],
    )
    assistant.voice.stream = lambda text: iter([text.encode()])
    played = played_by(speaker)
    assistant.converse(follow_up_s=0)
    assert played == [b"It's 3 AM."]
    assert brain.asked == ["what time", "what time is it in Tokyo"] and brain.forgotten == 1


def test_without_a_pause_it_answers_at_the_end(speaker):
    assistant, brain = make_assistant(speaker, [speech()], ["hello"], replies=["Hello."])
    assistant.voice.stream = lambda text: iter([text.encode()])
    played = played_by(speaker)
    assistant.converse(follow_up_s=0)
    assert played == [b"Hello."] and brain.asked == ["hello"]


def test_the_brain_forgets_a_reply_it_was_still_writing():
    client = fake_openai_chat(lambda messages: "One. Two. Three.")
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False))
    list(brain.stream_reply("first"))
    unfinished = brain.stream_reply("second")
    next(unfinished)
    brain.forget_last()
    assert [m["content"] for m in brain._history] == ["first", "One. Two. Three."]


def test_a_cancelled_draft_is_undone_once_prepared():
    pool, undone = ThreadPoolExecutor(max_workers=1), []
    started = threading.Event()

    def prepare(draft):
        started.set()
        return "answer"

    draft = Draft(pool, prepare, undone.append)
    started.wait(1)
    draft.cancel()
    pool.shutdown(wait=True)
    assert undone == ["answer"]


def test_a_draft_that_failed_raises_when_taken():
    def prepare(draft):
        raise ConnectionError("the stream dropped")

    draft = Draft(ThreadPoolExecutor(max_workers=1), prepare, lambda answer: None)
    with pytest.raises(ConnectionError):
        draft.take()
    draft.done()
