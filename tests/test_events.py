"""The event log: storage, automatic labels, clusters, and what the trigger and assistant write into it."""

import sqlite3
import time
import types

import numpy as np
import pytest

from voice_assistant import events
from voice_assistant.audio import BLOCK_SAMPLES, SAMPLE_RATE
from voice_assistant.events import (
    ASKED,
    NEAR_MISS,
    NOT_FOR_US,
    NOT_REAL,
    REAL,
    SAID_NOTHING,
    EventLog,
)
from voice_assistant.journal import Journal
from voice_assistant.store import TABLES
from voice_assistant.verify import ANSWER, IGNORE, NEAR_QUIET_S, VerifiedTrigger

from .conftest import AUDIO, FakeTrigger, SilentMic, make_assistant, speech

pytestmark = pytest.mark.usefixtures("no_tts")


def test_automatic_labels_follow_what_happened_next(log):
    asked = log.add_wake(AUDIO, 0.9, "answer", "hey tars", 0.9)
    log.set_follow(asked, ASKED)
    unanswered_ask = log.add_wake(AUDIO, 0.7, "ask", "hey cars", 0.1)
    log.set_follow(unanswered_ask, SAID_NOTHING)
    rejected = log.add_wake(AUDIO, 0.6, "ignore", "hey there", 0.0)
    overheard = log.add_wake(AUDIO, 0.8, "answer", "hey tars", 0.8)
    log.set_follow(overheard, NOT_FOR_US)
    label = {e["id"]: e["auto_label"] for e in log.events()}
    assert label[asked] == REAL and label[unanswered_ask] == NOT_REAL
    assert label[rejected] == NOT_REAL and label[overheard] == NOT_REAL


def test_a_near_miss_right_before_a_real_wake_counts_as_a_missed_hey_tars(log, monkeypatch):
    clock = iter([1000.0, 1003.0, 2000.0])
    monkeypatch.setattr(events.time, "time", lambda: next(clock))
    missed = log.add_near_miss(AUDIO, 0.4)  # t=1000
    log.add_wake(AUDIO, 0.9, "answer", "hey tars", 0.9)  # t=1003: they said it again, louder
    lonely = log.add_near_miss(AUDIO, 0.35)  # t=2000, nothing after it
    label = {e["id"]: e["auto_label"] for e in log.events()}
    assert label[missed] == REAL and label[lonely] is None
    # The same whichever rows are asked for: only near-misses, or a page that ends at the near-miss.
    assert {e["id"]: e["auto_label"] for e in log.events(kind=NEAR_MISS)} == {missed: REAL, lonely: None}
    assert [e["auto_label"] for e in log.events(limit=3)[-1:]] == [REAL]


def test_a_persons_label_wins_over_the_automatic_one(log):
    event = log.add_wake(AUDIO, 0.6, "ignore", "hey bars", 0.1)
    log.set_label(event, REAL)  # it WAS "hey TARS", the check got it wrong
    (row,) = log.events()
    assert row["auto_label"] == NOT_REAL and row["final_label"] == REAL


def test_requests_keep_audio_transcript_and_voice(log):
    event = log.add_wake(AUDIO, 0.9, "answer", "hey tars", 0.9)
    log.add_request(event, AUDIO, "what's the weather", "alon", 0.83, np.ones(4, np.float32))
    row = log.get(event)
    assert row["transcript"] == "what's the weather" and row["speaker"] == "alon"
    assert (log.folder / row["utterance_audio"]).exists()
    ((event_id, embedding, cluster, pinned),) = log.embeddings()
    assert event_id == event and embedding.tolist() == [1, 1, 1, 1] and cluster is None and not pinned


def test_merge_moves_everything_and_pins_it(log):
    a, b = log.new_cluster(), log.new_cluster()
    e1, e2 = log.add_wake(AUDIO, 0.9, "answer", "", None), log.add_wake(AUDIO, 0.9, "answer", "", None)
    log.assign(e1, a, pinned=False)
    log.assign(e2, b, pinned=False)
    log.merge_clusters(keep=a, absorb=b)
    assert [c["id"] for c in log.clusters()] == [a]
    assert log.get(e2)["cluster_id"] == a and log.get(e2)["cluster_pinned"] == 1


def test_delete_removes_the_audio_too(log):
    event = log.add_wake(AUDIO, 0.9, "answer", "", None)
    path = log.folder / log.get(event)["audio"]
    log.delete(event)
    assert not path.exists() and log.get(event) is None


def test_ids_are_never_reused(log):
    old = log.add_wake(AUDIO, 0.9, "answer", "", None)
    log.delete(old)
    assert log.add_wake(AUDIO, 0.9, "answer", "", None) != old


def test_an_older_database_is_upgraded_keeping_every_row(tmp_path):
    folder = tmp_path / "events"
    folder.mkdir()
    with sqlite3.connect(folder / "events.db") as db:  # the first schema: ids could be reused, no file_name
        for create in TABLES.values():
            create = create.replace(" AUTOINCREMENT", "")
            db.execute(
                create.replace("    file_name TEXT                   -- file: the name it downloads as\n", "").replace(
                    "size INTEGER,     --", "size INTEGER      --"
                )
            )
        db.execute("INSERT INTO events (id, ts, kind) VALUES (7, 1.0, 'wake')")
        db.execute("INSERT INTO conversations (id, started, wake_event_id) VALUES (1, 1.0, 99)")  # a deleted wake
    log = EventLog(folder)
    assert log.get(7)["kind"] == "wake"
    assert log.add_wake(AUDIO, 0.9, "answer", "", None) == 8
    assert log.store.rows("SELECT wake_event_id FROM conversations") == [{"wake_event_id": None}]
    assert "file_name" in {r["name"] for r in log.store.rows("PRAGMA table_info(items)")}
    assert "AUTOINCREMENT" in log.store.rows("SELECT sql FROM sqlite_master WHERE name='items'")[0]["sql"]


