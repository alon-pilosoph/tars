"""Every conversation TARS has: what each person said, what TARS answered, and what it sent them.

Shares the event log's store (see store.py) and links each conversation to the wake that started it. Items (links,
notes, lists, files) are what TARS "sends" to the web UI. There are no private inboxes: an item is for a person
(whoever asked) or the household, and anyone at home can see it.
"""

import json
import time
from contextlib import closing
from dataclasses import dataclass

import numpy as np

from .events import EventLog, person_key

ROLE_PERSON, ROLE_TARS = "person", "tars"
LINK, NOTE, LIST, FILE = "link", "note", "list", "file"
KINDS = (LINK, NOTE, LIST, FILE)
PERSON, HOUSEHOLD = "person", "household"
SCOPES = (PERSON, HOUSEHOLD)
# How long each stage of an answer took, in seconds, as the assistant's log prints them: the silence waited through,
# speech to text, the brain's first sentence, the voice's first audio, and from the end of speech to first sound.
TIMINGS = ("greet", "end_of_speech", "stt", "llm", "tts", "total")
# Where answering failed: transcribing, the brain, the voice, or anything else.
STAGES = STT, LLM, TTS, OTHER = ("stt", "llm", "tts", "other")
MAX_ERROR = 300


@dataclass
class SentItem:
    kind: str
    title: str
    scope: str = PERSON  # person: whoever asked; household: shared, like a shopping list
    url: str | None = None
    site: str | None = None
    description: str | None = None
    body: str | None = None  # note: short markdown
    entries: list[str] | None = None
    file_name: str | None = None
    file_bytes: bytes | None = None
    mime: str | None = None


