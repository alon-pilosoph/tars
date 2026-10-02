"""The event log: storage, automatic labels, what training learns from, clusters, and what the trigger and assistant
write into it."""

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
    WAKE,
    EventLog,
    learning_label,
)
from voice_assistant.journal import Journal
from voice_assistant.store import MIGRATIONS
from voice_assistant.verify import ANSWER, ASK, IGNORE, NEAR_QUIET_S

from .conftest import AUDIO, FakeTrigger, SilentMic, make_assistant, speech
from .test_verify import listening

pytestmark = pytest.mark.usefixtures("no_tts")


def test_automatic_labels_follow_what_happened_next(log):
    asked = log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9)
    log.set_follow(asked, ASKED)
    unanswered_ask = log.add_wake(AUDIO, 0.7, ASK, "hey cars", 0.1)
    log.set_follow(unanswered_ask, SAID_NOTHING)
    rejected = log.add_wake(AUDIO, 0.6, IGNORE, "hey there", 0.0)
    overheard = log.add_wake(AUDIO, 0.8, ANSWER, "hey tars", 0.8)
    log.set_follow(overheard, NOT_FOR_US)
    reply_failed = log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9)
    log.add_request(reply_failed, AUDIO, "what's the weather", None, None, None)  # and then TARS couldn't answer
    ask_reply_failed = log.add_wake(AUDIO, 0.7, ASK, "hey cars", 0.1)
    log.add_request(ask_reply_failed, AUDIO, "yes, what's the time", None, None, None)
    label = {e["id"]: e["auto_label"] for e in log.events()}
    assert label[asked] == REAL and label[unanswered_ask] == NOT_REAL and label[reply_failed] == REAL
    assert label[ask_reply_failed] == REAL
    assert label[rejected] == NOT_REAL and label[overheard] == NOT_REAL


def test_a_near_miss_right_before_a_real_wake_counts_as_a_missed_hey_tars(log, monkeypatch):
    clock = iter([1000.0, 1003.0, 2000.0])
    monkeypatch.setattr(events.time, "time", lambda: next(clock))
    missed = log.add_near_miss(AUDIO, 0.4)  # t=1000
    log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9)  # t=1003: they said it again, louder
    lonely = log.add_near_miss(AUDIO, 0.35)  # t=2000, nothing after it
    rows = {e["id"]: e for e in log.events()}
    assert rows[missed]["auto_label"] == REAL and rows[lonely]["auto_label"] is None
    assert rows[lonely]["auto_reason"] == "it never woke up"
    # The same whichever rows are asked for: a page that ends at the near-miss.
    assert [e["auto_label"] for e in log.events(limit=3)[-1:]] == [REAL]


def test_a_persons_label_wins_over_the_automatic_one(log):
    event = log.add_wake(AUDIO, 0.6, IGNORE, "hey bars", 0.1)
    log.set_label(event, REAL)  # it WAS "hey TARS", the check got it wrong
    (row,) = log.events()
    assert row["auto_label"] == NOT_REAL and learning_label(row) == REAL


def test_training_learns_from_people_and_from_what_happened_but_not_from_the_check_itself():
    def event(label=None, auto=None, outcome=ANSWER):
        return {"label": label, "auto_label": auto, "outcome": outcome}

    assert learning_label(event(auto=REAL)) == REAL  # a request followed
    assert learning_label(event(auto=NOT_REAL, outcome=ASK)) == NOT_REAL  # nobody answered "Did you call me?"
    assert learning_label(event(auto=NOT_REAL, outcome=IGNORE)) is None  # only the check's own verdict
    assert learning_label(event(label=REAL, auto=NOT_REAL, outcome=IGNORE)) == REAL  # a person said it was real


