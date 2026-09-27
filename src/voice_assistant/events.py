"""The event log that self-learning TARS learns from: every wake, every near-miss, and what happened next.

Stored locally (voice_data/events/, see store.py). Only raw facts are stored; the automatic "was this really
hey TARS?" guess is computed when reading (auto_label), so its rules can improve without touching the data. A
person's own label and cluster assignment always win over the automatic ones.
"""

import sqlite3
import time
from pathlib import Path

import numpy as np

from .store import Store
from .verify import ANSWER, ASK, IGNORE

WAKE, NEAR_MISS = "wake", "near_miss"
# What happened after a wake (the "follow" column).
ASKED, SAID_NOTHING, NOT_FOR_US = "asked", "said_nothing", "not_for_us"
# Labels, automatic or given by a person in the web UI.
REAL, NOT_REAL = "real", "not_real"
# Voice clusters: a person, something that isn't (the TV), or not decided yet.
PERSON, NOT_PERSON, UNKNOWN = "person", "not_person", "unknown"
MISSED_WINDOW_S = 6.0  # a near-miss followed this soon by a real wake was probably a missed "hey TARS"
DAY_S = 24 * 3600


def person_key(name: str | None) -> str | None:
    """How names are matched: speaker ID's voiceprint names, cluster names and "for" names are the same person
    whatever their case or stray spaces."""
    return (name.strip().lower() or None) if name else None


