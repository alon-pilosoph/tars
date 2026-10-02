"""The event log that self-learning TARS learns from: every wake, every near-miss, and what happened next.

Only raw facts are stored (see store.py). The automatic "was this really hey TARS?" guess is computed on read
(auto_label), so its rules can improve without touching the data. A person's own label and cluster assignment always
win over the automatic ones.
"""

import sqlite3
import time
from pathlib import Path

import numpy as np

from .store import Store
from .verify import ANSWER, ASK, IGNORE

WAKE, NEAR_MISS = "wake", "near_miss"
ASKED, SAID_NOTHING, NOT_FOR_US = "asked", "said_nothing", "not_for_us"
REAL, NOT_REAL = "real", "not_real"
PERSON, NOT_PERSON, UNKNOWN = "person", "not_person", "unknown"
MISSED_WINDOW_S = 6.0  # a near-miss followed this soon by a real wake was probably a missed "hey TARS"
DAY_S = 24 * 3600
REVIEW_SHOWN = 500  # Review shows this many of the newest wakes waiting for an answer, and as many answered ones
SAMPLES = 4  # requests a voice is shown with


def person_key(name: str | None) -> str | None:
    """Voiceprint, cluster and "for" names are the same person whatever their case or stray spaces."""
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
        with self.store.removed_on_failure(audio):
            return self.store.write(
                "INSERT INTO events (ts, kind, wake_score, outcome, heard, confidence, wake_model, check_model, audio) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, WAKE, wake_score, outcome, heard, confidence, wake_model, check_model, audio),
            )

    def add_near_miss(self, pcm: np.ndarray, peak_score: float, wake_model: str = "", ts: float | None = None) -> int:
        ts = ts or time.time()
        audio = self.store.save_audio(pcm, f"{int(ts * 1000)}_near")
        with self.store.removed_on_failure(audio):
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
        with self.store.removed_on_failure(audio):
            self.store.write(
                "UPDATE events SET utterance_audio=?, transcript=?, speaker=?, speaker_score=?, embedding=? WHERE id=?",
                (audio, transcript, speaker, speaker_score, blob, event_id),
            )
        return audio

    def set_follow(self, event_id: int, follow: str) -> None:
        self.store.write("UPDATE events SET follow=? WHERE id=?", (follow, event_id))

    def prune_audio(self, keep_days: float, now: float | None = None) -> int:
        """Drops the audio of old events training wouldn't learn from (lonely near-misses, wakes nobody spoke after,
        unreviewed wakes the check turned away); a TV can make dozens a day. Labeled audio is training data and stays.
        Returns how many events lost their audio."""
        cutoff = (now or time.time()) - keep_days * DAY_S
        rows = self._labeled(
            self.store.rows(
                "SELECT * FROM events WHERE ts < ? AND label IS NULL AND "
                "(audio IS NOT NULL OR utterance_audio IS NOT NULL)",
                (cutoff,),
            )
        )
        dropped = []
        with self.store.transaction() as db:
            for r in rows:
                # Someone may have answered it in Review since it was read.
                if (
                    learning_label(r) is None
                    and db.execute(
                        "UPDATE events SET audio=NULL, utterance_audio=NULL WHERE id=? AND label IS NULL", (r["id"],)
                    ).rowcount
                ):
                    db.execute("UPDATE turns SET audio=NULL WHERE audio=?", (r["utterance_audio"],))
                    dropped.append(r)
        self.store.remove([rel for r in dropped for rel in (r["audio"], r["utterance_audio"])])
        return len(dropped)

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
        """Meant for an empty voice; any events still in it become unassigned."""
        with self.store.transaction() as db:
            db.execute("UPDATE events SET cluster_id=NULL, cluster_pinned=0 WHERE cluster_id=?", (cluster_id,))
            db.execute("DELETE FROM clusters WHERE id=?", (cluster_id,))

    def merge_clusters(self, keep: int, absorb: int) -> None:
        """Moves everything in `absorb` to `keep`, pinned, since a person decided they're the same voice."""
        with self.store.transaction() as db:
            db.execute("UPDATE events SET cluster_id=?, cluster_pinned=1 WHERE cluster_id=?", (keep, absorb))
            db.execute("DELETE FROM clusters WHERE id=?", (absorb,))

    def get(self, event_id: int) -> dict | None:
        rows = self.store.rows("SELECT * FROM events WHERE id=?", (event_id,))
        return rows[0] if rows else None

    def events(self, limit: int | None = None) -> list[dict]:
        """Newest first (all of them without a `limit`), each with its automatic label."""
        return self._labeled(
            self.store.rows("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (-1 if limit is None else limit,))
        )

    def for_review(self) -> list[dict]:
        """Newest first: the events waiting for an answer, and the newest answered ones. A wake a request followed
        settles itself, and one whose audio is gone can't be listened to, so neither waits."""
        return self._labeled(
            self.store.rows(
                "SELECT * FROM events WHERE id IN (SELECT id FROM events WHERE label IS NULL AND audio IS NOT NULL "
                "AND follow IS NOT ? ORDER BY ts DESC LIMIT ?) "
                "OR id IN (SELECT id FROM events WHERE label IS NOT NULL ORDER BY ts DESC LIMIT ?) ORDER BY ts DESC",
                (ASKED, REVIEW_SHOWN, REVIEW_SHOWN),
            )
        )

    def _labeled(self, rows: list[dict]) -> list[dict]:
        answered = self._answered_times(rows)
        for r in rows:
            r["auto_label"], r["auto_reason"] = auto_label(r, answered)
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
        """Counts of what training would learn from (see learning_label), plus wakes still waiting in Review."""
        rows = [e for e in self.events() if e["audio"] and (since is None or e["ts"] > since)]
        labels = [(e["kind"], learning_label(e)) for e in rows]
        return {
            "real": labels.count((WAKE, REAL)),
            "not_real": labels.count((WAKE, NOT_REAL)),
            "missed": labels.count((NEAR_MISS, REAL)),
            "to_review": labels.count((WAKE, None)),
        }

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

    def samples(self) -> dict[int, list[dict]]:
        """Per voice, its SAMPLES newest requests that have audio: [{event_id, transcript}]."""
        rows = self.store.rows(
            "SELECT id, cluster_id, transcript FROM (SELECT id, cluster_id, transcript, ROW_NUMBER() OVER "
            "(PARTITION BY cluster_id ORDER BY ts DESC) AS n FROM events "
            "WHERE cluster_id IS NOT NULL AND utterance_audio IS NOT NULL) WHERE n <= ? ORDER BY n",
            (SAMPLES,),
        )
        samples: dict[int, list[dict]] = {}
        for r in rows:
            samples.setdefault(r["cluster_id"], []).append({"event_id": r["id"], "transcript": r["transcript"]})
        return samples

    def request_audio(self, cluster_id: int) -> list[Path]:
        paths = [
            self.folder / r["utterance_audio"]
            for r in self.store.rows(
                "SELECT utterance_audio FROM events WHERE cluster_id=? AND utterance_audio IS NOT NULL", (cluster_id,)
            )
        ]
        return [p for p in paths if p.exists()]

    def wakes(self, ids: list[int | None]) -> dict[int, dict]:
        """What conversations show about the wakes that started them, including whether a person decided the voice."""
        ids = [i for i in ids if i is not None]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.store.rows(
            "SELECT e.id, e.heard, e.confidence, e.outcome, e.label, "
            "e.utterance_audio IS NOT NULL AS has_request_audio, e.cluster_id, e.cluster_pinned, "
            "c.name AS cluster_name, c.kind AS cluster_kind "
            f"FROM events e LEFT JOIN clusters c ON c.id = e.cluster_id WHERE e.id IN ({marks})",
            ids,
        )
        for r in rows:
            cluster = {"name": r["cluster_name"], "kind": r["cluster_kind"]} if r["cluster_id"] is not None else None
            r["voice_decided"] = voice_decided(cluster, r["cluster_pinned"])
        return {r["id"]: r for r in rows}


def voice_decided(cluster: dict | None, pinned: bool) -> bool:
    """Whether a person decided which voice a request is (re-clustering leaves it alone, and it wins over speaker
    ID's guess): they moved it by hand, or named its voice or marked it not a person."""
    return cluster is not None and bool(pinned or cluster["name"] or cluster["kind"] == NOT_PERSON)


def learning_label(event: dict) -> str | None:
    """The label training learns from: a person's, else the automatic one, unless that is only the check's own
    verdict. Learning from those would teach both stages what the check already thinks, including its mistakes on a
    soft "t"; a person's answer in Review makes them count."""
    if event["label"]:
        return event["label"]
    return None if event["outcome"] == IGNORE else event["auto_label"]


def auto_label(event: dict, answered_wake_times: list[float]) -> tuple[str | None, str]:
    """Whether someone really said the wake phrase, with the reason shown in the UI."""
    if event["kind"] == NEAR_MISS:
        later = [t for t in answered_wake_times if 0 < t - event["ts"] <= MISSED_WINDOW_S]
        if later:
            return REAL, "a real wake followed within seconds, so it was probably a missed hey TARS"
        return None, "it never woke up"
    follow, outcome = event["follow"], event["outcome"]
    if outcome == IGNORE:
        return NOT_REAL, "the double-check heard something else"
    if follow == ASKED or (follow is None and event["transcript"]):  # a request but no follow: the reply failed
        return REAL, "a request followed" if outcome == ANSWER else "they answered “Did you call me?”"
    if follow == NOT_FOR_US:
        return NOT_REAL, "the reply wasn't meant for TARS"
    if follow == SAID_NOTHING:
        if outcome == ASK:
            return NOT_REAL, "nobody answered “Did you call me?”"
        return None, "it woke, but nobody spoke"
    return None, ""
