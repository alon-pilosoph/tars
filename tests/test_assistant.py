"""The conversation loop: follow-ups, "Did you call me?", greetings, error lines, and what's written down."""

import threading
import time
import types
from concurrent.futures import Future

import numpy as np
import pytest
from openai import OpenAIError

import voice_assistant.assistant as assistant_module
from voice_assistant import phrases
from voice_assistant.assistant import ASK_PHRASE, ERROR_LINES, Assistant
from voice_assistant.audio import SpeakerError, chime
from voice_assistant.body import Wake
from voice_assistant.conversations import HOUSEHOLD, LIST, NOTE, PERSON, TIMINGS, SentItem
from voice_assistant.errors import ServiceError
from voice_assistant.events import EventLog
from voice_assistant.journal import Journal
from voice_assistant.llm import ASKED_TAG, FOLLOW_UP_TAG, QUICK, REMINDER_TAG
from voice_assistant.presence import Board, People, State
from voice_assistant.reminders import (
    ACKNOWLEDGED,
    ADD,
    MESSAGE,
    REMINDER,
    SAID,
    SCHEDULED,
    SNOOZE,
    TELEGRAM,
    TIMER,
    VOICE,
    WAITING,
    WEB,
    Change,
    ChatWhenOut,
    NewReminder,
    Reminders,
    ReminderTools,
)
from voice_assistant.stt import BufferedSession

from .conftest import AUDIO, make_assistant, raising


def test_follow_ups_continue_until_silence():
    assistant, brain = make_assistant([b"a", b"b", None], ["what time is it", "and tomorrow"])
    assistant.converse(follow_up_s=4.0)
    assert brain.asked == ["what time is it", f"{FOLLOW_UP_TAG} and tomorrow"]
    assert assistant.body.timeouts == [None, 4.0, 4.0]


def test_overheard_follow_up_is_skipped_forgotten_and_ends_the_conversation():
    assistant, brain = make_assistant(
        [b"a", b"b", b"c"], ["capital of France", "honey, did you buy milk"], replies=["Paris.", "<skip>"]
    )
    assistant.converse(follow_up_s=4.0)
    assert len(brain.asked) == 2
    assert brain.forgotten == 1
    assert assistant.body.timeouts == [None, 4.0]


def test_nothing_said_after_the_wake_word():
    assistant, brain = make_assistant([None], [])
    assistant.converse(follow_up_s=4.0)
    assert brain.asked == []


def test_empty_transcript_ends_the_conversation():
    assistant, brain = make_assistant([b"a", b"b"], ["hello", ""])
    assistant.converse(follow_up_s=4.0)
    assert brain.asked == ["hello"]


def test_an_answer_to_its_question_that_came_out_as_no_words_is_asked_for_again_once():
    # "Which channel, 11 or 12?" and "twelve" said as it ended: heard, but nothing of it written down.
    assistant, brain = make_assistant(
        [b"q", b"a", b"b", b"c"],
        ["turn the news on", "", "", "twelve"],
        replies=["Which channel, 11 or 12?", "On it."],
    )
    said = []
    assistant.say = lambda text, **kw: said.append(text)
    assistant.converse(follow_up_s=4.0)
    assert said == [assistant_module.PARDON]  # once: twice in a row is noise, not an answer
    assert brain.asked == ["turn the news on"]
    assistant, brain = make_assistant(
        [b"q", b"a", b"b", None],
        ["turn the news on", "", "twelve"],
        replies=["Which channel, 11 or 12?", "On it."],
    )
    assistant.say = lambda text, **kw: None
    assistant.converse(follow_up_s=4.0)
    assert brain.asked == ["turn the news on", f"{FOLLOW_UP_TAG} twelve"]


def test_each_question_that_gets_no_words_back_is_asked_again():
    assistant, _ = make_assistant(
        [b"q", b"a", b"b", b"c", None],
        ["turn the news on", "", "twelve", ""],
        replies=["Which channel, 11 or 12?", "The kitchen or the bedroom TV?"],
    )
    said = []
    assistant.say = lambda text, **kw: said.append(text) or True
    assistant.converse(follow_up_s=4.0)
    assert said == [assistant_module.PARDON, assistant_module.PARDON]


def test_asking_again_is_kept_in_the_conversation(tmp_path):
    assistant, convos = logged_assistant(
        tmp_path,
        [b"q", b"a", b"b", None],
        ["turn the news on", "", "twelve"],
        replies=["Which channel, 11 or 12?", "On it."],
    )
    assistant.voice.stream = lambda text: iter([text.encode()])
    assistant.converse(follow_up_s=4.0)
    assert [t["text"] for t in turns_of(convos)] == [
        "turn the news on",
        "Which channel, 11 or 12?",
        assistant_module.PARDON,
        "twelve",
        "On it.",
    ]


def test_asking_again_is_made_ahead():
    assistant, _ = make_assistant([], [])
    assistant.prepare_phrases()
    assert assistant.phrases.made_ahead(assistant_module.PARDON)


def test_no_words_after_an_answer_that_asked_nothing_ends_the_conversation_quietly():
    assistant, brain = make_assistant([b"q", b"a", b"b"], ["lights on", "", "x"], replies=["Done."])
    said = []
    assistant.say = lambda text, **kw: said.append(text)
    assistant.converse(follow_up_s=4.0)
    assert said == [] and brain.asked == ["lights on"]


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("Which channel, 11 or 12?", ("11", "eleven", "12", "twelve")),
        ("Sure. The kitchen or the living room?", ("kitchen", "living")),
        ("11, 12 or 13?", ("12", "twelve", "13", "thirteen", "11", "eleven")),
        ("Done. Anything else?", ()),  # no choice offered
        ("Kitchen or bedroom? Never mind, done.", ()),  # it's no question once it's moved on
    ],
)
def test_the_choices_a_question_offered_are_what_the_answer_is_expected_to_say(said, expected):
    assert assistant_module.offered(said) == expected


def test_the_answer_to_a_choice_is_heard_expecting_the_choices():
    assistant, _ = make_assistant(
        [b"q", b"a", None], ["turn the news on", "twelve"], replies=["Which channel, 11 or 12?", "On it."]
    )
    assistant.converse(follow_up_s=4.0)
    assert assistant.transcriber.expected == [(), ("11", "eleven", "12", "twelve"), ()]