class EventLog:
    def __init__(self, folder: Path):
        self.folder = folder
        self.store = Store(folder)

    def add_wake(
        self,
        pcm: np.ndarray,
        wake_score: float,
        outcome: str,
        heard: str,
        confidence: float | None,
        wake_model: str = "",
        check_model: str = "",
        ts: float | None = None,
    ) -> int:
        ts = ts or time.time()
        audio = self.store.save_audio(pcm, f"{int(ts * 1000)}_wake")
        return self.store.write(
            "INSERT INTO events (ts, kind, wake_score, outcome, heard, confidence, wake_model, check_model, audio) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, WAKE, wake_score, outcome, heard, confidence, wake_model, check_model, audio),
        )

    def add_near_miss(self, pcm: np.ndarray, peak_score: float, wake_model: str = "", ts: float | None = None) -> int:
        ts = ts or time.time()
        audio = self.store.save_audio(pcm, f"{int(ts * 1000)}_near")
        return self.store.write(
            "INSERT INTO events (ts, kind, wake_score, wake_model, audio) VALUES (?, ?, ?, ?, ?)",
            (ts, NEAR_MISS, peak_score, wake_model, audio),
        )

    def add_request(
        self,
        event_id: int,
        pcm: np.ndarray | bytes,
        transcript: str,
        speaker: str | None,
        speaker_score: float | None,
        embedding: np.ndarray | None,
    ) -> str:
        """Returns where the request's audio is stored, so the conversation's first turn can point at it too."""
        audio = self.store.save_audio(pcm, f"{int(time.time() * 1000)}_request")
        blob = np.asarray(embedding, dtype=np.float32).tobytes() if embedding is not None else None
        self.store.write(
            "UPDATE events SET utterance_audio=?, transcript=?, speaker=?, speaker_score=?, embedding=? WHERE id=?",
            (audio, transcript, speaker, speaker_score, blob, event_id),
        )
        return audio

    def set_follow(self, event_id: int, follow: str) -> None:
        self.store.write("UPDATE events SET follow=? WHERE id=?", (follow, event_id))

    def prune_audio(self, keep_days: float, now: float | None = None) -> int:
        """Drop the audio of old events nothing labels (lonely near-misses, wakes nobody spoke after): they can't
        teach anything, and a TV can make dozens a day. Labeled audio is training data and stays. Returns how
        many events lost their audio."""
        cutoff = (now or time.time()) - keep_days * DAY_S
        rows = self.store.rows(
            "SELECT * FROM events WHERE ts < ? AND label IS NULL AND "
            "(audio IS NOT NULL OR utterance_audio IS NOT NULL)",
            (cutoff,),
        )
        answered = self._answered_times(rows)
        drop = [r for r in rows if auto_label(r, answered)[0] is None]
        with self.store.transaction() as db:
            for r in drop:
                db.execute("UPDATE events SET audio=NULL, utterance_audio=NULL WHERE id=?", (r["id"],))
                db.execute("UPDATE turns SET audio=NULL WHERE audio=?", (r["utterance_audio"],))
        self.store.remove([rel for r in drop for rel in (r["audio"], r["utterance_audio"])])
        return len(drop)

    def set_label(self, event_id: int, label: str | None) -> None:
        self.store.write("UPDATE events SET label=? WHERE id=?", (label, event_id))

    def delete(self, event_id: int) -> None:
        """The wake and its audio. A conversation it started stays, without its wake or the request's audio."""
        with self.store.transaction() as db:
            files = self.delete_rows(db, event_id)
        self.store.remove(files)

    def delete_rows(self, db: sqlite3.Connection, event_id: int) -> list[str | None]:
        """Delete an event inside an open transaction; returns its audio, to remove once that commits."""
        event = db.execute("SELECT audio, utterance_audio FROM events WHERE id=?", (event_id,)).fetchone()
        if not event:
            return []
        db.execute("UPDATE conversations SET wake_event_id=NULL WHERE wake_event_id=?", (event_id,))
        db.execute("UPDATE turns SET audio=NULL WHERE audio=?", (event["utterance_audio"],))
        db.execute("DELETE FROM events WHERE id=?", (event_id,))
        return [event["audio"], event["utterance_audio"]]

    def new_cluster(self, name: str | None = None, kind: str = UNKNOWN) -> int:
        return self.store.write(
            "INSERT INTO clusters (name, kind, created) VALUES (?, ?, ?)", (name, kind, time.time())
        )

    def assign(self, event_id: int, cluster_id: int | None, pinned: bool) -> None:
        self.store.write(
            "UPDATE events SET cluster_id=?, cluster_pinned=? WHERE id=?", (cluster_id, int(pinned), event_id)
        )

    def rename_cluster(self, cluster_id: int, name: str | None, kind: str = PERSON) -> None:
        self.store.write("UPDATE clusters SET name=?, kind=? WHERE id=?", (name, kind, cluster_id))

    def delete_cluster(self, cluster_id: int) -> None:
        """Only an empty voice; events keep their audio and just become unassigned otherwise."""
        with self.store.transaction() as db:
            db.execute("UPDATE events SET cluster_id=NULL, cluster_pinned=0 WHERE cluster_id=?", (cluster_id,))
            db.execute("DELETE FROM clusters WHERE id=?", (cluster_id,))

    def merge_clusters(self, keep: int, absorb: int) -> None:
        """Everything in `absorb` moves to `keep` (pinned: a person decided they're the same voice)."""
        with self.store.transaction() as db:
            db.execute("UPDATE events SET cluster_id=?, cluster_pinned=1 WHERE cluster_id=?", (keep, absorb))
            db.execute("DELETE FROM clusters WHERE id=?", (absorb,))

    def get(self, event_id: int) -> dict | None:
        rows = self.store.rows("SELECT * FROM events WHERE id=?", (event_id,))
        return rows[0] if rows else None

    def events(self, limit: int = 500, kind: str | None = None) -> list[dict]:
        """Newest first, each with its automatic label."""
        sql, args = "SELECT * FROM events", ()
        if kind:
            sql, args = sql + " WHERE kind=?", (kind,)
        rows = self.store.rows(sql + " ORDER BY ts DESC LIMIT ?", (*args, limit))
        answered = self._answered_times(rows)
        for r in rows:
            r["auto_label"], r["auto_reason"] = auto_label(r, answered)
            r["final_label"] = r["label"] or r["auto_label"]
        return rows

    def _answered_times(self, rows: list[dict]) -> list[float]:
        """Answered wakes around these rows, looked up on their own so a near-miss gets the same label whichever
        rows were asked for (a near-miss filter, or the edge of a page)."""
        if not rows:
            return []
        lo, hi = min(r["ts"] for r in rows), max(r["ts"] for r in rows) + MISSED_WINDOW_S
        return [
            r["ts"]
            for r in self.store.rows(
                "SELECT ts FROM events WHERE kind=? AND outcome=? AND ts > ? AND ts <= ? ORDER BY ts",
                (WAKE, ANSWER, lo, hi),
            )
        ]

    def learning(self, since: float | None = None) -> dict:
        """What training would learn from (see learning_label): labeled wakes, real and not, and near-misses that
        were a missed "hey TARS"; plus the wakes still waiting for an answer in Review. `since`: only newer ones."""
        rows = [e for e in self.events(limit=1_000_000_000) if e["audio"] and (since is None or e["ts"] > since)]
        labels = [(e["kind"], learning_label(e)) for e in rows]
        return {
            "real": labels.count((WAKE, REAL)),
            "not_real": labels.count((WAKE, NOT_REAL)),
            "missed": labels.count((NEAR_MISS, REAL)),
            "to_review": labels.count((WAKE, None)),
        }

    def counts(self) -> dict:
        (row,) = self.store.rows("SELECT COUNT(*) AS events, COUNT(label) AS labeled FROM events")
        return row

    def cluster(self, cluster_id: int) -> dict | None:
        rows = self.store.rows("SELECT * FROM clusters WHERE id=?", (cluster_id,))
        return rows[0] if rows else None

    def clusters(self) -> list[dict]:
        return self.store.rows(
            "SELECT c.*, COUNT(e.id) AS size FROM clusters c LEFT JOIN events e ON e.cluster_id = c.id "
            "GROUP BY c.id ORDER BY size DESC"
        )

    def embeddings(self) -> list[tuple[int, np.ndarray, int | None, bool]]:
        """(event id, embedding, cluster id, pinned) for every request with a voice embedding."""
        rows = self.store.rows(
            "SELECT id, embedding, cluster_id, cluster_pinned FROM events WHERE embedding IS NOT NULL"
        )
        return [
            (r["id"], np.frombuffer(r["embedding"], dtype=np.float32), r["cluster_id"], bool(r["cluster_pinned"]))
            for r in rows
        ]

    def request_audio(self, cluster_id: int) -> list[Path]:
        paths = [
            self.folder / r["utterance_audio"]
            for r in self.store.rows(
                "SELECT utterance_audio FROM events WHERE cluster_id=? AND utterance_audio IS NOT NULL", (cluster_id,)
            )
        ]
        return [p for p in paths if p.exists()]

    def wakes(self, ids: list[int | None]) -> dict[int, dict]:
        """What conversations show about the wake that started them, with its voice and whether a person decided
        that voice (moved it by hand, or named or classified its cluster)."""
        ids = [i for i in ids if i is not None]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.store.rows(
            f"SELECT e.id, e.heard, e.confidence, e.outcome, e.cluster_id, e.cluster_pinned, c.name AS cluster_name, "
            f"c.kind AS cluster_kind FROM events e LEFT JOIN clusters c ON c.id = e.cluster_id WHERE e.id IN ({marks})",
            ids,
        )
        for r in rows:
            r["voice_decided"] = r["cluster_id"] is not None and bool(
                r["cluster_pinned"] or r["cluster_name"] or r["cluster_kind"] == NOT_PERSON
            )
        return {r["id"]: r for r in rows}


