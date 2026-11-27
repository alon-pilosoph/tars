"""A phone call's conversation (calls.py): taking turns, being talked over, and what's kept of it."""

import json
import threading
import time
import types

import numpy as np
import pytest

from voice_assistant import calls
from voice_assistant.assistant import Assistant
from voice_assistant.bodylink import BodyConnection, Hello
from voice_assistant.calls import CallConversation, echoes, goes_on, says_bye, takes_the_turn
from voice_assistant.journal import Journal

from .conftest import RecordingBrain


def until(check, wait_s=5.0):
    deadline = time.monotonic() + wait_s
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


class Brain(RecordingBrain):
    """Replies that can be held back, and remembers whether a tool was allowed to act."""

    def __init__(self, replies=()):
        super().__init__(replies)
        self.hold = threading.Event()
        self.hold.set()
        self.acted = []

    def stream_reply(self, text, request=None):
        self.asked.append(text)
        self.requests.append(request)
        reply = next(self.replies, "Okay.")

        def pieces():
            self.hold.wait(5)
            if reply.startswith("ACT "):
                self.acted.append(request.may_act(5))
            yield reply.removeprefix("ACT ")

        return pieces()

    def interrupt(self):
        self.hold.set()


class Phone:
    """The caller's end. `fast`: what's said has played as soon as it's sent; otherwise marks come back by hand."""

    def __init__(self):
        self.sent, self.plays, self.marks, self.fast = [], 0, [], False
        self.link = BodyConnection(Hello("web-1", kind="phone", web=True, person="robin"), self._take, lambda: None)
        self.link.awake = True
        self.bytes = 0

    def _take(self, message):
        if isinstance(message, bytes):
            self.bytes += len(message)
            return
        said = json.loads(message)
        self.sent.append(said["type"])
        if said["type"] == "play":
            self.plays += 1
        elif said["type"] == "mark":
            self.marks.append(said["id"])
            if self.fast:
                self.played(1)
        elif said["type"] == "stop":
            self.marks.clear()

    def played(self, n=None):
        for mark in self.marks[:n]:
            self.link.take(json.dumps({"type": "played", "id": mark, "cut": False}))
        del self.marks[:n]


class A:
    """A call under way."""

    def __init__(self, replies=(), log=None, seconds=1.0, spare=None):
        self.brain, self.phone, self.ears, self.asked_for = Brain(replies), Phone(), [], []
        audio = np.full(int(24000 * seconds), 3000, np.int16).tobytes()
        self.voice = types.SimpleNamespace(sample_rate=24000, stream=lambda t: iter([audio]), warm=lambda: None)
        self.journal = Journal(log, room="phone") if log else None
        self.assistant = Assistant(None, None, self.brain, self.voice, web=True, person="robin", journal=self.journal)
        self.assistant.prepare_phrases = lambda: None

        def listen(event):
            ear = types.SimpleNamespace(event=event, feed=lambda pcm: None, close=lambda: None)
            ear.expect = self.asked_for.append
            self.ears.append(ear)
            return ear

        self.call = CallConversation(self.assistant, self.phone.link, listen, (lambda: spare) if spare else None)
        self.thread = threading.Thread(target=self.call.hold, daemon=True)
        self.thread.start()

    def flux(self, kind, text="", sure=1.0):
        self.ears[-1].event(kind, text, sure)

    def says(self, text):
        self.flux("StartOfTurn", text.split()[0])
        self.flux("EagerEndOfTurn", text)
        self.flux("EndOfTurn", text)

    def speaks(self, n):
        """Waits until TARS is saying its `n`th thing, all of it on its way."""
        until(lambda: self.phone.plays >= n and self.phone.marks and self.call._now and self.call._now.made)
        time.sleep(0.08)

    def finished(self):
        self.phone.played()
        until(lambda: self.call._now is None)

    def greeted(self):
        self.speaks(1)
        self.finished()
        return self

    def ends(self):
        self.phone.link.lost()
        self.thread.join(8)
        assert not self.thread.is_alive()


