"""A streamed reply as TARS says it: split into sentences, cleaned of what can't be read out, and the TARS effect."""

import numpy as np
import pytest

from voice_assistant.effects import SpeakerBox, VoiceWithEffect, apply_effect
from voice_assistant.speech import clean_for_speech, split_sentences


def test_sentences_are_split_as_they_stream_in():
    pieces = ["Hello th", "ere. It is 3.5 deg", "rees! Want more?", " Ok"]
    assert list(split_sentences(pieces)) == ["Hello there.", "It is 3.5 degrees!", "Want more?", "Ok"]


def test_a_sentence_never_ends_inside_a_link():
    pieces = ["Read the [Dr. Who fan ", "site](https://example.com/who) now. ", "Then rest."]
    assert list(split_sentences(pieces)) == ["Read the Dr. Who fan site now.", "Then rest."]


def test_a_bracket_that_isnt_a_link_doesnt_stop_the_splitting():
    pieces = ["Rome has three airports [1]. ", "The main one is Fiumicino. ", "It's big."]
    assert list(split_sentences(pieces)) == ["Rome has three airports [1].", "The main one is Fiumicino.", "It's big."]


def test_markdown_is_stripped_before_speaking():
    assert clean_for_speech("**Bold** and `code` # heading ") == "Bold and code  heading"


@pytest.mark.parametrize(
    "text, spoken",
    [
        (
            "A frittata works well ([mayoclinic.org](https://www.mayoclinic.org/recipes/frittata?utm_source=openai)).",
            "A frittata works well.",
        ),
        ("([mayoclinic.org](https://www.mayoclinic.org/x?p=1&utm_source=openai))", ""),
        ("Try [this frittata](https://example.com/frittata) tonight.", "Try this frittata tonight."),
        ("It's at https://example.com/a?b=1 if you want it.", "It's at if you want it."),
        ("See [Python](https://en.wikipedia.org/wiki/Python_(programming_language)) for more.", "See Python for more."),
    ],
)
def test_links_and_citations_are_never_read_out(text, spoken):
    assert clean_for_speech(text) == spoken


def test_speaker_box_is_identical_whether_streamed_or_whole():
    audio = (np.random.default_rng(1).normal(0, 3000, 24_000 * 2)).astype(np.int16).tobytes()
    whole = SpeakerBox(24_000).process(audio)
    box = SpeakerBox(24_000)
    streamed = b"".join(box.process(audio[i : i + 4096]) for i in range(0, len(audio), 4096))
    assert streamed == whole


def test_voice_with_effect_handles_odd_sized_chunks():
    class OddVoice:
        sample_rate = 24_000

        def stream(self, text):
            yield b"\x01"  # half a sample
            yield b"\x00" * 999

    out = b"".join(VoiceWithEffect(OddVoice(), "tars").stream("hi"))
    assert len(out) % 2 == 0 and len(out) >= 1000


def test_no_effect_returns_the_voice_unchanged():
    voice = object()
    assert apply_effect(voice, "") is voice