def test_follow_ups_can_be_turned_off():
    assistant, _ = make_assistant([b"a"], ["hello"])
    assistant.converse(follow_up_s=0)
    assert assistant.body.timeouts == [None]


def test_first_request_is_never_treated_as_overheard(tmp_path):
    # "New zone." for "news on", after the wake word: the model took it for overheard, and the tag reached the voice.
    assistant, convos = logged_assistant(tmp_path, [b"a", None], ["New zone."], replies=["<skip>"])
    assistant.converse(follow_up_s=4.0)
    assert assistant.brain.forgotten == 1  # the tag never stays in the history
    assert turns_of(convos)[-1]["text"] == assistant_module.PARDON
    assert assistant.brain.remembered == [assistant_module.PARDON]  # what's said next answers it, not skipped
    assert assistant.body.timeouts == [None, 4.0]  # and the answer is listened for


def test_a_typed_question_that_comes_back_as_a_skip_is_asked_again_and_that_remembered():
    """As aloud: the pardon is part of the conversation, so what's typed next answers it."""
    assistant, brain = make_assistant([], [], replies=["<skip>"])
    said = assistant.answer_text("new zone")
    assert said.text == assistant_module.PARDON and brain.forgotten == 1
    assert brain.remembered == [assistant_module.PARDON]


def failing(assistant, error):
    def fail(heard):
        raise error

    assistant.handle = fail


@pytest.mark.parametrize("error", [RuntimeError("boom"), OpenAIError("connection dropped")])
def test_a_failed_request_says_so_in_tars_voice(error):
    assistant, _ = make_assistant([b"a"], [])
    assistant.voice.stream = lambda text: iter([text.encode()])
    played = assistant.body.played
    failing(assistant, error)
    assistant.converse(follow_up_s=4.0)
    assert [chunk.decode() for chunk in played] in [[line] for line in ERROR_LINES["dry"]]


def test_below_half_humor_the_error_lines_are_plain():
    assistant, _ = make_assistant([b"a"], [])
    assistant = Assistant(
        assistant.body,
        assistant.transcriber,
        assistant.brain,
        assistant.voice,
        humor=20,
    )
    assistant.voice.stream = lambda text: iter([text.encode()])
    played = assistant.body.played
    failing(assistant, RuntimeError("boom"))
    assistant.converse(follow_up_s=4.0)
    assert [chunk.decode() for chunk in played] in [[line] for line in ERROR_LINES["plain"]]


def test_when_the_voice_is_down_too_it_stays_quiet():
    assistant, _ = make_assistant([b"a"], [])

    def down(text):
        raise ConnectionError("no voice")

    assistant.voice.stream = down
    played = assistant.body.played
    failing(assistant, OpenAIError("the voice service is down too"))
    assistant.converse(follow_up_s=4.0)
    assert played == []


def test_an_error_line_that_failed_to_make_is_made_again_later():
    now = [1000.0]
    assistant, _ = make_assistant([b"a"], [], clock=lambda: now[0])
    down = [True]

    def flaky(text):
        if down[0]:
            raise ConnectionError("no network yet")
        return iter([text.encode()])

    assistant.voice.stream = flaky
    assistant.prepare_phrases()
    assistant.say_error()  # the lines failed: nothing to say it with
    down[0] = False
    now[0] += phrases.RETRY_S
    played = assistant.body.played
    failing(assistant, RuntimeError("boom"))
    assistant.converse(follow_up_s=4.0)
    assert played and played[0].decode() in ERROR_LINES["dry"]


def with_phrases(folder, stream):
    assistant, _ = make_assistant([b"a"], [])
    assistant = Assistant(
        assistant.body,
        assistant.transcriber,
        assistant.brain,
        assistant.voice,
        phrase_folder=folder,
    )
    assistant.voice.stream = stream
    return assistant


def test_an_error_line_made_once_plays_after_a_restart_with_the_voice_down(tmp_path):
    first = with_phrases(tmp_path, lambda text: iter([text.encode()]))
    first.prepare_phrases()
    first.phrases.drain()  # every line made, and saved

    def down(text):
        raise ConnectionError("no network since boot")

    later = with_phrases(tmp_path, down)
    played = later.body.played
    failing(later, RuntimeError("boom"))
    later.converse(follow_up_s=4.0)
    assert [chunk.decode() for chunk in played] in [[line] for line in ERROR_LINES["dry"]]


def test_a_line_that_cant_be_saved_still_plays(tmp_path):
    (tmp_path / "phrases").write_text("a file where the folder should be")
    assistant = with_phrases(tmp_path / "phrases", lambda text: iter([text.encode()]))
    played = assistant.body.played
    assert assistant.say("Yes?")
    assert played == [b"Yes?"]


def test_a_line_that_failed_to_make_is_tried_again_when_it_is_needed():
    assistant, _ = make_assistant([], [])
    down = [True]

    def flaky(text):
        if down[0]:
            raise ConnectionError("no network yet")
        return iter([text.encode()])

    assistant.voice.stream = flaky
    played = assistant.body.played
    assert not assistant.say(ASK_PHRASE)  # just after boot, before the network is up
    down[0] = False
    assert assistant.say(ASK_PHRASE) and played == [ASK_PHRASE.encode()]


def test_a_line_stuck_behind_the_voice_keeps_nobody_waiting_long():
    assistant, _ = make_assistant([], [])
    stuck = threading.Event()
    assistant.voice.stream = lambda text: stuck.wait() and iter([text.encode()])
    try:
        t = time.monotonic()
        assert not assistant.say("Yes, Alon?", wait_s=0.05)
        assert time.monotonic() - t < 1
    finally:
        stuck.set()


def test_a_reminders_line_is_made_ahead_and_let_go_once_its_done(tmp_path):
    assistant, _, _, _ = reminding(tmp_path, [None], [], needs_ack=False)
    assistant._reminder_clock.read()
    line = "Stacey, a message from Alon: dinner's at eight."
    assert line in assistant.phrases
    assistant.say_reminders()  # said, and it doesn't wait for "got it": done
    assert line not in assistant.phrases


