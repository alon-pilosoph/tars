"""An answer prepared in a pause is used once the speaker is done, and thrown away without a trace if they carry on
talking."""

import threading

import pytest

from voice_assistant.draft import Draft
from voice_assistant.speech import StreamedReply

from .conftest import make_assistant, speech


def played_by(speaker):
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    return played


def test_stopping_a_reply_stuck_on_the_network_does_not_wait_for_it():
    network = threading.Event()

    def pieces():
        network.wait()  # a web search that takes its time
        yield "Sunny."

    reply = StreamedReply(pieces(), voice=None, on_sentence=lambda sentence: None)
    stopped = threading.Thread(target=reply.stop)
    stopped.start()
    stopped.join(1)
    network.set()
    assert not stopped.is_alive()


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


def test_a_reply_that_fails_while_playing_is_stopped_and_forgotten(speaker):
    assistant, brain = make_assistant(speaker, [speech()], ["tell me a story"], replies=["Once. Upon. A time."])
    interrupted = []
    brain.interrupt = lambda: interrupted.append(True)

    def fail(chunks, *args, **kwargs):
        raise TimeoutError("the voice stalled")

    speaker.play_pcm_stream = fail
    assistant.say_error = lambda: None
    assistant.converse(follow_up_s=0)
    with assistant._thinking:  # the discarded draft has been cleaned up once it lets go
        assert brain.forgotten == 1 and interrupted == [True]


def test_a_question_the_brain_failed_on_is_forgotten(speaker):
    assistant, brain = make_assistant(speaker, [["pause", speech()]], ["yes, the lights"])

    def broken(text, request=None):
        brain.asked.append(text)
        raise ConnectionError("the brain is down")
        yield

    brain.stream_reply = broken
    assistant.say = lambda text: True
    assistant.say_error = lambda: None
    assistant.ask_if_called(follow_up_s=0)
    assert brain.forgotten == 1


def test_a_cancelled_draft_is_undone_once_prepared():
    turn, undone = threading.Lock(), []
    started = threading.Event()

    def prepare(draft):
        started.set()
        return "answer"

    draft = Draft(turn, prepare, undone.append)
    started.wait(1)
    draft.cancel()
    with turn:  # the draft holds it until it's settled
        assert undone == ["answer"]


def test_a_draft_that_failed_raises_when_taken():
    def prepare(draft):
        raise ConnectionError("the stream dropped")

    draft = Draft(threading.Lock(), prepare, lambda answer: None)
    with pytest.raises(ConnectionError):
        draft.take()
    draft.cancel()


def test_a_reply_thrown_away_stops_its_sentences_and_frees_the_voice():
    speaking, closed = threading.Event(), threading.Event()

    class Voice:
        def stream(self, text):
            try:
                while True:
                    speaking.set()
                    yield b"\x00\x00"
            finally:
                closed.set()

    reply = StreamedReply(iter(["A long first sentence."]), Voice(), on_sentence=lambda sentence: None)
    assert speaking.wait(1)
    reply.stop()
    assert closed.wait(1)


def test_what_an_answer_does_waits_for_the_turn_and_a_thrown_away_one_does_nothing(speaker):
    assistant, brain = make_assistant(
        speaker,
        [["pause", "resume", "pause", speech()]],
        ["unlock the", "unlock the door"],
        replies=["Early.", "Unlock the front door?"],
    )
    assistant.voice.stream = lambda text: iter([text.encode()])
    played_by(speaker)
    assistant.converse(follow_up_s=0)
    thrown_away, played = brain.requests
    assert played.by_voice and thrown_away.by_voice
    assert thrown_away.may_act(0) is False and played.may_act(0) is True


def test_a_draft_may_act_only_once_its_turn_is_over():
    draft = Draft(threading.Lock(), lambda d: "answer", lambda r: None)
    assert draft.may_act(0.01) is False  # still drafting: not yet
    draft.go()
    assert draft.may_act(0) is True
    draft.cancel()
    assert draft.may_act(0) is False