def test_what_is_waiting_to_be_learned_from(log):
    log.set_label(log.add_wake(AUDIO, 0.9, IGNORE, "hey cars", 0.1, ts=100), REAL)  # answered in Review
    log.add_wake(AUDIO, 0.9, IGNORE, "hey cars", 0.1, ts=101)  # the check's own verdict: waits for Review
    log.set_label(log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9, ts=102), NOT_REAL)
    log.set_label(log.add_near_miss(AUDIO, 0.4, ts=103), REAL)
    assert log.learning() == {"real": 1, "not_real": 1, "missed": 1, "to_review": 1}
    assert log.learning(since=101.5) == {"real": 0, "not_real": 1, "missed": 1, "to_review": 0}


def test_review_gets_the_newest_waiting_and_answered_events(log, monkeypatch):
    monkeypatch.setattr(events, "REVIEW_SHOWN", 2)
    _, waiting, newest_waiting = (log.add_near_miss(AUDIO, 0.4, ts=t) for t in (1, 2, 3))
    old, new = (log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9, ts=t) for t in (4, 5))
    log.set_label(old, REAL)
    log.set_label(new, NOT_REAL)
    oldest = log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9, ts=0.5)
    log.set_label(oldest, REAL)
    assert [e["id"] for e in log.for_review()] == [new, old, newest_waiting, waiting]
    assert len(log.events()) == 6


def test_review_leaves_out_wakes_a_request_settled_and_ones_without_audio(log):
    settled = log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9, ts=1)
    log.set_follow(settled, ASKED)
    log.add_near_miss(AUDIO, 0.4, ts=2)  # lonely: its audio is pruned
    log.prune_audio(keep_days=0, now=1e9)
    waiting = log.add_near_miss(AUDIO, 0.4, ts=3)
    assert [e["id"] for e in log.for_review()] == [waiting]


def test_requests_keep_audio_transcript_and_voice(log):
    event = log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9)
    log.add_request(event, AUDIO, "what's the weather", "alon", 0.83, np.ones(4, np.float32))
    row = log.get(event)
    assert row["transcript"] == "what's the weather" and row["speaker"] == "alon"
    assert (log.folder / row["utterance_audio"]).exists()
    ((event_id, embedding, cluster, pinned),) = log.embeddings()
    assert event_id == event and embedding.tolist() == [1, 1, 1, 1] and cluster is None and not pinned


def test_audio_is_not_left_behind_when_its_row_cant_be_written(log, monkeypatch):
    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(log.store, "write", locked)
    with pytest.raises(sqlite3.OperationalError):
        log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9)
    assert not any(p.is_file() for p in (log.folder / "audio").rglob("*"))


def test_merge_moves_everything_and_pins_it(log):
    a, b = log.new_cluster(), log.new_cluster()
    e1, e2 = log.add_wake(AUDIO, 0.9, ANSWER, "", None), log.add_wake(AUDIO, 0.9, ANSWER, "", None)
    log.assign(e1, a, pinned=False)
    log.assign(e2, b, pinned=False)
    log.merge_clusters(keep=a, absorb=b)
    assert [c["id"] for c in log.clusters()] == [a]
    assert log.get(e2)["cluster_id"] == a and log.get(e2)["cluster_pinned"] == 1


def test_delete_removes_the_audio_too(log):
    event = log.add_wake(AUDIO, 0.9, ANSWER, "", None)
    path = log.folder / log.get(event)["audio"]
    log.delete(event)
    assert not path.exists() and log.get(event) is None


def test_ids_are_never_reused(log):
    old = log.add_wake(AUDIO, 0.9, ANSWER, "", None)
    log.delete(old)
    assert log.add_wake(AUDIO, 0.9, ANSWER, "", None) != old


