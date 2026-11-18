"""Ending a conversation by saying so: "thanks", "that's all"."""

import pytest

from voice_assistant.closing import DONE, THANKS, closing

from .conftest import make_assistant


@pytest.mark.parametrize(
    "text, kind",
    [
        ("Thanks.", THANKS),
        ("Okay, thanks TARS!", THANKS),
        ("great, thank you very much", THANKS),
        ("thanks for that", THANKS),
        ("never mind", DONE),
        ("That's all, thank you.", DONE),
        ("No thanks", DONE),
        ("nothing else", DONE),
        ("I’m good, thanks", DONE),
    ],
)
def test_a_way_to_end_the_conversation_is_heard_as_one(text, kind):
    assert closing(text) == kind


@pytest.mark.parametrize(
    "text",
    [
        "thanks, and turn off the lights",
        "thank you, what about tomorrow?",
        "stop",  # the music, a timer: about something
        "cancel the timer",
        "good night",  # may start the house's evening
        "what's the weather",
        "",
    ],
)
def test_anything_more_is_a_request(text):
    assert closing(text) is None


def test_thanks_gets_a_short_line_without_the_model_and_ends_the_conversation():
    assistant, brain = make_assistant([b"a", b"b", b"c"], ["what time is it", "thanks", "and tomorrow"])
    assistant.converse(follow_up_s=4.0)
    assert brain.asked == ["what time is it"]  # the model wasn't asked about "thanks"
    assert brain.remembered and brain.remembered[-1][1] == "Anytime."  # the exchange is in the conversation
    assert assistant.body.timeouts == [None, 4.0]  # no third listen: it ended


def test_a_request_with_thanks_in_it_is_still_answered():
    assistant, brain = make_assistant([b"a", b"b", None], ["what time is it", "thanks, and tomorrow?"])
    assistant.converse(follow_up_s=4.0)
    assert len(brain.asked) == 2 and "thanks, and tomorrow?" in brain.asked[1]


def test_after_a_question_no_thanks_is_the_answer_for_the_model():
    assistant, brain = make_assistant(
        [b"a", b"b", None], ["is the door locked", "no thanks"], replies=["It is. Lock the back too?"]
    )
    assistant.converse(follow_up_s=4.0)
    assert len(brain.asked) == 2 and "no thanks" in brain.asked[1]