def test_a_new_voice_makes_its_lines_again(tmp_path):
    from voice_assistant.config import TTSConfig
    from voice_assistant.wiring import phrase_folder

    before = phrase_folder(types.SimpleNamespace(tts=TTSConfig(voice="alloy")), tmp_path)
    assert phrase_folder(types.SimpleNamespace(tts=TTSConfig(voice="onyx")), tmp_path) != before
    assert phrase_folder(types.SimpleNamespace(tts=TTSConfig(voice="alloy")), tmp_path) == before


def test_a_typed_question_is_answered_out_loud():
    assistant, brain = make_assistant([], [], replies=["It's noon."])
    said = assistant.answer_text("what time is it")
    assert brain.asked == ["what time is it"] and said.text == "It's noon."


def test_a_dead_speaker_stops_the_assistant_instead_of_being_one_failed_request():
    assistant, _ = make_assistant([b"a"], ["hello"])
    assistant.body.play = raising(SpeakerError("the speaker stopped playing"))
    with pytest.raises(SpeakerError):
        assistant.converse(follow_up_s=4.0)


def test_did_you_call_me_carries_on_when_answered():
    assistant, brain = make_assistant([b"yes", None], ["yes, what's the weather"])
    assistant.ask_if_called(follow_up_s=4.0)
    assert brain.asked == [f"{ASKED_TAG} yes, what's the weather"]
    assert assistant.body.timeouts[0] == assistant_module.ASK_TIMEOUT_S


def test_did_you_call_me_goes_quiet_on_silence_or_no():
    assistant, brain = make_assistant([None], [])
    assistant.ask_if_called(follow_up_s=4.0)
    assert brain.asked == []
    assistant, brain = make_assistant([b"no"], ["no"], replies=["<skip>"])
    assistant.ask_if_called(follow_up_s=4.0)
    assert brain.forgotten == 1 and assistant.body.timeouts == [assistant_module.ASK_TIMEOUT_S]


HEY_TARS = np.zeros(32000, np.int16)  # Alon's, to speaker ID
ALON = Wake(audio=HEY_TARS)


class FakeSpeakerID:
    def __init__(self, who):
        self.voiceprints = {"alon": None}
        self.who, self.thresholds = who, []

    def identify(self, pcm, threshold=None):
        self.thresholds.append(threshold)
        return self.who

    def describe(self, pcm):
        return self.who, 0.8 if self.who else 0.1, None


def test_a_bare_wake_word_gets_a_greeting_by_name_then_the_question():
    assistant, brain = make_assistant([None, b"q", None], ["what's the weather"])
    assistant.speaker_id = FakeSpeakerID("alon")
    said = []
    assistant.say = lambda text, **kw: said.append(text)
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5, wake=ALON)
    assert said == ["Yes, Alon?"]
    assert assistant.body.timeouts[:2] == [1.5, None]  # short wait, greeting, then the normal wait
    assert assistant.speaker_id.thresholds[0] == assistant.name_threshold  # the greeting uses the wake clip's bar
    assert brain.asked[0].endswith("what's the weather")


def test_greeting_without_a_confident_name_is_just_yes():
    assistant, _ = make_assistant([None, None], [])
    assistant.speaker_id = FakeSpeakerID(None)
    said = []
    assistant.say = lambda text, **kw: said.append(text)
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5, wake=ALON)
    assert said == ["Yes?"]


@pytest.mark.parametrize("greet, heard", [("always", [b"q", None]), ("pause", [None, b"q", None])])
def test_when_speaker_id_fails_on_the_wake_word_the_greeting_is_just_yes(greet, heard):
    assistant, brain, played = greeting_at_once(heard, ["what time is it"])
    assistant.speaker_id.identify = raising(ValueError("shapes (256,) and (192,) not aligned"))
    assistant.voice.stream = lambda text: iter([text.encode()])
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5, greet=greet, wake=ALON)
    assert played[0] == b"Yes?" and brain.asked[0].endswith("what time is it")


def test_when_speaker_id_fails_the_request_is_answered_as_from_an_unknown_voice():
    assistant, brain = make_assistant([b"q", None], ["what time is it"])
    assistant.speaker_id = FakeSpeakerID("alon")
    assistant.speaker_id.describe = raising(ValueError("the voiceprints file is broken"))
    assistant.converse(follow_up_s=4.0)
    assert brain.asked == ["[Speaker: unknown] what time is it"]


def test_a_service_failing_is_a_line_in_the_log_not_a_traceback(capsys):
    assistant, _ = make_assistant([b"a"], [])
    failing(assistant, ServiceError("the deepgram voice failed: TimeoutError()"))
    assistant.converse(follow_up_s=4.0)
    out, err = capsys.readouterr()
    assert "ServiceError" in out and "Traceback" not in err


def test_asking_straight_away_skips_the_greeting():
    assistant, brain = make_assistant([b"q", None], ["what time is it"])
    said = []
    assistant.say = lambda text, **kw: said.append(text)
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5)
    assert said == [] and brain.asked == ["what time is it"]


def found(pcm):
    done = Future()
    done.set_result(pcm)
    return done


def test_what_followed_the_name_goes_to_the_first_request_only():
    """Said in one breath with the name, "what" of "what's new" was read off the mic with it, so it's handed on."""
    assistant, _ = make_assistant([b"q", b"f", None], ["what's new", "and the weather"])
    what = np.ones(1920, np.int16)
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5, wake=Wake(after_name=found(what)))
    leads = assistant.body.leads
    assert leads[0] is what and leads[1:] == [None, None]  # follow-ups come after TARS spoke: nothing to carry


@pytest.mark.parametrize(
    "greet, echo, heard",
    [("always", True, [b"q", None]), ("pause", False, [None, b"q", None]), ("never", False, [b"q", None])],
)
def test_what_followed_the_name_is_heard_while_the_mic_stays_open(greet, echo, heard):
    transcript = "Yes, Alon? What's new" if echo else "what's new"
    assistant, _, _ = greeting_at_once(heard, [transcript], echo=echo)
    what = np.ones(1920, np.int16)
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5, greet=greet, wake=ALON._replace(after_name=found(what)))
    assert assistant.body.leads[0] is what
    assert all(lead is None for lead in assistant.body.leads[1:])  # after a greeting, the mic starts afresh