# The schema at PRAGMA user_version 0, before numbered migrations.
UNVERSIONED = [
    (
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, "
        "wake_score REAL, outcome TEXT, heard TEXT, confidence REAL, wake_model TEXT, check_model TEXT, audio TEXT, "
        "utterance_audio TEXT, transcript TEXT, follow TEXT, speaker TEXT, speaker_score REAL, embedding BLOB, "
        "label TEXT, cluster_id INTEGER, cluster_pinned INTEGER DEFAULT 0)"
    ),
    (
        "CREATE TABLE clusters (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, kind TEXT DEFAULT 'unknown', "
        "created REAL NOT NULL)"
    ),
    (
        "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL NOT NULL, ended REAL, "
        "wake_event_id INTEGER)"
    ),
    (
        "CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL, ts REAL NOT NULL, "
        "role TEXT NOT NULL, text TEXT, audio TEXT, speaker TEXT, speaker_score REAL, embedding BLOB, "
        "not_for_tars INTEGER DEFAULT 0, corrected_text TEXT, rating TEXT)"
    ),
    (
        "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, turn_id INTEGER, "
        "ts REAL NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, scope TEXT NOT NULL, for_name TEXT, "
        "seen INTEGER DEFAULT 0, url TEXT, site TEXT, description TEXT, body TEXT, entries TEXT, file TEXT, "
        "mime TEXT, size INTEGER, file_name TEXT)"
    ),
    "CREATE INDEX events_ts ON events(ts)",
    "CREATE INDEX turns_conversation ON turns(conversation_id)",
    "CREATE INDEX items_conversation ON items(conversation_id)",
]


def test_an_older_database_is_upgraded_keeping_every_row(tmp_path):
    folder = tmp_path / "events"
    folder.mkdir()
    with sqlite3.connect(folder / "events.db") as db:
        for statement in UNVERSIONED:
            db.execute(statement)
        db.execute("INSERT INTO events (id, ts, kind) VALUES (7, 1.0, 'wake')")
        db.execute("INSERT INTO conversations (id, started) VALUES (1, 1.0)")
        db.execute(
            "INSERT INTO turns (conversation_id, ts, role, text, speaker_score) VALUES (1, 1.0, 'person', 'hi', 0.8)"
        )
    log = EventLog(folder)
    assert log.get(7)["kind"] == WAKE and log.store.rows("SELECT text FROM turns") == [{"text": "hi"}]
    assert log.add_wake(AUDIO, 0.9, ANSWER, "", None) == 8
    assert "speaker_score" not in {r["name"] for r in log.store.rows("PRAGMA table_info(turns)")}
    assert log.store.rows("PRAGMA user_version")[0]["user_version"] == len(MIGRATIONS)
    EventLog(folder)  # opening it again changes nothing


def test_a_new_database_starts_at_the_latest_version(log):
    assert log.store.rows("PRAGMA user_version")[0]["user_version"] == len(MIGRATIONS)


def test_old_audio_nothing_learns_from_is_dropped_and_the_rest_kept(log):
    now = time.time()
    old = now - 90 * events.DAY_S
    lonely = log.add_near_miss(AUDIO, 0.35, ts=old)  # nothing labels it
    silent = log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9, ts=old + 100)
    log.set_follow(silent, SAID_NOTHING)  # it woke and nobody spoke: no guess either way
    turned_away = log.add_wake(AUDIO, 0.6, IGNORE, "hey there", 0.0, ts=old + 150)  # the TV, never reviewed
    asked = log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9, ts=old + 200)
    log.set_follow(asked, ASKED)  # automatically real: training data
    mine = log.add_near_miss(AUDIO, 0.35, ts=old + 300)
    log.set_label(mine, REAL)  # a person said it was real
    recent = log.add_near_miss(AUDIO, 0.35)
    paths = {e: log.folder / log.get(e)["audio"] for e in (lonely, silent, turned_away, asked, mine, recent)}
    assert log.prune_audio(keep_days=60, now=now) == 3
    assert [e for e, p in paths.items() if not p.exists()] == [lonely, silent, turned_away]
    assert log.get(lonely)["audio"] is None and log.get(asked)["audio"] is not None


def test_audio_answered_in_review_while_pruning_is_kept(log, monkeypatch):
    old = time.time() - 90 * events.DAY_S
    lonely = log.add_near_miss(AUDIO, 0.35, ts=old)
    rows = log.store.rows

    def answered_meanwhile(sql, args=()):
        found = rows(sql, args)
        log.set_label(lonely, REAL)  # someone answers it in Review just after pruning read it
        return found

    monkeypatch.setattr(log.store, "rows", answered_meanwhile)
    assert log.prune_audio(keep_days=60) == 0
    monkeypatch.undo()
    assert (log.folder / log.get(lonely)["audio"]).exists()