@pytest.fixture
def a():
    made = []

    def make(*args, **kw):
        made.append(A(*args, **kw))
        return made[-1].greeted()

    yield make
    for one in made:
        one.ends()


def test_it_greets_then_answers_and_is_ready_for_the_next_without_a_wake(a):
    it = a(["Sunny.", "Warm."])
    it.says("what's the weather")
    it.speaks(2)
    it.finished()
    it.says("and tomorrow")
    it.speaks(3)
    assert it.brain.remembered == ["Hey Robin."]
    assert it.brain.asked == ["what's the weather", "and tomorrow"]
    request = it.brain.requests[0]
    assert request.by_web and not request.by_voice and request.speaker == "robin"


def test_the_answer_is_started_early_but_neither_heard_nor_acted_on_until_they_are_done(a):
    it = a(["ACT Done."])
    it.flux("StartOfTurn", "turn")
    it.flux("EagerEndOfTurn", "turn on the light")
    until(lambda: it.brain.asked)
    time.sleep(0.2)
    assert it.phone.plays == 1 and it.brain.acted == []
    it.flux("EndOfTurn", "turn on the light")
    it.speaks(2)
    assert it.brain.acted == [True] and len(it.brain.asked) == 1


def test_carrying_on_throws_the_early_answer_away_and_nothing_is_done(a):
    it = a(["ACT Done.", "The other one."])
    it.flux("StartOfTurn", "turn")
    it.flux("EagerEndOfTurn", "turn on the light")
    until(lambda: it.brain.asked)
    it.flux("TurnResumed", "turn on the light no")
    it.flux("EndOfTurn", "turn on the light no wait the other one")
    it.speaks(2)
    assert it.brain.acted == [False] and it.brain.forgotten == 1
    assert it.brain.asked[-1] == "turn on the light no wait the other one"


def test_talking_over_it_stops_it_and_only_what_was_heard_is_kept(a, log):
    it = a(["One two three four. Five six seven eight. Nine ten.", "Sure."], log=log, seconds=0.8)
    it.says("count for me")
    it.speaks(2)
    it.phone.played(2)  # two marks of 0.4 s: the first sentence
    time.sleep(0.1)
    it.flux("StartOfTurn", "stop")
    until(lambda: "stop" in it.phone.sent)
    it.flux("EndOfTurn", "stop that now")
    it.speaks(3)
    (kept,) = [r for r in it.brain.remembered if isinstance(r, tuple)]
    assert kept[0] == "count for me" and kept[1].startswith("One two three four.")
    assert kept[1].endswith("…") and "seven" not in kept[1]
    assert it.brain.asked[-1] == "[Interrupted you] stop that now"


def test_one_unsure_word_only_pauses_it_and_it_goes_on_from_the_word_it_was_on(a):
    it = a(["One two three four. Five six seven eight."], seconds=0.8)
    it.says("count for me")
    it.speaks(2)
    it.phone.played(2)
    time.sleep(0.1)
    sent = it.phone.bytes
    it.flux("StartOfTurn", "banana", 0.3)
    until(lambda: "stop" in it.phone.sent)
    assert it.call._now.paused_at is not None
    it.flux("EndOfTurn", "banana", 0.3)
    it.speaks(3)
    again = (it.phone.bytes - sent) / 2 / 24000
    assert 0.3 < again < 1.1  # from the word it was on (it all lasts 1.6 s), however slow this machine is
    it.finished()
    assert len(it.brain.asked) == 1 and it.call.counts["resumed"] == 1
    assert it.brain.forgotten == 0


def test_a_pause_nothing_comes_of_ends_by_itself(a, monkeypatch):
    monkeypatch.setattr(calls, "RESUME_S", 0.2)
    it = a(["One two three four."])
    it.says("count for me")
    it.speaks(2)
    it.flux("StartOfTurn", "banana", 0.3)
    until(lambda: it.phone.plays == 3)


def test_a_word_flux_is_sure_of_takes_the_turn(a):
    it = a(["One two three four.", "Louder."])
    it.says("count for me")
    it.speaks(2)
    it.says("louder")
    it.speaks(3)
    assert it.brain.asked[-1].endswith("louder")


