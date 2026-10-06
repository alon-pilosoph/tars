"""The conversation loop: follow-ups, "Did you call me?", greetings, error lines, and what's written down."""

import threading
import time
import types

import numpy as np
import pytest
from openai import OpenAIError

import voice_assistant.assistant as assistant_module
from voice_assistant.assistant import ASK_PHRASE, ERROR_LINES, Assistant
from voice_assistant.audio import SpeakerError, chime
from voice_assistant.conversations import HOUSEHOLD, LIST, NOTE, PERSON, TIMINGS, SentItem
from voice_assistant.events import EventLog
from voice_assistant.journal import Journal
from voice_assistant.llm import ASKED_TAG, FOLLOW_UP_TAG, QUICK, REMINDER_TAG
from voice_assistant.reminders import (
    ACKNOWLEDGED,
    ADD,
    MESSAGE,
    REMINDER,
    SAID,
    TIMER,
    VOICE,
    WAITING,
    WEB,
    Change,
    NewReminder,
    Reminders,
    ReminderTools,
)
from voice_assistant.speech import StreamedReply
from voice_assistant.stt import BufferedSession

from .conftest import AUDIO, make_assistant, raising

pytestmark = pytest.mark.usefixtures("no_tts")


def test_follow_ups_continue_until_silence(speaker):
    assistant, brain = make_assistant(speaker, [b"a", b"b", None], ["what time is it", "and tomorrow"])
    assistant.converse(follow_up_s=4.0)
    assert brain.asked == ["what time is it", f"{FOLLOW_UP_TAG} and tomorrow"]
    assert assistant.recorder.timeouts == [None, 4.0, 4.0]


def test_overheard_follow_up_is_skipped_forgotten_and_ends_the_conversation(speaker):
    assistant, brain = make_assistant(
        speaker, [b"a", b"b", b"c"], ["capital of France", "honey, did you buy milk"], replies=["Paris.", "<skip>"]
    )
    assistant.converse(follow_up_s=4.0)
    assert len(brain.asked) == 2
    assert brain.forgotten == 1
    assert assistant.recorder.timeouts == [None, 4.0]


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
    assert brain.forgotten == 0


def failing(assistant, error):
    def fail(heard):
        raise error

    assistant.handle = fail


@pytest.mark.parametrize("error", [RuntimeError("boom"), OpenAIError("connection dropped")])
def test_a_failed_request_says_so_in_tars_voice(speaker, error):
    assistant, _ = make_assistant(speaker, [b"a"], [])
    assistant.voice.stream = lambda text: iter([text.encode()])
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    failing(assistant, error)
    assistant.converse(follow_up_s=4.0)
    assert [chunk.decode() for chunk in played] in [[line] for line in ERROR_LINES["dry"]]


def test_below_half_humor_the_error_lines_are_plain(speaker):
    assistant, _ = make_assistant(speaker, [b"a"], [])
    assistant = Assistant(
        assistant.mic,
        speaker,
        None,
        assistant.recorder,
        assistant.transcriber,
        assistant.brain,
        assistant.voice,
        humor=20,
    )
    assistant.voice.stream = lambda text: iter([text.encode()])
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    failing(assistant, RuntimeError("boom"))
    assistant.converse(follow_up_s=4.0)
    assert [chunk.decode() for chunk in played] in [[line] for line in ERROR_LINES["plain"]]


def test_when_the_voice_is_down_too_it_stays_quiet(speaker):
    assistant, _ = make_assistant(speaker, [b"a"], [])

    def down(text):
        raise ConnectionError("no voice")

    assistant.voice.stream = down
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    failing(assistant, OpenAIError("the voice service is down too"))
    assistant.converse(follow_up_s=4.0)
    assert played == []