def test_counts(log):
    log.set_label(log.add_wake(AUDIO, 0.9, "answer", "", None), REAL)
    log.add_near_miss(AUDIO, 0.4)
    assert log.counts() == {"events": 2, "labeled": 1}


def test_old_unlabeled_audio_is_dropped_and_labeled_audio_kept(log):
    now = time.time()
    old = now - 90 * events.DAY_S
    lonely = log.add_near_miss(AUDIO, 0.35, ts=old)  # nothing labels it
    silent = log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9, ts=old + 100)
    log.set_follow(silent, SAID_NOTHING)  # it woke and nobody spoke: no guess either way
    asked = log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9, ts=old + 200)
    log.set_follow(asked, ASKED)  # automatically real: training data
    mine = log.add_near_miss(AUDIO, 0.35, ts=old + 300)
    log.set_label(mine, REAL)  # a person said it was real
    recent = log.add_near_miss(AUDIO, 0.35)
    paths = {e: log.folder / log.get(e)["audio"] for e in (lonely, silent, asked, mine, recent)}
    assert log.prune_audio(keep_days=60, now=now) == 2
    assert [e for e, p in paths.items() if not p.exists()] == [lonely, silent]
    assert log.get(lonely)["audio"] is None and log.get(asked)["audio"] is not None


class ScriptedTrigger(FakeTrigger):
    """Returns the given score for each block."""

    def __init__(self, scores):
        super().__init__(fire_at=[])
        self.scores = list(scores)

    def score(self, block):
        self.n += 1
        return self.scores[self.n - 1] if self.n <= len(self.scores) else 0.0


def verifier_saying(*outcomes):
    answers = iter(outcomes)
    return types.SimpleNamespace(decide=lambda pcm: next(answers), last_confidence=0.5)


def test_trigger_logs_near_misses_and_every_wake(log):
    quiet = int(NEAR_QUIET_S * SAMPLE_RATE / BLOCK_SAMPLES) + 1
    # The fake trigger wakes at 0.9, so 0.6-0.7 is a near-miss (above 60% of the threshold).
    scores = [0.1, 0.6, 0.7, 0.2] + [0.0] * quiet + [0.9] + [0.0] * 5 + [0.95]
    journal = Journal(log)
    trigger = VerifiedTrigger(
        ScriptedTrigger(scores),
        verifier_saying((IGNORE, "hey there"), (ANSWER, "hey tars")),
        journal=journal,
        wake_model="m.tflite",
    )
    assert trigger.wait(SilentMic(len(scores) + 5)) == ANSWER
    journal.flush()
    # Near-misses are written in the background, so ids can come in any order; times are when it happened.
    rows = sorted(log.events(), key=lambda r: r["ts"])
    assert [(r["kind"], r["outcome"]) for r in rows] == [("near_miss", None), ("wake", "ignore"), ("wake", "answer")]
    assert rows[0]["wake_score"] == pytest.approx(0.7) and rows[2]["wake_model"] == "m.tflite"
    assert journal._wake == rows[2]["id"]


def test_a_near_miss_that_turns_into_a_wake_is_just_a_wake(log):
    journal = Journal(log)
    trigger = VerifiedTrigger(ScriptedTrigger([0.6, 0.7, 0.95]), verifier_saying((ANSWER, "hey tars")), journal=journal)
    assert trigger.wait(SilentMic(40)) == ANSWER
    journal.flush()
    assert [r["kind"] for r in log.events()] == ["wake"]


def test_a_broken_event_log_never_stops_the_trigger(log, monkeypatch):
    monkeypatch.setattr(log, "add_wake", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    trigger = VerifiedTrigger(ScriptedTrigger([0.0, 0.95]), verifier_saying((ANSWER, "hey tars")), journal=Journal(log))
    assert trigger.wait(SilentMic(5)) == ANSWER


def woken_assistant(log, speaker, utterances, transcripts, replies=(), outcome=ANSWER):
    journal = Journal(log)
    assistant, _ = make_assistant(speaker, utterances, transcripts, replies, journal=journal)
    journal.wake(AUDIO, 0.9, outcome, "hey tars", 0.9, "", "")
    assistant.trigger = types.SimpleNamespace(last_audio=None)
    return assistant, journal._wake


def test_assistant_records_what_followed_each_wake(log, speaker):
    assistant, event = woken_assistant(log, speaker, [speech(), None], ["what time is it"])
    assistant.converse(follow_up_s=4.0)
    row = log.get(event)
    assert row["follow"] == ASKED and row["transcript"] == "what time is it"

    assistant, ask = woken_assistant(log, speaker, [None], [], outcome="ask")
    assistant.say = lambda text: True
    assistant.ask_if_called(follow_up_s=4.0)
    assert log.get(ask)["follow"] == SAID_NOTHING

    assistant, no = woken_assistant(log, speaker, [speech()], ["no"], replies=["<skip>"], outcome="ask")
    assistant.say = lambda text: True
    assistant.ask_if_called(follow_up_s=4.0)
    assert log.get(no)["follow"] == NOT_FOR_US

    assistant, noise = woken_assistant(log, speaker, [speech()], [""])
    assistant.converse(follow_up_s=4.0)
    assert log.get(noise)["follow"] == SAID_NOTHING and log.get(noise)["utterance_audio"]