def test_the_choices_it_offers_are_what_flux_listens_for(a):
    it = a(["Ten or eleven?"])
    it.says("the news")
    it.speaks(2)
    it.finished()
    until(lambda: it.asked_for and "eleven" in it.asked_for[0])


def test_early_answers_are_limited(a):
    it = a()
    it.flux("StartOfTurn", "a")
    for n in range(8):
        it.flux("EagerEndOfTurn", f"words {n}")
        time.sleep(0.15)  # long enough for the brain to be asked
        it.flux("TurnResumed", f"words {n} and")
    time.sleep(0.4)
    it.flux("EndOfTurn", "words at last")
    until(lambda: it.brain.asked and it.brain.asked[-1] == "words at last")
    assert len(it.brain.asked) == calls.EARLY_TRIES + 1


def test_a_slow_answer_gets_a_word_and_then_the_answer(a, monkeypatch):
    monkeypatch.setattr(calls, "SLOW_S", 0.2)
    it = a(["The answer."], seconds=0.5)
    it.assistant.phrases.get(calls.ONE_MOMENT).result(5)
    it.brain.hold.clear()
    it.says("something slow")
    until(lambda: it.call._now and it.call._now.said == ["One moment."])
    it.brain.hold.set()
    until(lambda: it.call._now.spoken == "One moment. The answer." and it.call._now.made)
    assert [w for w, _ in it.call._now.words] == ["One", "moment.", "The", "answer."]
    ends = [e for _, e in it.call._now.words]
    assert ends == sorted(ends) and ends[-1] == pytest.approx(1.0, abs=0.05)


class Spare(Brain):
    def carry_over(self, other):
        self.from_brain = other

    def interrupt(self):
        pass


def slow(a, monkeypatch, spare, **kw):
    monkeypatch.setattr(calls, "DETACH_S", 0.1)
    it = a(["Found it."], spare=spare, **kw)
    it.assistant.phrases.get(calls.FOUND).result(5)
    it.brain.hold.clear()
    it.says("look something up")
    until(lambda: it.brain.asked and it.call._now and it.call._now.went)
    time.sleep(0.3)
    return it


def test_something_slow_goes_on_while_they_talk_and_is_said_when_it_is_quiet(a, monkeypatch):
    spare = Spare(["Meanwhile."])
    it = slow(a, monkeypatch, spare)
    it.says("what time is it")
    it.speaks(2)
    assert spare.from_brain is it.brain and spare.asked == ["what time is it"]
    assert spare.remembered == [("look something up", calls.STILL_ON_IT)] and it.brain.forgotten == 0
    it.brain.hold.set()
    it.finished()
    it.speaks(3)
    assert it.call._now.spoken == "Found it."
    it.finished()
    assert spare.remembered[-1] == "Found it."


def test_calling_it_off_stops_the_slow_thing(a, monkeypatch):
    spare = Spare()
    it = slow(a, monkeypatch, spare)
    it.says("never mind that one")
    until(lambda: it.brain.forgotten == 1)
    assert not it.call._later and not spare.asked


def test_hanging_up_on_something_slow_sends_what_it_found(a, monkeypatch, log):
    it = slow(a, monkeypatch, Spare(), log=log)
    kept = []
    monkeypatch.setattr(it.journal, "sent", lambda items, who: kept.append((items, who)))
    it.ends()
    it.brain.hold.set()
    until(lambda: kept)
    ((note,), who) = kept[0]
    assert note.body == "Found it." and note.title == "look something up" and who == "robin"


def along(sentences_heard, broken=False):
    """A voice that goes on in one breath, saying when each word ends."""

    def speak(first, more):
        if broken:
            raise calls.ServiceError("no")
        sentence = first
        at = 0.0
        while sentence is not None:
            sentences_heard.append(sentence)
            for word in sentence.split():
                at += 0.2
                yield [(word, at)]
                yield np.full(4800, 3000, np.int16).tobytes()
            sentence = more()

    return speak