def test_a_greeting_said_with_the_mic_closed_drops_what_followed_the_name():
    """Without echo cancellation the mic is cleared for "Yes, Alon?": what's heard after it doesn't run on from it."""
    assistant, _, _ = greeting_at_once([b"q", None], ["what's new"])
    assistant.converse(follow_up_s=4.0, greet="always", wake=ALON._replace(after_name=found(np.ones(1920, np.int16))))
    assert assistant.body.leads == [None, None] and assistant.body.pauses >= 1


def test_if_the_greeting_cant_be_said_what_followed_the_name_is_still_heard(capsys):
    assistant, _, _ = greeting_at_once([b"q", None], ["what's new"], echo=True)
    unsaid = Future()
    unsaid.set_exception(RuntimeError("no voice"))
    assistant.phrases.get = lambda text, **kw: unsaid
    what = np.ones(1920, np.int16)
    assistant.converse(follow_up_s=4.0, greet="always", wake=ALON._replace(after_name=found(what)))
    assert assistant.body.leads[0] is what
    assert "couldn't say 'Yes, Alon?'" in capsys.readouterr().out


def test_if_the_name_couldnt_be_timed_the_request_is_heard_from_the_mic(capsys):
    assistant, brain = make_assistant([b"q", None], ["what's new"])
    failed = Future()
    failed.set_exception(RuntimeError("recognizer failed"))
    assistant.converse(follow_up_s=4.0, wake=Wake(after_name=failed))
    assert assistant.body.leads[0] is None and brain.asked == ["what's new"]
    assert "couldn't tell where the name ended" in capsys.readouterr().out


def logged_assistant(tmp_path, utterances, transcripts, replies=(), elsewhere=None):
    journal = Journal(EventLog(tmp_path / "events"))
    assistant, _ = make_assistant(utterances, transcripts, replies, journal=journal, elsewhere=elsewhere)
    journal.wake(AUDIO, 0.9, "answer", "hey tars", 0.9, "", "")
    return assistant, journal.conversations


def test_a_conversation_is_logged_turn_by_turn(tmp_path):
    assistant, convos = logged_assistant(
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


def test_what_was_said_is_written_down_while_tars_answers_not_before(tmp_path, monkeypatch):
    assistant, convos = logged_assistant(tmp_path, [b"q1", None], ["what's the weather"], replies=["Sunny."])
    answering = threading.Event()
    write = convos.add_person_turn

    def slow_disk(*args, **kwargs):
        if not answering.wait(5):
            raise TimeoutError("the reply waited for the log")
        return write(*args, **kwargs)

    monkeypatch.setattr(convos, "add_person_turn", slow_disk)
    assistant.body.on_play = lambda chunks: answering.set()
    assistant.converse(follow_up_s=4.0)
    turns = convos.get(convos.conversations()[0]["id"])["turns"]
    assert [(t["role"], t["text"]) for t in turns] == [("person", "what's the weather"), ("tars", "Sunny.")]


def test_the_first_request_keeps_one_copy_of_its_audio_on_the_wake(tmp_path):
    assistant, convos = logged_assistant(tmp_path, [b"q1", None], ["what's the weather"], replies=["Sunny."])
    assistant.converse(follow_up_s=4.0)
    conv = convos.get(convos.conversations()[0]["id"])
    wake = convos.events.get(conv["wake"]["event_id"])
    assert convos.turn(conv["turns"][0]["id"])["audio"] == wake["utterance_audio"]
    assert wake["transcript"] == "what's the weather" and wake["follow"] == "asked"


def test_did_you_call_me_and_greetings_open_the_thread(tmp_path):
    assistant, convos = logged_assistant(tmp_path, [b"ye", None], ["yes, set a timer"], replies=["Done."])
    assistant.say = lambda text, **kw: True
    assistant.ask_if_called(follow_up_s=4.0)
    turns = convos.get(convos.conversations()[0]["id"])["turns"]
    assert [(t["role"], t["text"]) for t in turns] == [
        ("tars", "Did you call me?"),
        ("person", "yes, set a timer"),
        ("tars", "Done."),
    ]

    assistant, convos = logged_assistant(tmp_path / "2", [None, b"qq", None], ["what time is it"], ["Noon."])
    assistant.speaker_id = FakeSpeakerID("alon")
    assistant.say = lambda text, **kw: True
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5, wake=ALON)
    turns = convos.get(convos.conversations()[0]["id"])["turns"]
    assert [t["text"] for t in turns] == ["Yes, Alon?", "what time is it", "Noon."]
    assert turns[1]["speaker"]["name"] == "Alon"


def test_no_conversation_when_nobody_speaks(tmp_path):
    assistant, convos = logged_assistant(tmp_path, [None], [])
    assistant.say = lambda text, **kw: True
    assistant.ask_if_called(follow_up_s=4.0)
    assistant.body.script = [None, None]
    assistant.speaker_id = FakeSpeakerID(None)
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5)  # "Yes?", then silence
    assert convos.conversations() == []


def test_no_conversation_when_the_request_was_only_noise(tmp_path):
    assistant, convos = logged_assistant(tmp_path, [None, b"qq"], [""])
    assistant.say = lambda text, **kw: True
    assistant.speaker_id = FakeSpeakerID(None)
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5)  # "Yes?", then a noise that transcribes to nothing
    assert convos.conversations() == []


@pytest.mark.parametrize("broken", ["add_person_turn", "add_tars_turn", "start", "add_item"])
def test_a_broken_conversation_log_never_costs_a_reply(tmp_path, monkeypatch, broken):
    assistant, convos = logged_assistant(tmp_path, [b"qq", None], ["what's the weather"], replies=["Sunny."])
    assistant.brain.sent = [SentItem(NOTE, "Forecast", body="Sunny.")]
    monkeypatch.setattr(convos, broken, raising(OSError("disk full")))
    assistant.say_error = lambda: pytest.fail("it said something went wrong")
    assistant.converse(follow_up_s=4.0)
    assert assistant.brain.asked == ["what's the weather"]


def test_a_broken_event_log_never_costs_a_reply(tmp_path, monkeypatch):
    assistant, convos = logged_assistant(tmp_path, [None], [])
    monkeypatch.setattr(convos.events, "set_follow", raising(OSError("database is locked")))
    assistant.say = lambda text, **kw: True
    assistant.ask_if_called(follow_up_s=4.0)  # nobody answers: saving that fails, and nothing else happens


