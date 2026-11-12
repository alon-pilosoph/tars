"""TARS's body (body.py): the assistant driven end to end through the port, by a FakeBody's scripted wakes and
utterances, and LocalBody doing for the port what the assistant did with the Pi's parts itself."""

import types
from concurrent.futures import Future
from contextlib import contextmanager

import numpy as np
import pytest

from voice_assistant.assistant import ASK_PHRASE, ASK_TIMEOUT_S
from voice_assistant.audio import MicrophoneError, SpeakerError
from voice_assistant.body import LocalBody, Recorded, Wake
from voice_assistant.llm import FOLLOW_UP_TAG
from voice_assistant.verify import ANSWER, ASK

from .conftest import Asleep, make_assistant, raising, speech

NAMED = Wake(ANSWER)  # its name, confirmed


def awake(utterances, transcripts, replies=(), wakes=(NAMED,)):
    """An assistant whose body wakes as `wakes` say, then hears `utterances`; its voice says each sentence as its
    text."""
    assistant, brain = make_assistant(utterances, transcripts, replies)
    assistant.voice.stream = lambda text: iter([text.encode()])
    assistant.body.wakes = list(wakes)
    return assistant, brain


def run(assistant):
    with pytest.raises(Asleep):  # no more wakes
        assistant.run_forever("Say 'hey tars'...", follow_up_s=4.0)


# ---------- The assistant, through the port ----------


def test_woken_by_name_it_answers_and_listens_for_a_follow_up_until_nobody_speaks():
    assistant, brain = awake([speech(), speech(), None], ["what time is it", "and in Tokyo"], ["Noon.", "Three AM."])
    run(assistant)
    assert brain.asked == ["what time is it", f"{FOLLOW_UP_TAG} and in Tokyo"]
    assert assistant.body.plays == [b"Noon.", b"Three AM."]  # no greeting: the request came straight away
    assert assistant.body.timeouts == [None, 4.0, 4.0] and assistant.body.waits == 2
    assert {"end_of_speech", "stt", "llm", "tts", "total"} <= set(assistant.timings)


def test_a_follow_up_not_meant_for_tars_is_skipped_and_ends_the_conversation():
    assistant, brain = awake(
        [speech(), speech()], ["capital of France", "honey, did you buy milk"], replies=["Paris.", "<skip>"]
    )
    run(assistant)
    assert assistant.body.plays == [b"Paris."] and brain.forgotten == 1
    assert assistant.body.timeouts == [None, 4.0]  # nobody listened for after it


def test_what_followed_the_name_is_where_the_request_starts():
    what = np.ones(1920, np.int16)
    found: Future = Future()
    found.set_result(what)
    assistant, _ = awake([speech(), None], ["what's new"], wakes=[Wake(ANSWER, None, found)])
    run(assistant)
    assert assistant.body.leads[0] is what


def test_a_wake_that_only_sounded_close_asks_first():
    assistant, brain = awake([None], [], wakes=[Wake(ASK)])
    run(assistant)
    assert assistant.body.plays == [ASK_PHRASE.encode()] and brain.asked == []
    assert assistant.body.timeouts == [ASK_TIMEOUT_S]


def test_stopped_mid_reply_it_listens_for_no_follow_up_and_the_next_wake_isnt_cut_short():
    assistant, brain = awake([speech(), speech(), None], ["tell me a story", "what time is it"], wakes=[Wake()] * 2)

    def press_stop(chunks):  # the button, on the first reply only
        if not assistant.body.stops:
            assistant.stop()

    assistant.body.on_play = press_stop
    run(assistant)
    assert brain.asked == ["tell me a story", "what time is it"]
    assert assistant.body.timeouts == [None, None, 4.0]  # no follow-up after the story; one after the time
    assert assistant.body.stops == 1


def test_a_play_the_body_cut_short_itself_is_taken_as_the_stop_button():
    """How "TARS, stop" said over a reply will stop it: the body hears it, and cuts its play short."""
    assistant, brain = awake([speech(), speech()], ["tell me a story", "and another"])
    interrupted = []
    brain.interrupt = lambda: interrupted.append(True)
    assistant.body.cuts = {0}
    run(assistant)
    assert brain.asked == ["tell me a story"] and assistant.body.timeouts == [None]
    assert interrupted == [True] and assistant.body.stops == 1  # what's still being written stops too