@pytest.mark.parametrize("broken", [False, True])
def test_a_voice_that_can_goes_on_in_one_breath_or_a_sentence_at_a_time(a, broken):
    it = a(["One two. Three four five."], seconds=0.4)
    breath = []
    it.voice.along = along(breath, broken)
    it.says("count")
    it.speaks(2)
    assert breath == ([] if broken else ["One two.", "Three four five."])
    assert [w for w, _ in it.call._now.words] == ["One", "two.", "Three", "four", "five."]
    assert it.call._now.words[-1][1] == pytest.approx(0.8 if broken else 1.0, abs=0.02)


def test_interrupted_before_a_word_it_is_told_nothing_was_heard(a):
    it = a(["Slow.", "Fine."])
    it.brain.hold.clear()
    it.says("look something up")
    until(lambda: it.brain.asked)
    it.says("never mind what I said")
    it.speaks(2)
    assert ("look something up", "…") in it.brain.remembered
    assert it.brain.asked[-1] == "[Interrupted you before you answered] never mind what I said"


@pytest.mark.parametrize("text", ["mm-hm", "Yeah.", "uh huh", "okay", "a long answer"])
def test_listening_noises_and_its_own_echo_do_not_stop_it(a, text):
    it = a(["This is a long answer about things."])
    it.says("tell me")
    it.speaks(2)
    it.says(text)
    time.sleep(0.3)
    assert "stop" not in it.phone.sent and len(it.brain.asked) == 1


def test_a_yeah_that_goes_on_takes_the_turn(a):
    it = a(["This is a long answer.", "Right."])
    it.says("tell me")
    it.speaks(2)
    it.flux("StartOfTurn", "yeah")
    time.sleep(0.15)
    assert "stop" not in it.phone.sent
    it.flux("Update", "yeah but what about tomorrow")
    until(lambda: "stop" in it.phone.sent)
    it.flux("EndOfTurn", "yeah but what about tomorrow")
    it.speaks(3)


def test_a_yeah_that_goes_on_after_it_finished_is_answered(a):
    it = a(["This is a long answer.", "Right."])
    it.says("tell me")
    it.speaks(2)
    it.flux("StartOfTurn", "yeah")
    time.sleep(0.15)
    it.finished()
    it.flux("EndOfTurn", "yeah and turn off the light")
    it.speaks(3)


def test_yeah_to_its_question_is_the_answer(a):
    it = a(["Shall I?", "Done."])
    it.says("the boiler")
    it.speaks(2)
    it.says("yeah")
    it.speaks(3)
    assert it.brain.asked[-1].endswith("yeah")


def test_what_it_set_is_kept_if_they_heard_it_and_not_if_they_heard_nothing(a, monkeypatch):
    it = a(["Timer set for ten minutes.", "ok"], seconds=0.1)
    applied = []
    monkeypatch.setattr(it.assistant, "_change_reminders", lambda changes, who: applied.append(list(changes)))
    it.brain.changes = ["timer"]
    it.says("ten minute timer")
    it.speaks(2)
    time.sleep(0.6)
    it.says("thanks for that one")
    until(lambda: ["timer"] in applied)


def test_goodbye_hangs_up_once_it_has_been_heard(a):
    it = a()
    it.says("okay bye")
    it.speaks(2)
    assert "hangup" not in it.phone.sent
    it.phone.played()
    until(lambda: "hangup" in it.phone.sent)
    assert it.brain.asked == []


def test_a_lost_stream_is_opened_again_and_the_call_goes_on_until_it_keeps_failing(a):
    it = a(["Yes."])
    it.flux("Failed", "gone")
    until(lambda: len(it.ears) == 2)
    it.says("are you there")
    it.speaks(2)
    for n in range(calls.FLUX_TRIES):
        it.flux("Failed", "gone")
        if n < calls.FLUX_TRIES - 1:
            until(lambda n=n: len(it.ears) == 3 + n)
    it.speaks(3)
    it.phone.played()
    until(lambda: "hangup" in it.phone.sent)