def test_a_conversation_deleted_while_it_goes_on_stays_deleted(tmp_path):
    assistant, convos = logged_assistant(
        tmp_path, [b"q1", b"q2", None], ["hello", "and another thing"], replies=["Hi.", "Go on."]
    )
    brain = assistant.brain
    original = brain.stream_reply

    def delete_after_first(text, request=None):
        if len(brain.asked) == 1:
            convos.delete(convos.conversations()[0]["id"])  # from the web UI, mid-conversation
        return original(text)

    brain.stream_reply = delete_after_first
    assistant.converse(follow_up_s=4.0)
    assert convos.conversations() == [] and convos.store.rows("SELECT * FROM turns") == []


def test_what_tars_sends_is_kept_with_its_reply(tmp_path):
    assistant, convos = logged_assistant(tmp_path, [b"qq", None], ["send me the shopping list"], replies=["Sent it."])
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


def test_sent_for_an_unknown_voice_goes_to_the_household(tmp_path):
    assistant, convos = logged_assistant(tmp_path, [b"qq", None], ["send me a recipe"], replies=["Sent."])
    assistant.speaker_id = FakeSpeakerID(None)
    assistant.brain.sent = [SentItem(NOTE, "Pasta", scope=PERSON, body="Boil water.")]
    assistant.converse(follow_up_s=4.0)
    (item,) = convos.items()
    assert item["scope"] == HOUSEHOLD


def test_sent_items_are_kept_even_if_the_conversation_couldnt_be(tmp_path, monkeypatch):
    assistant, convos = logged_assistant(tmp_path, [b"qq", None], ["send me a recipe"], replies=["Sent."])
    monkeypatch.setattr(convos, "start", raising(OSError("disk full")))
    assistant.brain.sent = [SentItem(NOTE, "Pasta", scope=HOUSEHOLD, body="Boil water.")]
    assistant.converse(follow_up_s=4.0)
    (item,) = convos.items()
    assert item["title"] == "Pasta" and item["conversation_id"] is None


def turns_of(convos):
    (summary,) = convos.conversations()
    return convos.get(summary["id"])["turns"]


def test_each_answer_is_kept_with_how_long_it_took_and_who_wrote_it(tmp_path):
    assistant, convos = logged_assistant(tmp_path, [b"q", None], ["what time is it"], replies=["Late."])
    assistant.brain.answered_by = QUICK
    assistant.converse(follow_up_s=4.0)
    tars = turns_of(convos)[-1]
    assert tars["text"] == "Late." and tars["answered_by"] == QUICK and tars["failed_at"] is None
    assert set(tars["timings"]) == set(TIMINGS) - {"greet"} and tars["timings"]["total"] >= 0  # no greeting here


def test_an_answer_started_during_the_pause_keeps_how_early_it_started(tmp_path):
    assistant, convos = logged_assistant(tmp_path, [["pause", b"q"], None], ["what time is it"], ["Late."])
    record = assistant.body.record

    def still_quiet(**kw):
        recorded = record(**kw)
        time.sleep(0.2)  # the silence goes on after the draft started, until the turn is called over
        return recorded

    assistant.body.record = still_quiet
    assistant.converse(follow_up_s=4.0)
    assert 0 < turns_of(convos)[-1]["timings"]["head_start"] < 1.0


def test_an_answer_asked_only_after_the_turn_logs_no_head_start(tmp_path, capsys):
    assistant, convos = logged_assistant(tmp_path, [b"q", None], ["what time is it"], replies=["Late."])
    assistant.converse(follow_up_s=4.0)
    assert turns_of(convos)[-1]["timings"]["head_start"] < 0.005
    line = next(x for x in capsys.readouterr().out.splitlines() if "TOTAL to first sound" in x)
    assert "started early" not in line


@pytest.mark.parametrize("stage", ["stt", "llm", "tts"])
def test_a_failed_answer_is_kept_with_where_it_failed(tmp_path, stage):
    assistant, convos = logged_assistant(tmp_path, [b"q"], ["what's the weather"], replies=["Sunny."])
    down = OpenAIError(f"{stage} is down")
    if stage == "stt":
        assistant.transcriber = types.SimpleNamespace(session=lambda expect=(): BufferedSession(raising(down)))
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


def test_a_bug_is_kept_as_a_failure_too(tmp_path):
    assistant, convos = logged_assistant(tmp_path, [b"q"], ["hi"])
    assistant.brain.stream_reply = lambda text, request=None: iter(["fine"])
    assistant._speak = raising(KeyError("oops"))
    assistant.converse(follow_up_s=4.0)
    assert turns_of(convos)[-1]["failed_at"] == "other"


def reminding(tmp_path, utterances, transcripts, replies=(), elsewhere=None, **kw):
    """A logged assistant with one reminder due now; returns (assistant, reminders, its id, what was played).
    `elsewhere`: where a reminder due aloud may go instead."""
    assistant, convos = logged_assistant(tmp_path, utterances, transcripts, replies, elsewhere)
    assistant.journal.end_conversation()  # a reminder comes due while TARS waits, not after a wake
    assistant.use_reminders(ReminderTools(Reminders(convos.store)))
    played = assistant.body.played
    assistant.voice.stream = lambda text: iter([text.encode()])
    kw = {"kind": MESSAGE, "text": "dinner's at eight", "for_name": "stacey", "from_name": "alon", **kw}
    rid = assistant.reminders.add(NewReminder(due=time.time(), **kw), WEB)
    return assistant, assistant.reminders, rid, played


def test_got_it_for_one_just_said_counts_though_the_table_didnt_take_its_saying(tmp_path):
    assistant, reminders, rid, _ = reminding(tmp_path, [b"ok", None], ["got it"], replies=["<ack>"])
    marking = reminders.said
    reminders.said = lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full"))
    assistant.say_reminders(follow_up_s=4.0)
    reminders.said = marking
    assert reminders.get(rid)["status"] == ACKNOWLEDGED


def spoken(played):
    return [c.decode() for c in played if c != chime(24_000)]


