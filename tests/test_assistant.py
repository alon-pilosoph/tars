import types

import numpy as np
import pytest
from openai import OpenAIError

import voice_assistant.assistant as assistant_module
from voice_assistant.conversations import HOUSEHOLD, LIST, NOTE, PERSON, SentItem
from voice_assistant.events import EventLog
from voice_assistant.journal import Journal
from voice_assistant.llm import ASKED_TAG, FOLLOW_UP_TAG

from .conftest import AUDIO, make_assistant

pytestmark = pytest.mark.usefixtures("no_tts")


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

    def fail(pcm, follow_up=False, tag=None):
        raise error

    assistant.handle = fail
    assistant.converse(follow_up_s=4.0)
    assert speaker.sounds == [("error", None)]


def test_did_you_call_me_carries_on_when_answered(speaker):
    assistant, brain = make_assistant(speaker, [b"yes", None], ["yes, what's the weather"])
    assistant.ask_if_called(follow_up_s=4.0)
    assert brain.asked == [f"{ASKED_TAG} yes, what's the weather"]
    assert assistant.recorder.timeouts[0] == assistant_module.ASK_TIMEOUT_S


def test_did_you_call_me_goes_quiet_on_silence_or_no(speaker):
    assistant, brain = make_assistant(speaker, [None], [])
    assistant.ask_if_called(follow_up_s=4.0)
    assert brain.asked == []
    assistant, brain = make_assistant(speaker, [b"no"], ["no"], replies=["<skip>"])
    assistant.ask_if_called(follow_up_s=4.0)
    assert brain.forgotten == 1 and assistant.recorder.timeouts == [assistant_module.ASK_TIMEOUT_S]


class FakeSpeakerID:
    def __init__(self, who):
        self.voiceprints = {"alon": None}
        self.who, self.thresholds = who, []

    def identify(self, pcm, threshold=None):
        self.thresholds.append(threshold)
        return self.who

    def describe(self, pcm):
        return self.who, 0.8 if self.who else 0.1, None


def test_a_bare_wake_word_gets_a_greeting_by_name_then_the_question(speaker):
    assistant, brain = make_assistant(speaker, [None, b"q", None], ["what's the weather"])
    assistant.trigger = types.SimpleNamespace(last_audio=np.zeros(32000, np.int16))
    assistant.speaker_id = FakeSpeakerID("alon")
    said = []
    assistant.say = said.append
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5)
    assert said == ["Yes, Alon?"]
    assert assistant.recorder.timeouts[:2] == [1.5, None]  # short wait, greeting, then the normal wait
    assert assistant.speaker_id.thresholds[0] == assistant.name_threshold  # then the normal check on the question
    assert brain.asked[0].endswith("what's the weather")


def test_greeting_without_a_confident_name_is_just_yes(speaker):
    assistant, _ = make_assistant(speaker, [None, None], [])
    assistant.speaker_id = FakeSpeakerID(None)
    assistant.trigger = types.SimpleNamespace(last_audio=np.zeros(32000, np.int16))
    said = []
    assistant.say = said.append
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5)
    assert said == ["Yes?"]


def test_asking_straight_away_skips_the_greeting(speaker):
    assistant, brain = make_assistant(speaker, [b"q", None], ["what time is it"])
    said = []
    assistant.say = said.append
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5)
    assert said == [] and brain.asked == ["what time is it"]


def logged_assistant(speaker, tmp_path, utterances, transcripts, replies=()):
    journal = Journal(EventLog(tmp_path / "events"))
    assistant, _ = make_assistant(speaker, utterances, transcripts, replies, journal=journal)
    journal.wake(AUDIO, 0.9, "answer", "hey tars", 0.9, "", "")
    assistant.trigger = types.SimpleNamespace(last_audio=None)
    return assistant, journal.conversations


def test_a_conversation_is_logged_turn_by_turn(speaker, tmp_path):
    assistant, convos = logged_assistant(
        speaker,
        tmp_path,
        [b"q1", b"q2", b"as", None],
        ["what's the weather", "and tomorrow", "pass the salt"],
        replies=["Sunny.", "Rain.", "<skip>"],
    )
    assistant.converse(follow_up_s=4.0)
    (summary,) = convos.conversations()
    conv = convos.get(summary["id"])
    assert [(t["role"], t["text"]) for t in conv["turns"]] == [
        ("person", "what's the weather"),
        ("tars", "Sunny."),
        ("person", "and tomorrow"),
        ("tars", "Rain."),
        ("person", "pass the salt"),
    ]
    assert conv["turns"][-1]["not_for_tars"] and conv["turns"][0]["has_audio"]
    assert conv["wake"]["heard"] == "hey tars" and conv["ended"] is not None


def test_the_first_request_keeps_one_copy_of_its_audio_on_the_wake(speaker, tmp_path):
    assistant, convos = logged_assistant(speaker, tmp_path, [b"q1", None], ["what's the weather"], replies=["Sunny."])
    assistant.converse(follow_up_s=4.0)
    conv = convos.get(convos.conversations()[0]["id"])
    wake = convos.events.get(conv["wake"]["event_id"])
    assert convos.turn(conv["turns"][0]["id"])["audio"] == wake["utterance_audio"]
    assert wake["transcript"] == "what's the weather" and wake["follow"] == "asked"