def test_an_error_line_that_failed_to_make_is_made_again_later(speaker):
    assistant, _ = make_assistant(speaker, [b"a"], [])
    down = [True]

    def flaky(text):
        if down[0]:
            raise ConnectionError("no network yet")
        return iter([text.encode()])

    assistant.voice.stream = flaky
    assistant.prepare_phrases()
    assistant.say_error()  # the lines failed: nothing to say it with
    down[0] = False
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    failing(assistant, RuntimeError("boom"))
    assistant.converse(follow_up_s=4.0)
    assert played and played[0].decode() in ERROR_LINES["dry"]


def with_phrases(speaker, folder, stream):
    assistant, _ = make_assistant(speaker, [b"a"], [])
    assistant = Assistant(
        assistant.mic,
        speaker,
        None,
        assistant.recorder,
        assistant.transcriber,
        assistant.brain,
        assistant.voice,
        phrases=folder,
    )
    assistant.voice.stream = stream
    return assistant


def test_an_error_line_made_once_plays_after_a_restart_with_the_voice_down(speaker, tmp_path):
    first = with_phrases(speaker, tmp_path, lambda text: iter([text.encode()]))
    first.prepare_phrases()
    first._phrasing.shutdown(wait=True)  # every line made, and saved

    def down(text):
        raise ConnectionError("no network since boot")

    later = with_phrases(speaker, tmp_path, down)
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    failing(later, RuntimeError("boom"))
    later.converse(follow_up_s=4.0)
    assert [chunk.decode() for chunk in played] in [[line] for line in ERROR_LINES["dry"]]


def test_a_line_that_cant_be_saved_still_plays(speaker, tmp_path):
    (tmp_path / "phrases").write_text("a file where the folder should be")
    assistant = with_phrases(speaker, tmp_path / "phrases", lambda text: iter([text.encode()]))
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    assert assistant.say("Yes?")
    assert played == [b"Yes?"]


def test_a_line_that_failed_to_make_is_tried_again_when_it_is_needed(speaker):
    assistant, _ = make_assistant(speaker, [], [])
    down = [True]

    def flaky(text):
        if down[0]:
            raise ConnectionError("no network yet")
        return iter([text.encode()])

    assistant.voice.stream = flaky
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    assert not assistant.say(ASK_PHRASE)  # just after boot, before the network is up
    down[0] = False
    assert assistant.say(ASK_PHRASE) and played == [ASK_PHRASE.encode()]


def test_a_new_voice_makes_its_lines_again(tmp_path):
    from voice_assistant.__main__ import phrase_folder
    from voice_assistant.config import TTSConfig

    before = phrase_folder(types.SimpleNamespace(tts=TTSConfig(voice="alloy")), tmp_path)
    assert phrase_folder(types.SimpleNamespace(tts=TTSConfig(voice="onyx")), tmp_path) != before
    assert phrase_folder(types.SimpleNamespace(tts=TTSConfig(voice="alloy")), tmp_path) == before


def test_a_typed_question_is_answered_out_loud(speaker):
    assistant, brain = make_assistant(speaker, [], [], replies=["It's noon."])
    said = assistant.answer_text("what time is it")
    assert brain.asked == ["what time is it"] and said.text == "It's noon."


def test_a_dead_speaker_stops_the_assistant_instead_of_being_one_failed_request(speaker):
    assistant, _ = make_assistant(speaker, [b"a"], ["hello"])
    speaker.play_pcm_stream = raising(SpeakerError("the speaker stopped playing"))
    with pytest.raises(SpeakerError):
        assistant.converse(follow_up_s=4.0)


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
    assert assistant.speaker_id.thresholds[0] == assistant.name_threshold  # the greeting uses the wake clip's bar
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