@pytest.mark.parametrize("fails", ["wait", "record", "play"])
def test_an_audio_device_that_fails_ends_tars_with_no_error_line(fails):
    """So systemd starts it again on the device that came back."""
    assistant, _ = awake([speech(), None], ["what time is it"])
    error = SpeakerError("gone") if fails == "play" else MicrophoneError("gone")
    setattr(assistant.body, fails, raising(error))
    with pytest.raises(type(error)):
        assistant.run_forever("Say 'hey tars'...", follow_up_s=4.0)
    assert assistant.body.plays == []  # "something went wrong" isn't said: it's no failed request


# ---------- LocalBody ----------


class Mic:
    def __init__(self, echo=None):
        self.echo, self.pauses, self.cleared, self.discarded = echo, 0, 0, 0

    @contextmanager
    def paused(self):
        self.pauses += 1
        yield

    def clear(self):
        self.cleared += 1

    def discard(self):
        self.discarded += 1


class Speaker:
    def __init__(self, cut=False):
        self.cut, self.stops, self.played = cut, 0, []

    def play_pcm_stream(self, chunks, sample_rate, on_first_audio=None):
        self.played.append((list(chunks), sample_rate, on_first_audio))
        return self.cut

    def stop(self):
        self.stops += 1


def test_local_body_waits_on_the_trigger_with_its_mic_and_hands_back_what_woke_it():
    mic, audio, after = Mic(), np.zeros(320, np.int16), Future()
    asked = []

    def wait(m, due=None):
        asked.append((m, due))
        return ANSWER

    trigger = types.SimpleNamespace(wait=wait, last_audio=audio, after_name=after)
    body = LocalBody(mic, Speaker(), trigger, None)
    due = lambda: False
    assert body.wait(due) == Wake(ANSWER, audio, after) and asked == [(mic, due)]


def test_local_body_records_from_its_mic_and_says_how_long_the_silence_after_was():
    mic, heard = Mic(), []

    def record(m, **kw):
        heard.append((m, kw))
        return b"pcm"

    recorder = types.SimpleNamespace(record=record, trailing_silence_s=0.4)
    body = LocalBody(mic, Speaker(), None, recorder)
    assert body.record(start_timeout_s=4.0, max_s=6.0) == Recorded(b"pcm", 0.4)
    assert heard == [(mic, {"start_timeout_s": 4.0, "max_s": 6.0})]
    recorder.record = lambda m, **kw: None
    assert body.record() is None


def test_local_body_shuts_the_mic_while_it_plays_unless_it_goes_on_listening():
    mic, speaker = Mic(), Speaker()
    body = LocalBody(mic, speaker, None, None)
    first = object()
    assert body.play([b"yes"], 24_000, first) is False
    assert mic.pauses == 1 and speaker.played == [([b"yes"], 24_000, first)]
    body.play([b"yes, alon?"], 24_000, listening=True)
    assert mic.pauses == 1  # the greeting, said with the echo taken out
    assert LocalBody(None, speaker, None, None).play([b"noon"], 24_000) is False  # typed: no mic to shut


def test_local_body_says_a_play_was_cut_short_and_stops_its_speaker():
    speaker = Speaker(cut=True)
    body = LocalBody(Mic(), speaker, None, None)
    assert body.play([b"once upon"], 24_000) is True
    body.stop()
    assert speaker.stops == 1


def test_local_body_hears_while_it_speaks_only_with_echo_cancellation_working():
    assert not LocalBody(None, Speaker(), None, None).hears_while_speaking
    assert not LocalBody(Mic(), Speaker(), None, None).hears_while_speaking
    assert not LocalBody(Mic(types.SimpleNamespace(working=False)), Speaker(), None, None).hears_while_speaking
    assert LocalBody(Mic(types.SimpleNamespace(working=True)), Speaker(), None, None).hears_while_speaking


def test_local_body_throws_away_and_clears_what_its_mic_heard():
    mic = Mic()
    body = LocalBody(mic, Speaker(), None, None)
    body.discard()
    body.clear()
    assert (mic.discarded, mic.cleared) == (1, 1)