def test_did_you_call_me_and_greetings_open_the_thread(speaker, tmp_path):
    assistant, convos = logged_assistant(speaker, tmp_path, [b"ye", None], ["yes, set a timer"], replies=["Done."])
    assistant.say = lambda text: None
    assistant.ask_if_called(follow_up_s=4.0)
    turns = convos.get(convos.conversations()[0]["id"])["turns"]
    assert [(t["role"], t["text"]) for t in turns] == [
        ("tars", "Did you call me?"),
        ("person", "yes, set a timer"),
        ("tars", "Done."),
    ]

    assistant, convos = logged_assistant(speaker, tmp_path / "2", [None, b"qq", None], ["what time is it"], ["Noon."])
    assistant.speaker_id = FakeSpeakerID("alon")
    assistant.trigger.last_audio = np.zeros(32000, np.int16)
    assistant.say = lambda text: None
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5)
    turns = convos.get(convos.conversations()[0]["id"])["turns"]
    assert [t["text"] for t in turns] == ["Yes, Alon?", "what time is it", "Noon."]
    assert turns[1]["speaker"]["name"] == "Alon"


def test_no_conversation_when_nobody_speaks(speaker, tmp_path):
    assistant, convos = logged_assistant(speaker, tmp_path, [None], [])
    assistant.say = lambda text: None
    assistant.ask_if_called(follow_up_s=4.0)
    assistant.recorder.script = [None, None]
    assistant.speaker_id = FakeSpeakerID(None)
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5)  # "Yes?", then silence
    assert convos.conversations() == []


def test_no_conversation_when_the_request_was_only_noise(speaker, tmp_path):
    assistant, convos = logged_assistant(speaker, tmp_path, [None, b"qq"], [""])
    assistant.say = lambda text: None
    assistant.speaker_id = FakeSpeakerID(None)
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5)  # "Yes?", then a noise that transcribes to nothing
    assert convos.conversations() == []


@pytest.mark.parametrize("broken", ["add_person_turn", "add_tars_turn", "start", "add_item"])
def test_a_broken_conversation_log_never_costs_a_reply(speaker, tmp_path, monkeypatch, broken):
    assistant, convos = logged_assistant(speaker, tmp_path, [b"qq", None], ["what's the weather"], replies=["Sunny."])
    assistant.brain.sent = [SentItem(NOTE, "Forecast", body="Sunny.")]
    monkeypatch.setattr(convos, broken, lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    assistant.speaker.error_tone = lambda: pytest.fail("the error tone played")
    assistant.converse(follow_up_s=4.0)
    assert assistant.brain.asked == ["what's the weather"]


def test_a_broken_event_log_never_costs_a_reply(speaker, tmp_path, monkeypatch):
    assistant, convos = logged_assistant(speaker, tmp_path, [None], [])
    monkeypatch.setattr(convos.events, "set_follow", lambda *a: (_ for _ in ()).throw(OSError("database is locked")))
    assistant.say = lambda text: None
    assistant.ask_if_called(follow_up_s=4.0)  # nobody answers: saving that fails, and nothing else happens


def test_a_conversation_deleted_while_it_goes_on_stays_deleted(speaker, tmp_path):
    assistant, convos = logged_assistant(
        speaker, tmp_path, [b"q1", b"q2", None], ["hello", "and another thing"], replies=["Hi.", "Go on."]
    )
    brain = assistant.brain
    original = brain.stream_reply

    def delete_after_first(text):
        if len(brain.asked) == 1:
            convos.delete(convos.conversations()[0]["id"])  # from the web UI, mid-conversation
        return original(text)

    brain.stream_reply = delete_after_first
    assistant.converse(follow_up_s=4.0)
    assert convos.conversations() == [] and convos.store.rows("SELECT * FROM turns") == []


def test_what_tars_sends_is_kept_with_its_reply(speaker, tmp_path):
    assistant, convos = logged_assistant(
        speaker, tmp_path, [b"qq", None], ["send me the shopping list"], replies=["Sent it."]
    )
    assistant.speaker_id = FakeSpeakerID("alon")
    assistant.brain.sent = [
        SentItem(LIST, "Shopping", scope=HOUSEHOLD, entries=["milk"]),
        SentItem(NOTE, "Pasta", scope=PERSON, body="Boil water."),
    ]
    assistant.converse(follow_up_s=4.0)
    conv = convos.get(convos.conversations()[0]["id"])
    shopping, pasta = conv["turns"][1]["items"]
    assert shopping["scope"] == HOUSEHOLD and shopping["for"] is None
    assert pasta["scope"] == PERSON and pasta["for"]["name"] == "Alon"


def test_sent_for_an_unknown_voice_goes_to_the_household(speaker, tmp_path):
    assistant, convos = logged_assistant(speaker, tmp_path, [b"qq", None], ["send me a recipe"], replies=["Sent."])
    assistant.speaker_id = FakeSpeakerID(None)
    assistant.brain.sent = [SentItem(NOTE, "Pasta", scope=PERSON, body="Boil water.")]
    assistant.converse(follow_up_s=4.0)
    (item,) = convos.items()
    assert item["scope"] == HOUSEHOLD


def test_sent_items_are_kept_even_if_the_conversation_couldnt_be(speaker, tmp_path, monkeypatch):
    assistant, convos = logged_assistant(speaker, tmp_path, [b"qq", None], ["send me a recipe"], replies=["Sent."])
    monkeypatch.setattr(convos, "start", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    assistant.brain.sent = [SentItem(NOTE, "Pasta", scope=HOUSEHOLD, body="Boil water.")]
    assistant.converse(follow_up_s=4.0)
    (item,) = convos.items()
    assert item["title"] == "Pasta" and item["conversation_id"] is None