def test_a_due_reminder_is_said_after_a_chime_and_got_it_acknowledges_it(tmp_path):
    assistant, reminders, rid, played = reminding(tmp_path, [b"ok", None], ["got it"], replies=["<ack>"])
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


def test_muted_its_unmuted_just_for_got_it_then_muted_again(tmp_path):
    assistant, reminders, rid, _ = reminding(tmp_path, [b"ok", None], ["got it"], replies=["<ack>"])
    switched = []

    def button(muted):  # the speakerphone's mute, as start_buttons hands it over
        switched.append(muted)
        assistant.mute(muted)

    assistant.body.set_muted = button
    assistant.mute(True)
    assistant.say_reminders(follow_up_s=4.0)
    assert reminders.get(rid)["status"] == ACKNOWLEDGED
    assert switched == [False, True] and assistant.muted
    assert assistant.body.longest == [assistant_module.ANSWER_ONLY_S]  # "got it", and nothing after


def test_muted_with_nothing_to_wait_for_it_stays_muted(tmp_path):
    assistant, reminders, rid, _ = reminding(tmp_path, [], [], needs_ack=False)
    assistant.body.set_muted = lambda muted: pytest.fail("nothing to listen for")
    assistant.mute(True)
    assistant.say_reminders()
    assert assistant.muted and reminders.get(rid)["status"] == SAID


def test_nobody_answering_leaves_it_waiting_to_be_said_again(tmp_path):
    assistant, reminders, rid, _ = reminding(tmp_path, [None], [])
    assistant.say_reminders()
    r = reminders.get(rid)
    assert (r["status"], r["tries"]) == (WAITING, 1) and r["next_at"] > time.time() + 60
    assert assistant.journal.conversations.conversations() == []  # nothing said back, no conversation


def chat_when_out(tmp_path, stacey_home: bool | None) -> ChatWhenOut:
    """With [presence] and Telegram: Stacey home, out or not known, as the assistant last wrote it."""
    store = EventLog(tmp_path / "events").store
    board = Board(store, People({"Stacey": ["ca:fe:00:00:00:02"]}))
    board.write({"stacey": State(stacey_home, None, time.time())}, [], time.time())
    return ChatWhenOut(Reminders(store), board.is_out, ["Stacey"])


def test_a_reminder_for_someone_out_goes_to_their_chat_and_isnt_said_to_an_empty_room(tmp_path):
    out = chat_when_out(tmp_path, stacey_home=False)
    assistant, reminders, rid, played = reminding(tmp_path, [], [], elsewhere=out)
    assistant.say_reminders()
    assert played == [] and reminders.get(rid)["channel"] == TELEGRAM
    assert assistant.reminders.due() == []  # the Telegram service posts it now


@pytest.mark.parametrize("stacey_home", [True, None])
def test_for_someone_home_or_not_known_its_said_aloud_as_ever(tmp_path, stacey_home):
    here = chat_when_out(tmp_path, stacey_home)
    assistant, reminders, rid, played = reminding(tmp_path, [None], [], elsewhere=here)
    assistant.say_reminders()
    assert spoken(played) == ["Stacey, a message from Alon: dinner's at eight."]
    assert reminders.get(rid)["channel"] == VOICE


def test_a_reply_not_meant_for_tars_doesnt_acknowledge_it(tmp_path):
    assistant, reminders, rid, _ = reminding(tmp_path, [b"chat"], ["pass the salt"], replies=["<skip>"])
    assistant.say_reminders(follow_up_s=4.0)
    assert reminders.get(rid)["status"] == WAITING


def test_one_that_doesnt_wait_is_said_once_without_listening(tmp_path):
    assistant, reminders, rid, played = reminding(tmp_path, [], [], needs_ack=False)
    assistant.say_reminders()  # the recorder has nothing scripted: listening would fail
    assert reminders.get(rid)["status"] == SAID and spoken(played) == [
        "Stacey, a message from Alon: dinner's at eight."
    ]


def test_said_late_it_says_when_it_was_due(tmp_path):
    assistant, _, rid, played = reminding(tmp_path, [None], [])
    assistant.reminders.store.write("UPDATE reminders SET due=due-3600, next_at=next_at-3600 WHERE id=?", (rid,))
    assistant.say_reminders()
    assert spoken(played)[1].startswith("This was due ")


def test_later_i_got_the_message_acknowledges_the_one_waiting(tmp_path):
    assistant, reminders, rid, _ = reminding(tmp_path, [None, b"q", None], ["I got the message"])
    assistant.say_reminders()
    assistant.brain.replies = iter([f"<ack {rid}> Good."])
    assistant.converse(follow_up_s=4.0)
    r = reminders.get(rid)
    assert (r["status"], r["acked_via"]) == (ACKNOWLEDGED, VOICE)
    assert turns_of(assistant.journal.conversations)[-1]["text"] == "Good."


def test_a_bare_ack_in_conversation_is_for_the_one_said_last(tmp_path):
    assistant, reminders, first, _ = reminding(tmp_path, [None, None, b"q", None], ["done"])
    second = reminders.add(NewReminder(REMINDER, "pills", due=time.time()), WEB)
    reminders.store.write("UPDATE reminders SET next_at=? WHERE id=?", (time.time() + 999, second))
    assistant.say_reminders()
    reminders.store.write("UPDATE reminders SET next_at=? WHERE id=?", (time.time() - 1, second))
    assistant.say_reminders()
    assistant.brain.replies = iter(["<ack>"])
    assistant.converse(follow_up_s=4.0)
    assert [reminders.get(i)["status"] for i in (first, second)] == [WAITING, ACKNOWLEDGED]


def test_an_answer_thrown_away_acknowledges_nothing(tmp_path):
    # They paused ("got...") and the draft said <ack>, then carried on ("got a question actually").
    assistant, reminders, rid, _ = reminding(
        tmp_path, [["pause", "resume", b"q"], None], ["got", "got a question actually"],
        replies=["<ack>", "Go ahead."],
    )  # fmt: skip
    assistant.say_reminders(follow_up_s=4.0)
    assert reminders.get(rid)["status"] == WAITING