def learning_label(event: dict) -> str | None:
    """The label training learns from: a person's, else the automatic one, except where that's only the check's own
    verdict ("it heard something else"). Learning from those would teach both stages what the check already thinks,
    including when it's wrong about a soft "t"; a person's answer in Review makes them count."""
    if event["label"]:
        return event["label"]
    return None if event["outcome"] == IGNORE else event["auto_label"]


def auto_label(event: dict, answered_wake_times: list[float]) -> tuple[str | None, str]:
    """The automatic guess at whether someone really said the wake phrase, with the reason (shown in the UI)."""
    if event["kind"] == NEAR_MISS:
        later = [t for t in answered_wake_times if 0 < t - event["ts"] <= MISSED_WINDOW_S]
        if later:
            return REAL, "a real wake followed within seconds, so it was probably a missed hey TARS"
        return None, "near-miss"
    follow, outcome = event["follow"], event["outcome"]
    if outcome == IGNORE:
        return NOT_REAL, "the double-check heard something else"
    if follow == ASKED:
        return REAL, "a request followed" if outcome == ANSWER else "they answered 'Did you call me?'"
    if follow == NOT_FOR_US:
        return NOT_REAL, "the reply wasn't meant for TARS"
    if follow == SAID_NOTHING:
        return (
            (NOT_REAL, "nobody answered 'Did you call me?'") if outcome == ASK else (None, "it woke, but nobody spoke")
        )
    return None, ""
