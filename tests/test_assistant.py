import types

import pytest
from openai import OpenAIError

import voice_assistant.assistant as assistant_module
from voice_assistant.assistant import Assistant
from voice_assistant.llm import FOLLOW_UP_TAG

from .conftest import FakeMic


class ScriptedRecorder:
    """record() returns the next scripted utterance (None = nobody spoke) and logs the timeout it was given."""

    trailing_silence_s = 0.0

    def __init__(self, script):
        self.script = list(script)
        self.timeouts = []

    def record(self, mic, start_timeout_s=None, **kw):
        self.timeouts.append(start_timeout_s)
        return self.script.pop(0)


class ScriptedTranscriber:
    def __init__(self, texts):
        self.texts = iter(texts)

    def transcribe(self, pcm):
        return next(self.texts)


class RecordingBrain:
    def __init__(self, replies=()):
        self.replies = iter(replies)
        self.asked = []
        self.forgotten = 0

    def stream_reply(self, text):
        self.asked.append(text)
        return iter([next(self.replies, "ok")])

    def forget_last(self):
        self.forgotten += 1


@pytest.fixture(autouse=True)
def no_tts(monkeypatch):
    """Consume the reply the way playback would, without calling TTS."""
    monkeypatch.setattr(
        assistant_module, "speak_streamed_reply", lambda pieces, voice, on_sentence: iter(list(pieces) and [])
    )


def make_assistant(speaker, utterances, transcripts, replies=()):
    speaker.play_pcm_stream = lambda chunks, *a, **kw: list(chunks)
    voice = types.SimpleNamespace(sample_rate=24_000)
    brain = RecordingBrain(replies)
    recorder = ScriptedRecorder(utterances)
    return Assistant(FakeMic(), speaker, None, recorder, ScriptedTranscriber(transcripts), brain, voice), brain


def test_follow_ups_continue_until_silence(speaker):
    assistant, brain = make_assistant(speaker, [b"a", b"b", None], ["what time is it", "and tomorrow"])
    assistant.converse(follow_up_s=4.0)
    assert brain.asked == ["what time is it", f"{FOLLOW_UP_TAG} and tomorrow"]
    assert assistant.recorder.timeouts == [None, 4.0, 4.0]
    assert [kind for kind, _ in speaker.sounds] == ["chime", "chime"]  # the soft "still listening" cue


def test_overheard_follow_up_is_skipped_forgotten_and_ends_the_conversation(speaker):
    assistant, brain = make_assistant(
        speaker, [b"a", b"b", b"c"], ["capital of France", "honey, did you buy milk"], replies=["Paris.", "<skip>"]
    )
    assistant.converse(follow_up_s=4.0)
    assert len(brain.asked) == 2
    assert brain.forgotten == 1
    assert assistant.recorder.timeouts == [None, 4.0]  # stopped listening after the skip


def test_nothing_said_after_the_wake_word(speaker):
    assistant, brain = make_assistant(speaker, [None], [])
    assistant.converse(follow_up_s=4.0)
    assert brain.asked == []


def test_empty_transcript_ends_the_conversation(speaker):
    assistant, brain = make_assistant(speaker, [b"a", b"b"], ["hello", ""])
    assistant.converse(follow_up_s=4.0)
    assert brain.asked == ["hello"]


def test_follow_ups_can_be_turned_off(speaker):
    assistant, _ = make_assistant(speaker, [b"a"], ["hello"])
    assistant.converse(follow_up_s=0)
    assert assistant.recorder.timeouts == [None]


def test_first_request_is_never_treated_as_overheard(speaker):
    assistant, brain = make_assistant(speaker, [b"a", None], ["hi"], replies=["<skip>"])
    assistant.converse(follow_up_s=4.0)
    assert brain.forgotten == 0  # only follow-ups can be skipped


@pytest.mark.parametrize("error", [RuntimeError("boom"), OpenAIError("connection dropped")])
def test_a_failed_request_plays_the_error_tone_and_returns(speaker, error):
    assistant, _ = make_assistant(speaker, [b"a"], [])

    def fail(pcm, follow_up=False):
        raise error

    assistant.handle = fail
    assistant.converse(follow_up_s=4.0)
    assert speaker.sounds == [("error", None)]