def test_a_reply_that_acknowledges_a_reminder_and_puts_it_off_puts_it_off(tmp_path):
    # "Remind me again in ten minutes", answering it: Qwen both <ack>s it and snoozes it. Acknowledged first, it
    # couldn't be put off any more, and would never be said again.
    assistant, reminders, rid, _ = reminding(
        tmp_path, [b"later", None], ["remind me again in ten minutes"], replies=["<ack> Pushed to 11:19."]
    )
    stream = assistant.brain.stream_reply

    def putting_it_off(text, request=None):
        assistant.brain.changes = [Change(SNOOZE, id=rid, minutes=10)]
        return stream(text)

    assistant.brain.stream_reply = putting_it_off
    assistant.say_reminders(follow_up_s=4.0)
    r = reminders.get(rid)
    assert r["status"] == SCHEDULED and abs(r["next_at"] - (time.time() + 600)) < 5


def test_a_reminder_set_by_voice_is_made_once_the_answer_is_kept_in_its_conversation(tmp_path):
    assistant, convos = logged_assistant(tmp_path, [b"q", None], ["pasta timer, twelve minutes"])
    tools = ReminderTools(Reminders(convos.store))
    assistant.use_reminders(tools)
    change = Change(ADD, NewReminder(TIMER, "pasta", due=time.time() + 720))
    stream = assistant.brain.stream_reply

    def asking_for_a_timer(text, request=None):
        assistant.brain.changes = [change]
        return stream(text)

    assistant.brain.stream_reply = asking_for_a_timer
    assistant.converse(follow_up_s=4.0)
    (r,) = tools.reminders.active()
    (conversation,) = convos.conversations()
    assert (r["text"], r["set_via"], r["conversation_id"]) == ("pasta", VOICE, conversation["id"])


def holding(tmp_path, who, utterances, transcripts, replies):
    """A logged assistant that hears `who`, with a message held for Stacey until she's heard."""
    assistant, convos = logged_assistant(tmp_path, utterances, transcripts, replies)
    assistant.speaker_id = FakeSpeakerID(who)
    tools = ReminderTools(Reminders(convos.store), voices=lambda: ["stacey"])
    assistant.use_reminders(tools)
    new = NewReminder(MESSAGE, "the plumber called", "stacey", "alon", due=None)
    rid = tools.reminders.add(new, WEB, voices=["stacey"])
    return assistant, tools.reminders, rid


def test_a_message_waiting_for_someone_is_said_after_answering_them_and_their_got_it_counts(tmp_path):
    assistant, reminders, rid = holding(
        tmp_path, "stacey", [b"q", b"ok", None], ["what's the time", "got it"], ["Nine.", "<ack>"]
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
def test_it_waits_while_anyone_else_talks(tmp_path, who):
    assistant, reminders, rid = holding(tmp_path, who, [b"q", None], ["what's the time"], ["Nine."])
    assistant.converse(follow_up_s=4.0)
    assert reminders.get(rid)["tries"] == 0 and reminders.held_for("stacey")


def test_a_ringing_timer_only_chimes_between_its_lines_and_stop_turns_it_off(tmp_path):
    assistant, reminders, rid, played = reminding(
        tmp_path, [None, b"stop", None], ["stop"], replies=["<ack>"], kind=TIMER, text="pasta", for_name=None,
        from_name=None,
    )  # fmt: skip
    assistant.say_reminders()  # the first ring: the chime and the line
    assert spoken(played) == ["Your pasta timer is done."]
    played.clear()
    reminders.store.write("UPDATE reminders SET next_at=? WHERE id=?", (time.time() - 1, rid))
    assistant.say_reminders(follow_up_s=4.0)  # the second: only the chime, then "stop", and its answer
    assert spoken(played) == ["Got it."] and played[0] == chime(24_000)
    r = reminders.get(rid)
    assert (r["status"], r["tries"]) == (ACKNOWLEDGED, 2)


def test_a_reminder_that_cant_be_marked_said_doesnt_stop_the_assistant_or_repeat_at_once(tmp_path, monkeypatch):
    assistant, reminders, _, played = reminding(tmp_path, [None], [], needs_ack=False)
    monkeypatch.setattr(reminders, "said", raising(OSError("disk full")))
    assistant.say_reminders()
    assert spoken(played) == ["Stacey, a message from Alon: dinner's at eight."]
    played.clear()
    assistant.say_reminders()  # the table still says it's due, but it's held: said once, not in a loop
    assert played == [] and not assistant._reminder_clock.is_due()
    assistant, reminders, _ = holding(tmp_path / "2", "stacey", [b"q", None], ["what's the time"], ["Nine."])
    monkeypatch.setattr(reminders, "said", raising(OSError("disk full")))
    assistant.converse(follow_up_s=4.0)  # the held message is said, and the conversation carries on


def test_a_got_it_to_a_reminder_never_lands_on_an_old_wake(tmp_path):
    # TARS asked "Did you call me?" and nobody answered: a wake that wasn't real. Then a reminder, and "got it".
    assistant, reminders, rid, _ = reminding(tmp_path, [b"ok", None], ["got it"], replies=["<ack>"])
    journal = assistant.journal
    wake = journal.events.add_wake(AUDIO, 0.6, "ask", "hey cars", 0.4, "", "")
    journal._wake = wake  # what the trigger leaves behind
    journal.nobody_spoke()
    assistant.say_reminders(follow_up_s=4.0)
    event = journal.events.get(wake)
    assert event["follow"] == "said_nothing" and event["transcript"] is None and not event["utterance_audio"]
    (conversation,) = journal.conversations.conversations()
    assert conversation["wake"] is None and reminders.get(rid)["status"] == ACKNOWLEDGED


def test_a_reminder_the_voice_couldnt_say_isnt_counted_and_is_tried_again_without_a_chime(tmp_path):
    assistant, reminders, rid, played = reminding(tmp_path, [None], [])
    assistant.voice.stream = raising(OSError("offline"))
    assistant.say_reminders()  # no line, so no chime, and no listening for "got it" either
    r = reminders.get(rid)
    assert (r["status"], r["tries"]) == ("scheduled", 0) and assistant._reminder_clock.held(rid)
    assert played == [] and assistant.journal.conversations.conversations() == []
    assistant.voice.stream = lambda text: iter([text.encode()])
    reminders.store.write("UPDATE reminders SET next_at=? WHERE id=?", (time.time() - 1, rid))
    assistant._reminder_clock.hold(rid, 0)
    assistant.say_reminders()  # the voice is back: the chime, then the line
    assert played[0] == chime(24_000) and spoken(played) == ["Stacey, a message from Alon: dinner's at eight."]


def test_when_the_table_fails_the_wake_word_keeps_working(tmp_path, monkeypatch):
    assistant, reminders, _, _ = reminding(tmp_path, [], [])
    assistant._reminder_clock.read()
    assert assistant._reminder_clock.is_due()
    monkeypatch.setattr(reminders, "due", raising(OSError("database disk image is malformed")))
    assistant.say_reminders()
    assert not assistant._reminder_clock.is_due()  # rested, rather than "due" on every block of audio
    monkeypatch.setattr(reminders, "active", raising(OSError("malformed")))
    assistant._reminder_clock.pause(0)
    assistant._reminder_clock.read()
    assert not assistant._reminder_clock.is_due()


def test_a_bare_got_it_later_in_the_conversation_is_for_all_the_reminders_just_said(tmp_path):
    assistant, reminders, first, _ = reminding(
        tmp_path, [b"q", b"ok", None], ["what time is it", "got it"], replies=["Nine.", "<ack>"]
    )
    second = reminders.add(NewReminder(REMINDER, "pills", due=time.time()), WEB)
    assistant.speaker_id = FakeSpeakerID("alon")  # someone known: TARS checks for messages held for them each turn
    assistant.say_reminders(follow_up_s=4.0)
    assert [reminders.get(i)["status"] for i in (first, second)] == [ACKNOWLEDGED, ACKNOWLEDGED]


def greeting_at_once(utterances, transcripts, echo=False):
    """An assistant that has just heard "hey TARS" from Alon, greeting at once, with or without echo cancellation."""
    assistant, brain = make_assistant(utterances, transcripts)
    assistant.speaker_id = FakeSpeakerID("alon")
    assistant.body.hears_while_speaking = echo
    assistant._woke_at = time.monotonic()
    return assistant, brain, assistant.body.plays


def test_greeting_at_once_says_yes_with_no_wait_for_a_pause():
    assistant, brain, played = greeting_at_once([b"q", None], ["what's the weather"])
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5, greet="always", wake=ALON)
    assert len(played) == 2  # "Yes, Alon?", then the answer
    assert assistant.body.timeouts[0] is None  # no waiting to see whether more follows the wake word
    assert assistant.body.pauses >= 1  # without echo cancellation, the mic closes while it's said
    assert brain.asked[0].endswith("what's the weather") and "greet" in assistant.timings