def test_what_was_said_is_written_down_while_tars_answers_not_before(speaker, tmp_path, monkeypatch):
    assistant, convos = logged_assistant(speaker, tmp_path, [b"q1", None], ["what's the weather"], replies=["Sunny."])
    answering = threading.Event()
    write = convos.add_person_turn

    def slow_disk(*args, **kwargs):
        if not answering.wait(5):
            raise TimeoutError("the reply waited for the log")
        return write(*args, **kwargs)

    monkeypatch.setattr(convos, "add_person_turn", slow_disk)
    speaker.play_pcm_stream = lambda chunks, *a, **kw: (list(chunks), answering.set())
    assistant.converse(follow_up_s=4.0)
    turns = convos.get(convos.conversations()[0]["id"])["turns"]
    assert [(t["role"], t["text"]) for t in turns] == [("person", "what's the weather"), ("tars", "Sunny.")]


def test_the_first_request_keeps_one_copy_of_its_audio_on_the_wake(speaker, tmp_path):
    assistant, convos = logged_assistant(speaker, tmp_path, [b"q1", None], ["what's the weather"], replies=["Sunny."])
    assistant.converse(follow_up_s=4.0)
    conv = convos.get(convos.conversations()[0]["id"])
    wake = convos.events.get(conv["wake"]["event_id"])
    assert convos.turn(conv["turns"][0]["id"])["audio"] == wake["utterance_audio"]
    assert wake["transcript"] == "what's the weather" and wake["follow"] == "asked"


def test_did_you_call_me_and_greetings_open_the_thread(speaker, tmp_path):
    assistant, convos = logged_assistant(speaker, tmp_path, [b"ye", None], ["yes, set a timer"], replies=["Done."])
    assistant.say = lambda text: True
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
    assistant.say = lambda text: True
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5)
    turns = convos.get(convos.conversations()[0]["id"])["turns"]
    assert [t["text"] for t in turns] == ["Yes, Alon?", "what time is it", "Noon."]
    assert turns[1]["speaker"]["name"] == "Alon"


def test_no_conversation_when_nobody_speaks(speaker, tmp_path):
    assistant, convos = logged_assistant(speaker, tmp_path, [None], [])
    assistant.say = lambda text: True
    assistant.ask_if_called(follow_up_s=4.0)
    assistant.recorder.script = [None, None]
    assistant.speaker_id = FakeSpeakerID(None)
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5)  # "Yes?", then silence
    assert convos.conversations() == []


def test_no_conversation_when_the_request_was_only_noise(speaker, tmp_path):
    assistant, convos = logged_assistant(speaker, tmp_path, [None, b"qq"], [""])
    assistant.say = lambda text: True
    assistant.speaker_id = FakeSpeakerID(None)
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5)  # "Yes?", then a noise that transcribes to nothing
    assert convos.conversations() == []


@pytest.mark.parametrize("broken", ["add_person_turn", "add_tars_turn", "start", "add_item"])
def test_a_broken_conversation_log_never_costs_a_reply(speaker, tmp_path, monkeypatch, broken):
    assistant, convos = logged_assistant(speaker, tmp_path, [b"qq", None], ["what's the weather"], replies=["Sunny."])
    assistant.brain.sent = [SentItem(NOTE, "Forecast", body="Sunny.")]
    monkeypatch.setattr(convos, broken, raising(OSError("disk full")))
    assistant.say_error = lambda: pytest.fail("it said something went wrong")
    assistant.converse(follow_up_s=4.0)
    assert assistant.brain.asked == ["what's the weather"]


def test_a_broken_event_log_never_costs_a_reply(speaker, tmp_path, monkeypatch):
    assistant, convos = logged_assistant(speaker, tmp_path, [None], [])
    monkeypatch.setattr(convos.events, "set_follow", raising(OSError("database is locked")))
    assistant.say = lambda text: True
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
    monkeypatch.setattr(convos, "start", raising(OSError("disk full")))
    assistant.brain.sent = [SentItem(NOTE, "Pasta", scope=HOUSEHOLD, body="Boil water.")]
    assistant.converse(follow_up_s=4.0)
    (item,) = convos.items()
    assert item["title"] == "Pasta" and item["conversation_id"] is None


def turns_of(convos):
    (summary,) = convos.conversations()
    return convos.get(summary["id"])["turns"]