def test_a_failed_answer_does_not_end_the_call(a, monkeypatch):
    it = a(["Fine."])
    real = it.brain.stream_reply
    monkeypatch.setattr(it.brain, "stream_reply", lambda *_: (_ for _ in ()).throw(RuntimeError("down")))
    it.says("one")
    time.sleep(0.4)
    monkeypatch.setattr(it.brain, "stream_reply", real)
    it.says("two")
    it.speaks(2)
    assert it.brain.asked == ["two"]


def test_silence_ends_the_call(a, monkeypatch):
    it = a()
    monkeypatch.setattr(calls, "QUIET_S", 0.2)
    it.speaks(2)
    it.phone.played()
    until(lambda: "hangup" in it.phone.sent)


def test_the_call_is_in_the_conversations(a, log, convos):
    it = a(["Sunny."], log=log)
    it.says("weather")
    it.speaks(2)
    it.finished()
    it.ends()
    (kept,) = convos.conversations()
    assert [t["text"] for t in kept["turns"]] == ["Hey Robin.", "weather", "Sunny."]
    assert not kept["turns"][1].get("audio")


def test_who_is_taking_the_turn():
    assert goes_on("Mm-hmm.") and goes_on("okay, yeah") and not goes_on("okay stop")
    assert echoes("in ten minutes", "It starts in ten minutes.") and not echoes("ten hours", "in ten minutes")
    assert not takes_the_turn("", "x") and takes_the_turn("wait", "It starts.")
    assert takes_the_turn("eleven", "Ten or eleven?") and not takes_the_turn("do you want ten", "Do you want ten?")
    assert says_bye("Okay, bye TARS") and says_bye("hang up please") and says_bye("that's all, thanks")
    assert not says_bye("thanks") and not says_bye("never mind") and not says_bye("say bye to mom")


def test_a_message_for_someone_else_waits_for_approval_and_their_own_reminder_does_not(a, monkeypatch):
    from voice_assistant.reminders import ADD, Change, NewReminder

    it = a(["Okay."])
    asked, applied, done = [], [], []
    it.call._approvals = types.SimpleNamespace(
        check=lambda person, heard: None, ask=lambda *args: asked.append(args) or "asked"
    )
    tools = types.SimpleNamespace(apply=lambda change, from_name: done.append(change))
    it.assistant.reminder_tools = tools
    monkeypatch.setattr(it.assistant, "_reread_reminders", lambda: None)
    monkeypatch.setattr(it.assistant, "_change_reminders", lambda changes, who: applied.extend(changes))
    mine = Change(ADD, NewReminder("reminder", "milk", for_name="Robin"))
    theirs = Change(ADD, NewReminder("message", "running late", for_name="Sam"))
    it.brain.changes = [mine, theirs]
    it.says("tell sam I'm late and remind me about milk")
    it.speaks(2)
    it.finished()
    until(lambda: asked)
    assert applied == [mine] and done == []
    person, _, what, do = asked[0]
    assert person == "robin" and "message for Sam" in what and "running late" in what
    assert do() == "Done." and done == [theirs]


def test_every_request_on_a_call_goes_by_the_approvals_even_with_nobody_to_ask(a):
    from voice_assistant.approvals import NO_CHAT
    from voice_assistant.toolbox import ITS_OWN, Tool, Turn

    it = a(["Okay."])
    it.says("change something")
    until(lambda: it.brain.requests)
    request = it.brain.requests[0]
    tool = Tool({"name": "skills"}, ITS_OWN, lambda args, turn: "done")
    until(lambda: it.call._now and it.call._now.went)
    assert request.approve(tool, {"action": "save"}, Turn(request)) == NO_CHAT


def test_how_soon_it_answered_counts_the_wait_to_be_sure_they_were_done(a):
    it = a(["Sunny."])
    it.ears[-1].waited = 1.5
    it.says("weather")
    it.speaks(2)
    it.finished()
    until(lambda: it.call.totals)
    assert 1.5 <= it.call.totals[0] < 3