class ScriptedTrigger(FakeTrigger):
    def __init__(self, scores):
        super().__init__(fire_at=[])
        self.scores = list(scores)

    def score(self, block):
        self.n += 1
        return self.scores[self.n - 1] if self.n <= len(self.scores) else 0.0


def verifier_saying(*outcomes):
    answers = iter(outcomes)
    return types.SimpleNamespace(decide=lambda pcm: next(answers), last_confidence=0.5, use_check=lambda spec: None)


def test_trigger_logs_near_misses_and_every_wake(log):
    quiet = int(NEAR_QUIET_S * SAMPLE_RATE / BLOCK_SAMPLES) + 1
    # The fake trigger wakes at 0.9, so 0.6-0.7 is a near-miss (above 60% of the threshold).
    scores = [0.1, 0.6, 0.7, 0.2] + [0.0] * quiet + [0.9] + [0.0] * 5 + [0.95]
    journal = Journal(log)
    trigger = listening(
        ScriptedTrigger(scores),
        verifier_saying((IGNORE, "hey there"), (ANSWER, "hey tars")),
        journal,
        wake_model="m.tflite",
    )
    assert trigger.wait(SilentMic(len(scores) + 5)) == ANSWER
    journal.flush()
    # Near-misses are written in the background, so ids can come in any order; times are when it happened.
    rows = sorted(log.events(), key=lambda r: r["ts"])
    assert [(r["kind"], r["outcome"]) for r in rows] == [(NEAR_MISS, None), (WAKE, IGNORE), (WAKE, ANSWER)]
    assert rows[0]["wake_score"] == pytest.approx(0.7) and rows[2]["wake_model"] == "m.tflite"


def test_a_near_miss_that_turns_into_a_wake_is_just_a_wake(log):
    journal = Journal(log)
    trigger = listening(ScriptedTrigger([0.6, 0.7, 0.95]), verifier_saying((ANSWER, "hey tars")), journal)
    assert trigger.wait(SilentMic(40)) == ANSWER
    journal.flush()
    assert [r["kind"] for r in log.events()] == [WAKE]


def test_a_broken_event_log_never_stops_the_trigger(log, monkeypatch):
    monkeypatch.setattr(log, "add_wake", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    trigger = listening(ScriptedTrigger([0.0, 0.95]), verifier_saying((ANSWER, "hey tars")), Journal(log))
    assert trigger.wait(SilentMic(5)) == ANSWER


def woken_assistant(log, speaker, utterances, transcripts, replies=(), outcome=ANSWER):
    journal = Journal(log)
    assistant, _ = make_assistant(speaker, utterances, transcripts, replies, journal=journal)
    journal.wake(AUDIO, 0.9, outcome, "hey tars", 0.9, "", "")
    return assistant, journal, log.events(limit=1)[0]["id"]


def test_assistant_records_what_followed_each_wake(log, speaker):
    assistant, journal, event = woken_assistant(log, speaker, [speech(), None], ["what time is it"])
    assistant.converse(follow_up_s=4.0)
    journal.flush()
    row = log.get(event)
    assert row["follow"] == ASKED and row["transcript"] == "what time is it"

    assistant, journal, ask = woken_assistant(log, speaker, [None], [], outcome=ASK)
    assistant.say = lambda text: True
    assistant.ask_if_called(follow_up_s=4.0)
    journal.flush()
    assert log.get(ask)["follow"] == SAID_NOTHING

    assistant, journal, no = woken_assistant(log, speaker, [speech()], ["no"], replies=["<skip>"], outcome=ASK)
    assistant.say = lambda text: True
    assistant.ask_if_called(follow_up_s=4.0)
    journal.flush()
    assert log.get(no)["follow"] == NOT_FOR_US

    assistant, journal, noise = woken_assistant(log, speaker, [speech()], [""])
    assistant.converse(follow_up_s=4.0)
    journal.flush()
    assert log.get(noise)["follow"] == SAID_NOTHING and log.get(noise)["utterance_audio"]