def playing(speaker):
    def play(chunks, *a, on_first_audio=None, **kw):
        for _ in chunks:
            if on_first_audio:
                on_first_audio()
                on_first_audio = None

    speaker.play_pcm_stream = play


def test_each_answer_is_kept_with_how_long_it_took_and_who_wrote_it(speaker, tmp_path, monkeypatch):
    monkeypatch.setattr(assistant_module, "StreamedReply", StreamedReply)
    assistant, convos = logged_assistant(speaker, tmp_path, [b"q", None], ["what time is it"], replies=["Late."])
    playing(speaker)
    assistant.brain.answered_by = QUICK
    assistant.converse(follow_up_s=4.0)
    tars = turns_of(convos)[-1]
    assert tars["text"] == "Late." and tars["answered_by"] == QUICK and tars["failed_at"] is None
    assert set(tars["timings"]) == set(TIMINGS) and tars["timings"]["total"] >= 0


@pytest.mark.parametrize("stage", ["stt", "llm", "tts"])
def test_a_failed_answer_is_kept_with_where_it_failed(speaker, tmp_path, monkeypatch, stage):
    monkeypatch.setattr(assistant_module, "StreamedReply", StreamedReply)
    assistant, convos = logged_assistant(speaker, tmp_path, [b"q"], ["what's the weather"], replies=["Sunny."])
    playing(speaker)
    down = OpenAIError(f"{stage} is down")
    if stage == "stt":
        assistant.transcriber = types.SimpleNamespace(session=lambda: BufferedSession(raising(down)))
    elif stage == "llm":
        assistant.brain.stream_reply = raising(down)
    else:
        assistant.voice.stream = raising(down)
    assistant.converse(follow_up_s=4.0)
    turns = turns_of(convos)
    # When nothing could be heard, the failure starts the conversation on its own.
    assert [t["role"] for t in turns] == (["tars"] if stage == "stt" else ["person", "tars"])
    assert turns[-1]["failed_at"] == stage and turns[-1]["error"] == f"OpenAIError: {stage} is down"
    assert turns[-1]["text"] == ("Sunny." if stage == "tts" else "")  # what it had written before the voice failed


def test_a_bug_is_kept_as_a_failure_too(speaker, tmp_path):
    assistant, convos = logged_assistant(speaker, tmp_path, [b"q"], ["hi"])
    assistant.brain.stream_reply = lambda text: iter(["fine"])
    assistant._speak = raising(KeyError("oops"))
    assistant.converse(follow_up_s=4.0)
    assert turns_of(convos)[-1]["failed_at"] == "other"


def reminding(speaker, tmp_path, utterances, transcripts, replies=(), **kw):
    """A logged assistant with one reminder due now; returns (assistant, reminders, its id, what was played)."""
    assistant, convos = logged_assistant(speaker, tmp_path, utterances, transcripts, replies)
    assistant.journal.end_conversation()  # a reminder comes due while TARS waits, not after a wake
    assistant.reminders = Reminders(convos.store)
    played = []
    assistant.voice.stream = lambda text: iter([text.encode()])
    speaker.play_pcm_stream = lambda chunks, *a, **k: played.extend(chunks)
    kw = {"kind": MESSAGE, "text": "dinner's at eight", "for_name": "stacey", "from_name": "alon", **kw}
    rid = assistant.reminders.add(NewReminder(due=time.time(), **kw), WEB)
    return assistant, assistant.reminders, rid, played


def spoken(played):
    return [c.decode() for c in played if not c.startswith(b"\0\0")]  # the chime starts silent