class ConversationLog:
    def __init__(self, events: EventLog):
        self.events = events
        self.store = events.store

    def start(self, wake_event_id: int | None, ts: float | None = None) -> int:
        return self.store.write(
            "INSERT INTO conversations (started, wake_event_id) VALUES (?, ?)", (ts or time.time(), wake_event_id)
        )

    def end(self, conversation_id: int, ts: float | None = None) -> None:
        self.store.write("UPDATE conversations SET ended=? WHERE id=?", (ts or time.time(), conversation_id))

    def exists(self, conversation_id: int) -> bool:
        return bool(self.store.rows("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)))

    def add_person_turn(
        self,
        conversation_id: int,
        text: str,
        pcm: np.ndarray | bytes | None = None,
        speaker: str | None = None,
        audio: str | None = None,
        ts: float | None = None,
    ) -> int | None:
        """`audio` is already-stored audio (the request that came with the wake shares the wake's copy), else `pcm`
        is saved. None if the conversation was deleted from the web UI meanwhile."""
        if not self.exists(conversation_id):
            return None
        ts = ts or time.time()
        saved = self.store.save_audio(pcm, f"{int(ts * 1000)}_turn") if audio is None and pcm is not None else None
        with self.store.removed_on_failure(saved):
            turn = self._insert_turn(conversation_id, ts, ROLE_PERSON, text, audio=audio or saved, speaker=speaker)
        if turn is None:
            self.store.remove([saved])
        return turn

    def add_tars_turn(
        self,
        conversation_id: int,
        text: str,
        ts: float | None = None,
        timings: dict[str, float] | None = None,
        answered_by: str | None = None,
        failed_at: str | None = None,
        error: str | None = None,
    ) -> int | None:
        """`timings`: seconds by stage (TIMINGS); `failed_at` and `error`: where and why the answer failed, if it
        did."""
        kept = {k: round(v, 3) for k, v in (timings or {}).items() if k in TIMINGS}
        return self._insert_turn(
            conversation_id,
            ts or time.time(),
            ROLE_TARS,
            text,
            timings=json.dumps(kept) if kept else None,
            answered_by=answered_by,
            failed_at=failed_at,
            error=error[:MAX_ERROR] if error else None,
        )

    def _insert_turn(
        self,
        conversation_id: int,
        ts: float,
        role: str,
        text: str,
        **columns: str | None,
    ) -> int | None:
        """`columns`: the rest of the turn's columns, by name. None if the conversation is gone."""
        names = ", ".join(["conversation_id", "ts", "role", "text", *columns])
        marks = ", ".join("?" * (4 + len(columns)))
        with closing(self.store.connect()) as db:
            cursor = db.execute(
                f"INSERT INTO turns ({names}) SELECT {marks} WHERE EXISTS (SELECT 1 FROM conversations WHERE id=?)",
                (conversation_id, ts, role, text, *columns.values(), conversation_id),
            )
            return cursor.lastrowid if cursor.rowcount else None

    def mark_not_for_tars(self, turn_id: int) -> None:
        self.store.write("UPDATE turns SET not_for_tars=1 WHERE id=?", (turn_id,))

    def add_item(
        self,
        conversation_id: int | None,
        turn_id: int | None,
        item: SentItem,
        for_name: str | None = None,
        ts: float | None = None,
    ) -> int:
        """Kept even if the conversation was deleted in the web UI meanwhile; it's then on its own."""
        if item.kind not in KINDS:
            raise ValueError(f"unknown item kind {item.kind!r}")
        if item.scope not in SCOPES:
            raise ValueError(f"unknown item scope {item.scope!r}")
        file_name = (item.file_name or "file") if item.file_bytes is not None else None
        file_rel = self.store.save_file(item.file_bytes, file_name) if file_name else None
        entries = json.dumps([{"text": e, "done": False} for e in item.entries]) if item.entries is not None else None
        with self.store.removed_on_failure(file_rel):
            return self.store.write(
                "INSERT INTO items (conversation_id, turn_id, ts, kind, title, scope, for_name, url, site, "
                "description, body, entries, file, mime, size, file_name) "
                "VALUES ((SELECT id FROM conversations WHERE id=?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    turn_id,
                    ts or time.time(),
                    item.kind,
                    item.title,
                    item.scope,
                    for_name if item.scope == PERSON else None,
                    item.url,
                    item.site,
                    item.description,
                    item.body,
                    entries,
                    file_rel,
                    item.mime,
                    len(item.file_bytes) if file_name else None,
                    file_name,
                ),
            )

    def rate(self, turn_id: int, rating: str | None) -> None:
        self.store.write("UPDATE turns SET rating=? WHERE id=? AND role=?", (rating, turn_id, ROLE_TARS))

    def correct(self, turn_id: int, text: str | None) -> None:
        """What the person really said. Saving what TARS heard, or nothing, clears the correction."""
        self.store.write(
            "UPDATE turns SET corrected_text=CASE WHEN ?=text THEN NULL ELSE ? END WHERE id=? AND role=?",
            (text, text or None, turn_id, ROLE_PERSON),
        )

    def mark_seen(self, item_id: int, seen: bool = True) -> None:
        self.store.write("UPDATE items SET seen=? WHERE id=?", (int(seen), item_id))

    def tick(self, item_id: int, index: int, done: bool) -> None:
        """Raises IndexError for an entry the list doesn't have."""
        with self.store.transaction() as db:  # two phones ticking the same list mustn't undo each other
            row = db.execute("SELECT entries FROM items WHERE id=? AND kind=?", (item_id, LIST)).fetchone()
            entries = json.loads(row["entries"] or "[]") if row else []
            if not 0 <= index < len(entries):
                raise IndexError(index)
            entries[index]["done"] = done
            # Ticking an entry means someone has seen the list.
            db.execute("UPDATE items SET entries=?, seen=1 WHERE id=?", (json.dumps(entries), item_id))

    def delete_item(self, item_id: int) -> None:
        with self.store.transaction() as db:
            row = db.execute("SELECT file FROM items WHERE id=?", (item_id,)).fetchone()
            db.execute("DELETE FROM items WHERE id=?", (item_id,))
        self.store.remove([row["file"] if row else None])

    def delete(self, conversation_id: int) -> None:
        """The whole conversation: its turns, audio, items, and the wake that started it."""
        with self.store.transaction() as db:
            conv = db.execute("SELECT wake_event_id FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if not conv:
                return
            files = [
                r["audio"] for r in db.execute("SELECT audio FROM turns WHERE conversation_id=?", (conversation_id,))
            ]
            files += [
                r["file"] for r in db.execute("SELECT file FROM items WHERE conversation_id=?", (conversation_id,))
            ]
            db.execute("DELETE FROM items WHERE conversation_id=?", (conversation_id,))
            db.execute("DELETE FROM turns WHERE conversation_id=?", (conversation_id,))
            db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            if conv["wake_event_id"] is not None:
                files += self.events.delete_rows(db, conv["wake_event_id"])
        self.store.remove(files)

    def turn(self, turn_id: int) -> dict | None:
        rows = self.store.rows("SELECT * FROM turns WHERE id=?", (turn_id,))
        return rows[0] if rows else None

    def item(self, item_id: int) -> dict | None:
        """The raw row, including the stored file path the browser must never see."""
        rows = self.store.rows("SELECT * FROM items WHERE id=?", (item_id,))
        return rows[0] if rows else None

    def items(self) -> list[dict]:
        rows = self.store.rows("SELECT * FROM items ORDER BY ts DESC")
        people, voices = self._people(), self._voices([r["conversation_id"] for r in rows])
        return [_item(r, people, voices.get(r["conversation_id"])) for r in rows]

    def conversations(self, limit: int = 200, ids: list[int] | None = None) -> list[dict]:
        where, args = (f"WHERE c.id IN ({','.join('?' * len(ids))}) ", list(ids)) if ids is not None else ("", [])
        rows = self.store.rows(
            f"SELECT c.*, {FIRST_GUESS} AS guess FROM conversations c {where}ORDER BY c.started DESC LIMIT ?",
            (ROLE_PERSON, *args, limit),
        )
        if not rows:
            return []
        people, wakes = self._people(), self.events.wakes([r["wake_event_id"] for r in rows])
        voices = _voices(rows, wakes)
        out = [_conversation(r, wakes.get(r["wake_event_id"]), voices[r["id"]], people) for r in rows]
        self._add_turns(out, people, voices)
        return out

    def get(self, conversation_id: int) -> dict | None:
        found = self.conversations(ids=[conversation_id])
        return found[0] if found else None

    def _add_turns(self, convs: list[dict], people: dict, voices: dict) -> None:
        ids = [c["id"] for c in convs]
        marks = ",".join("?" * len(ids))
        turns = self.store.rows(f"SELECT * FROM turns WHERE conversation_id IN ({marks}) ORDER BY ts, id", ids)
        items = [
            _item(r, people, voices.get(r["conversation_id"]))
            for r in self.store.rows(f"SELECT * FROM items WHERE conversation_id IN ({marks}) ORDER BY ts, id", ids)
        ]
        by_turn: dict[int, list[dict]] = {}
        for i in items:
            by_turn.setdefault(i["turn_id"], []).append(i)
        by_conv: dict[int, list[dict]] = {}
        for t in turns:
            by_conv.setdefault(t["conversation_id"], []).append(
                _turn(t, by_turn.get(t["id"], []), people, voices.get(t["conversation_id"]))
            )
        for c in convs:
            c["turns"] = by_conv.get(c["id"], [])

    def _voices(self, conversation_ids: list[int | None]) -> dict[int, "Voice"]:
        ids = sorted({i for i in conversation_ids if i is not None})
        if not ids:
            return {}
        rows = self.store.rows(
            f"SELECT c.id, c.wake_event_id, {FIRST_GUESS} AS guess FROM conversations c "
            f"WHERE c.id IN ({','.join('?' * len(ids))})",
            (ROLE_PERSON, *ids),
        )
        return _voices(rows, self.events.wakes([r["wake_event_id"] for r in rows]))

    def _people(self) -> dict[str, dict]:
        """Named voices by name key, to connect speaker ID's guesses to the voices in the web UI."""
        return {person_key(c["name"]): c for c in self.events.clusters() if person_key(c["name"])}


# Speaker ID's guess for a conversation's first request; bind ROLE_PERSON to its parameter.
FIRST_GUESS = (
    "(SELECT t.speaker FROM turns t WHERE t.conversation_id = c.id AND t.role = ? ORDER BY t.ts, t.id LIMIT 1)"
)


@dataclass
class Voice:
    """Who a conversation's first request was: speaker ID's guess at the time, and the wake's voice cluster when that
    should win. The wake's cluster is about that very clip, so it wins when a person decided it ("that was Stacey")
    or when speaker ID had no guess. It then applies to every turn and item speaker ID guessed the same way; a turn
    heard as someone else (an aside) keeps its own."""

    guess: str | None
    wake: dict | None

    def applies_to(self, guess: str | None) -> bool:
        return self.wake is not None and person_key(guess) == person_key(self.guess)


def _voices(rows: list[dict], wakes: dict[int, dict]) -> dict[int, Voice]:
    """Who each conversation's first request was; rows need id, wake_event_id and FIRST_GUESS as guess."""
    return {r["id"]: Voice(r["guess"], _wake_voice(wakes.get(r["wake_event_id"]), r["guess"])) for r in rows}


def _wake_voice(wake: dict | None, first_guess: str | None) -> dict | None:
    if wake and wake["cluster_id"] is not None and (wake["voice_decided"] or not person_key(first_guess)):
        return {"cluster_id": wake["cluster_id"], "name": wake["cluster_name"]}
    return None


def _speaker(guess: str | None, people: dict, voice: Voice | None) -> dict | None:
    if voice and voice.applies_to(guess):
        return dict(voice.wake)
    if person_key(guess):
        c = people.get(person_key(guess))
        return {"cluster_id": c["id"], "name": c["name"]} if c else {"cluster_id": None, "name": guess.capitalize()}
    return None


def _conversation(row: dict, wake: dict | None, voice: Voice | None, people: dict) -> dict:
    out = {k: row[k] for k in ("id", "started", "ended")}
    out["wake"] = (
        {
            "event_id": wake["id"],
            "heard": wake["heard"],
            "confidence": wake["confidence"],
            "outcome": wake["outcome"],
            "label": wake["label"],
            "cluster_id": wake["cluster_id"],
            "has_request_audio": bool(wake["has_request_audio"]),
        }
        if wake
        else None
    )
    out["speaker"] = _speaker(voice.guess if voice else None, people, voice)
    return out


def _turn(row: dict, items: list[dict], people: dict, voice: Voice | None) -> dict:
    out = {"id": row["id"], "ts": row["ts"], "role": row["role"], "text": row["text"]}
    if row["role"] == ROLE_PERSON:
        out.update(
            has_audio=bool(row["audio"]),
            speaker=_speaker(row["speaker"], people, voice),
            corrected_text=row["corrected_text"],
            not_for_tars=bool(row["not_for_tars"]),
        )
    else:
        out.update(
            rating=row["rating"],
            items=items,
            timings=json.loads(row["timings"]) if row["timings"] else None,
            answered_by=row["answered_by"],
            failed_at=row["failed_at"],
            error=row["error"],
        )
    return out


def _item(row: dict, people: dict, voice: Voice | None) -> dict:
    out = {k: row[k] for k in ("id", "ts", "kind", "title", "scope", "conversation_id", "turn_id")}
    out["seen"] = bool(row["seen"])
    # Sent "for whoever asked": if a person corrected who that was, the item follows.
    out["for"] = _speaker(row["for_name"], people, voice) if row["scope"] == PERSON and row["for_name"] else None
    if row["kind"] == LINK:
        out.update(url=row["url"], site=row["site"], description=row["description"])
    elif row["kind"] == NOTE:
        out["body"] = row["body"]
    elif row["kind"] == LIST:
        out["entries"] = json.loads(row["entries"] or "[]")
    elif row["kind"] == FILE:
        out.update(name=row["file_name"], mime=row["mime"], size=row["size"])
    return out