def test_with_echo_cancellation_the_greeting_is_said_with_the_mic_open_and_its_trace_taken_off():
    assistant, brain, played = greeting_at_once([b"q", None], ["Yes, Alon? What's the weather"], echo=True)
    assistant.converse(follow_up_s=4.0, greet="always", wake=ALON)
    assert played[0] and assistant.body.pauses == 1  # the greeting with the mic open; only the answer closes it
    assert brain.asked[0].endswith("] What's the weather")  # what got through of "Yes, Alon?" is gone


def test_if_all_it_heard_was_its_own_greeting_it_keeps_listening():
    assistant, brain, _ = greeting_at_once([b"q", b"q", None], ["yes alon", "what time is it"], echo=True)
    heard = []
    assistant.journal.heard = lambda pcm, text, name, score, embedding, first: heard.append((text, name, embedding))
    assistant.converse(follow_up_s=4.0, greet="always", wake=ALON)
    assert len(brain.asked) == 1 and brain.asked[0].endswith("what time is it")
    assert heard[0] == ("", None, None)


def test_greeting_at_once_listens_once_when_nobody_speaks():
    assistant, brain, _ = greeting_at_once([None, None], [])
    assistant.converse(follow_up_s=4.0, greet="always", wake=ALON)
    assert assistant.body.timeouts == [None] and brain.asked == []  # one wait, not two


def test_never_greeting_waits_quietly():
    assistant, _, played = greeting_at_once([None], [])
    assistant.converse(follow_up_s=4.0, greet_after_s=1.5, greet="never", wake=ALON)
    assert played == [] and assistant.body.timeouts == [None]


def test_the_stop_button_cuts_a_reminder_and_acknowledges_it_without_listening(tmp_path):
    assistant, reminders, rid, _ = reminding(tmp_path, [b"ok", None], ["got it"])
    line = "Stacey, a message from Alon: dinner's at eight."

    def play(chunks):
        if line.encode() in chunks:  # pressed while its line plays
            assistant.stop()

    assistant.body.on_play = play
    assistant.say_reminders(follow_up_s=4.0)
    r = reminders.get(rid)
    assert (r["status"], r["acked_via"]) == (ACKNOWLEDGED, VOICE)
    assert assistant.body.timeouts == []  # nobody was listened for


def test_stopped_before_its_line_a_reminder_isn_t_acknowledged_unheard(tmp_path):
    assistant, reminders, rid, played = reminding(tmp_path, [b"ok", None], ["got it"])
    assistant.body.on_play = lambda chunks: assistant.stop()  # pressed on the chime
    assistant.say_reminders(follow_up_s=4.0)
    assert played == [chime(24_000)]  # its line was never said
    assert reminders.get(rid)["status"] != ACKNOWLEDGED and reminders.get(rid)["tries"] == 0
    assert assistant.body.timeouts == []


def test_the_stop_button_turns_a_ringing_timer_off_on_its_chime(tmp_path):
    assistant, reminders, rid, _ = reminding(
        tmp_path, [None, None], [], kind=TIMER, text="pasta", for_name=None, from_name=None
    )
    assistant.say_reminders()  # the first ring: the chime and the line; nobody answers
    reminders.store.write("UPDATE reminders SET next_at=? WHERE id=?", (time.time() - 1, rid))
    assistant.body.on_play = lambda chunks: assistant.stop()
    assistant.say_reminders(follow_up_s=4.0)  # the second: only the chime, and the button pressed on it
    r = reminders.get(rid)
    assert (r["status"], r["tries"]) == (ACKNOWLEDGED, 2)
    assert assistant.body.timeouts == [assistant.ack_window_s]  # only the first ring listened