def test_a_due_reminder_is_said_after_a_chime_and_got_it_acknowledges_it(speaker, tmp_path):
    assistant, reminders, rid, played = reminding(speaker, tmp_path, [b"ok", None], ["got it"], replies=["<ack>"])
    assistant.say_reminders(follow_up_s=4.0)
    assert spoken(played)[0] == "Stacey, a message from Alon: dinner's at eight."
    assert len(played[0]) > 1000 and played[0] == chime(24_000)  # the chime first
    assert assistant.brain.asked[0].startswith(REMINDER_TAG)
    r = reminders.get(rid)
    assert (r["status"], r["acked_via"], r["acked_by"], r["tries"]) == (ACKNOWLEDGED, VOICE, None, 1)
    turns = turns_of(assistant.journal.conversations)
    assert [(t["role"], t["text"]) for t in turns] == [
        ("tars", "Stacey, a message from Alon: dinner's at eight."),
        ("person", "got it"),
        ("tars", "Got it."),
    ]


def test_nobody_answering_leaves_it_waiting_to_be_said_again(speaker, tmp_path):
    assistant, reminders, rid, _ = reminding(speaker, tmp_path, [None], [])
    assistant.say_reminders()
    r = reminders.get(rid)
    assert (r["status"], r["tries"]) == (WAITING, 1) and r["next_at"] > time.time() + 60
    assert assistant.journal.conversations.conversations() == []  # nothing said back, no conversation


def test_a_reply_not_meant_for_tars_doesnt_acknowledge_it(speaker, tmp_path):
    assistant, reminders, rid, _ = reminding(speaker, tmp_path, [b"chat"], ["pass the salt"], replies=["<skip>"])
    assistant.say_reminders(follow_up_s=4.0)
    assert reminders.get(rid)["status"] == WAITING


def test_one_that_doesnt_wait_is_said_once_without_listening(speaker, tmp_path):
    assistant, reminders, rid, played = reminding(speaker, tmp_path, [], [], needs_ack=False)
    assistant.say_reminders()  # the recorder has nothing scripted: listening would fail
    assert reminders.get(rid)["status"] == SAID and spoken(played) == [
        "Stacey, a message from Alon: dinner's at eight."
    ]


def test_said_late_it_says_when_it_was_due(speaker, tmp_path):
    assistant, _, rid, played = reminding(speaker, tmp_path, [None], [])
    assistant.reminders.store.write("UPDATE reminders SET due=due-3600, next_at=next_at-3600 WHERE id=?", (rid,))
    assistant.say_reminders()
    assert spoken(played)[1].startswith("This was due ")


def test_later_i_got_the_message_acknowledges_the_one_waiting(speaker, tmp_path):
    assistant, reminders, rid, _ = reminding(speaker, tmp_path, [None, b"q", None], ["I got the message"])
    assistant.say_reminders()
    assistant.brain.replies = iter([f"<ack {rid}> Good."])
    assistant.converse(follow_up_s=4.0)
    r = reminders.get(rid)
    assert (r["status"], r["acked_via"]) == (ACKNOWLEDGED, VOICE)
    assert turns_of(assistant.journal.conversations)[-1]["text"] == "Good."


def test_a_bare_ack_in_conversation_is_for_the_one_said_last(speaker, tmp_path):
    assistant, reminders, first, _ = reminding(speaker, tmp_path, [None, None, b"q", None], ["done"])
    second = reminders.add(NewReminder(REMINDER, "pills", due=time.time()), WEB)
    reminders.store.write("UPDATE reminders SET next_at=? WHERE id=?", (time.time() + 999, second))
    assistant.say_reminders()
    reminders.store.write("UPDATE reminders SET next_at=? WHERE id=?", (time.time() - 1, second))
    assistant.say_reminders()
    assistant.brain.replies = iter(["<ack>"])
    assistant.converse(follow_up_s=4.0)
    assert [reminders.get(i)["status"] for i in (first, second)] == [WAITING, ACKNOWLEDGED]


def test_an_answer_thrown_away_acknowledges_nothing(speaker, tmp_path):
    # They paused ("got...") and the draft said <ack>, then carried on ("got a question actually").
    assistant, reminders, rid, _ = reminding(
        speaker, tmp_path, [["pause", "resume", b"q"], None], ["got", "got a question actually"],
        replies=["<ack>", "Go ahead."],
    )  # fmt: skip
    assistant.say_reminders(follow_up_s=4.0)
    assert reminders.get(rid)["status"] == WAITING


