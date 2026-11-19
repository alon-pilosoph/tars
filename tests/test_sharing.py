"""What the rooms share (P4: one assistant per body, each on its own thread), hammered from several threads at once:
nothing said twice, nothing lost, nothing read half-changed."""

import threading
import time
import types
from concurrent.futures import Future

import numpy as np
import pytest

THREADS = 4


def together(*work, times: int = 1) -> list:
    """Runs each of `work` on its own thread, all started at once, `times` over; what each returned, and raises the
    first error any of them hit."""
    barrier, results, errors = threading.Barrier(len(work) * times), [], []

    def run(job):
        barrier.wait()
        try:
            results.append(job())
        except Exception as e:  # noqa: BLE001 - raised below, in the test's thread
            errors.append(e)

    threads = [threading.Thread(target=run, args=(job,)) for job in work * times]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    if errors:
        raise errors[0]
    return results


def test_speaker_id_matches_while_voiceprints_are_swapped(tmp_path):
    from voice_assistant.speaker import SpeakerID

    sid = SpeakerID.__new__(SpeakerID)  # without the model: embed is a stand-in
    sid._voiceprints_path, sid.threshold, sid._lock = tmp_path / "voiceprints.npz", 0.5, threading.Lock()
    sid.prints, sid._from_clusters, sid._loaded_mtime = {}, set(), None
    sid.embed = lambda audio: np.ones(4) / 2
    rng = np.random.default_rng(0)

    def match():
        for _ in range(200):
            sid.describe(b"\1\0" * 16000)

    def rename():
        for i in range(50):
            sid.set_cluster_voiceprints({(f"p{j}", ""): rng.normal(size=4) for j in range(i % 7)})

    together(match, match, rename)
    assert sid.names() == sorted(sid.voiceprints) or set(sid.names()) == set(sid.voiceprints)


def test_phrases_make_each_line_once_however_many_rooms_ask():
    from voice_assistant.phrases import Phrases

    made = []
    voice = types.SimpleNamespace(stream=lambda text: made.append(text) or iter([text.encode()]))
    phrases = Phrases(voice)
    lines = [f"line {i}" for i in range(20)]
    results = together(lambda: [phrases.get(line).result(5) for line in lines], times=THREADS)
    assert sorted(made) == sorted(lines)  # each made once
    assert all(r == [[line.encode()] for line in lines] for r in results)


def test_an_announcement_is_taken_by_one_room_only_and_put_back_with_its_time():
    from voice_assistant.assistant import Announcements

    now = [100.0]
    desk = Announcements(clock=lambda: now[0])
    for i in range(200):
        desk.add(f"line {i}")
    taken = together(desk.take, times=THREADS)
    everything = [line for lines in taken for line in lines]
    assert sorted(everything) == sorted(f"line {i}" for i in range(200))  # each once, none lost
    first = next(lines for lines in taken if lines)
    desk.add("new")
    desk.take()  # another room's take: the first room's lines' times are still known
    now[0] += 1
    desk.put_back(first[:1], retry_in_s=0)
    assert desk._lines[0] == (100.0, first[0])  # put back with when it was added, not now


def test_a_due_skill_is_handed_to_one_room():
    from voice_assistant.skills import Clock

    clock = Clock.__new__(Clock)
    clock._lock, clock._due = threading.Lock(), [f"skill {i}" for i in range(100)]
    taken = together(clock.take, times=THREADS)
    assert sorted(item for items in taken for item in items) == sorted(f"skill {i}" for i in range(100))


def test_reminders_said_and_acknowledged_from_several_rooms_stay_consistent(log):
    from voice_assistant.reminders import REMINDER, NewReminder, Reminders

    reminders = Reminders(log.store, repeat_every_s=60, max_tries=30)
    rid = reminders.add(NewReminder(REMINDER, "stretch", due=time.time() + 60, needs_ack=True), set_via="voice")
    together(lambda: reminders.said(rid), times=10)
    assert reminders.get(rid)["tries"] == 10  # every saying counted, none lost
    acks = together(lambda: reminders.ack(rid, "Alon", "voice"), times=THREADS)
    assert acks.count(True) == 1 and reminders.get(rid)["status"] == "acknowledged"  # acknowledged once


def test_the_voices_warm_connections_serve_several_rooms_within_the_services_limit():
    from voice_assistant.tts import WarmSockets

    class Fake(WarmSockets):
        def __init__(self):
            super().__init__()
            self.at_once = self.most = self.opened = 0
            self.lock = threading.Lock()

        def _connect(self):
            with self.lock:
                self.opened += 1
            return types.SimpleNamespace(close=lambda: None)

        def _send(self, ws, text):
            ws.text = text

        def _audio(self, ws):
            with self.lock:
                self.at_once += 1
                self.most = max(self.most, self.at_once)
            time.sleep(0.01)
            with self.lock:
                self.at_once -= 1
            yield ws.text.encode()

    voice = Fake()
    said = together(lambda: [b"".join(voice.stream(f"s{i}")) for i in range(15)], times=THREADS)
    assert all(s == [f"s{i}".encode() for i in range(15)] for s in said)
    assert voice.most <= WarmSockets.AT_ONCE


def test_two_rooms_journals_keep_two_conversations_in_one_database(log):
    from voice_assistant.journal import Journal

    def a_conversation(name):
        journal = Journal(log)
        for i in range(5):
            journal.heard(b"\0\0" * 1600, f"{name} asks {i}", name, 0.9, None, first=i == 0)
            journal.answered(f"to {name} {i}", [], asker=name)
        journal.flush()
        return journal.conversation_id

    ids = together(lambda: a_conversation("alon"), lambda: a_conversation("stacey"))
    assert len(set(ids)) == 2
    for cid in ids:
        texts = [t["text"] for t in journal_turns(log, cid)]
        names = {word for text in texts for word in text.split() if word in ("alon", "stacey")}
        assert len(texts) == 10 and len(names) == 1  # its own person's turns only, none of the other room's


def journal_turns(log, conversation_id):
    from voice_assistant.conversations import ConversationLog

    return ConversationLog(log).get(conversation_id)["turns"]


def test_two_bodies_confirming_wakes_at_once_each_get_their_own_audio_back():
    from voice_assistant.bodylink import RemoteTrigger

    class SlowChecker:
        last_audio = after_name = None

        def confirm(self, audio, score):
            self.last_audio = audio
            time.sleep(0.01)  # another body's confirm would overwrite it here, unlocked
            found = Future()
            found.set_result(audio)
            self.after_name = found
            return "answer"

    checker = SlowChecker()

    def a_body(level):
        link = types.SimpleNamespace(
            hello=types.SimpleNamespace(wake="model", device=f"b{level}", kind="atom", version="")
        )
        trigger = RemoteTrigger(link, checker)
        for _ in range(20):
            audio = np.full(1280, level, np.int16)
            with trigger._checking:
                trigger.checker.confirm(audio, 0.9)
                got = trigger.checker.last_audio
            assert got[0] == level

    together(lambda: a_body(1), lambda: a_body(2), lambda: a_body(3))


@pytest.mark.parametrize("module", ["voice_assistant.speaker", "voice_assistant.assistant", "voice_assistant.bodylink"])
def test_the_audited_modules_import(module):
    __import__(module)