def test_a_reminder_set_by_voice_is_made_once_the_answer_is_kept_in_its_conversation(speaker, tmp_path):
    assistant, convos = logged_assistant(speaker, tmp_path, [b"q", None], ["pasta timer, twelve minutes"])
    tools = ReminderTools(Reminders(convos.store))
    assistant.reminder_tools, assistant.reminders = tools, tools.reminders
    change = Change(ADD, NewReminder(TIMER, "pasta", due=time.time() + 720))
    stream = assistant.brain.stream_reply

    def asking_for_a_timer(text):
        assistant.brain.changes = [change]
        return stream(text)

    assistant.brain.stream_reply = asking_for_a_timer
    assistant.converse(follow_up_s=4.0)
    (r,) = tools.reminders.active()
    (conversation,) = convos.conversations()
    assert (r["text"], r["set_via"], r["conversation_id"]) == ("pasta", VOICE, conversation["id"])


def holding(speaker, tmp_path, who, utterances, transcripts, replies):
    """A logged assistant that hears `who`, with a message held for Stacey until she's heard."""
    assistant, convos = logged_assistant(speaker, tmp_path, utterances, transcripts, replies)
    assistant.speaker_id = FakeSpeakerID(who)
    tools = ReminderTools(Reminders(convos.store), voices=lambda: ["stacey"])
    assistant.reminder_tools, assistant.reminders = tools, tools.reminders
    new = NewReminder(MESSAGE, "the plumber called", "stacey", "alon", due=None)
    rid = tools.reminders.add(new, WEB, voices=["stacey"])
    return assistant, tools.reminders, rid


def test_a_message_waiting_for_someone_is_said_after_answering_them_and_their_got_it_counts(speaker, tmp_path):
    assistant, reminders, rid = holding(
        speaker, tmp_path, "stacey", [b"q", b"ok", None], ["what's the time", "got it"], ["Nine.", "<ack>"]
    )
    assistant.converse(follow_up_s=4.0)
    assert assistant.brain.asked[1].startswith(REMINDER_TAG)
    r = reminders.get(rid)
    assert (r["status"], r["acked_by"], r["tries"]) == (ACKNOWLEDGED, "stacey", 1)
    assert [t["text"] for t in turns_of(assistant.journal.conversations)] == [
        "what's the time",
        "Nine.",
        "By the way, Stacey, a message from Alon: the plumber called.",
        "got it",
        "Got it.",
    ]


@pytest.mark.parametrize("who", ["alon", None])
def test_it_waits_while_anyone_else_talks(speaker, tmp_path, who):
    assistant, reminders, rid = holding(speaker, tmp_path, who, [b"q", None], ["what's the time"], ["Nine."])
    assistant.converse(follow_up_s=4.0)
    assert reminders.get(rid)["tries"] == 0 and reminders.held_for("stacey")


def test_a_ringing_timer_only_chimes_between_its_lines_and_stop_turns_it_off(speaker, tmp_path):
    assistant, reminders, rid, played = reminding(
        speaker, tmp_path, [None, b"stop", None], ["stop"], replies=["<ack>"], kind=TIMER, text="pasta", for_name=None,
        from_name=None,
    )  # fmt: skip
    assistant.say_reminders()  # the first ring: the chime and the line
    assert spoken(played) == ["Your pasta timer is done."]
    played.clear()
    reminders.store.write("UPDATE reminders SET next_at=? WHERE id=?", (time.time() - 1, rid))
    assistant.say_reminders(follow_up_s=4.0)  # the second: only the chime, then "stop"
    assert spoken(played) == [] and played[0] == chime(24_000)
    r = reminders.get(rid)
    assert (r["status"], r["tries"]) == (ACKNOWLEDGED, 2)
