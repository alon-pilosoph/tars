"""Every conversation TARS has: what each person said, what TARS answered, and what it sent them.

Lives in the same store as the event log (see store.py) and links each conversation to the wake that started it.
Items (links, notes, lists, files) are what TARS "sends": it says it sent them, and they show up in the web UI.
There are no private inboxes: an item is for a person (whoever asked) or the household, and anyone at home can
see it.
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .events import EventLog, person_key

PERSON, TARS = "person", "tars"
GOOD, BAD = "good", "bad"
LINK, NOTE, LIST, FILE = "link", "note", "list", "file"
KINDS = (LINK, NOTE, LIST, FILE)
HOUSEHOLD = "household"
SCOPES = (PERSON, HOUSEHOLD)


@dataclass
class SentItem:
    kind: str
    title: str
    scope: str = PERSON  # person: whoever asked; household: shared, like a shopping list
    url: str | None = None  # link
    site: str | None = None
    description: str | None = None
    body: str | None = None  # note: short markdown
    entries: list[str] | None = None  # list
    file_name: str | None = None  # file
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
        speaker_score: float | None = None,
        embedding: np.ndarray | None = None,
        audio: str | None = None,
        ts: float | None = None,
    ) -> int | None:
        """`audio` is audio that's already stored (the request that came with the wake keeps its one copy on the
        wake), else `pcm` is saved. None if the conversation is gone (deleted from the web UI mid-conversation)."""
        if not self.exists(conversation_id):
            return None
        ts = ts or time.time()
        saved = self.store.save_audio(pcm, f"{int(ts * 1000)}_turn") if audio is None and pcm is not None else None
        blob = np.asarray(embedding, dtype=np.float32).tobytes() if embedding is not None and saved else None
        turn = self._insert_turn(conversation_id, ts, PERSON, text, audio or saved, speaker, speaker_score, blob)
        if turn is None:
            self.store.remove([saved])
        return turn

    def add_tars_turn(self, conversation_id: int, text: str, ts: float | None = None) -> int | None:
        return self._insert_turn(conversation_id, ts or time.time(), TARS, text)

    def _insert_turn(self, conversation_id: int, ts: float, role: str, text: str, *person_fields) -> int | None:
        columns = ["conversation_id", "ts", "role", "text", "audio", "speaker", "speaker_score", "embedding"]
        values = (conversation_id, ts, role, text, *person_fields)
        with self.store.transaction() as db:
            cursor = db.execute(
                f"INSERT INTO turns ({', '.join(columns[: len(values)])}) SELECT {', '.join('?' * len(values))} "
                "WHERE EXISTS (SELECT 1 FROM conversations WHERE id=?)",
                (*values, conversation_id),
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
        if item.kind not in KINDS:
            raise ValueError(f"unknown item kind {item.kind!r}")
        if item.scope not in SCOPES:
            raise ValueError(f"unknown item scope {item.scope!r}")
        file_rel = (
            self.store.save_file(item.file_bytes, item.file_name or "file") if item.file_bytes is not None else None
        )
        entries = json.dumps([{"text": e, "done": False} for e in item.entries]) if item.entries is not None else None
        return self.store.write(
            "INSERT INTO items (conversation_id, turn_id, ts, kind, title, scope, for_name, url, site, description, "
            "body, entries, file, mime, size, file_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                len(item.file_bytes) if item.file_bytes is not None else None,
                item.file_name if file_rel else None,
            ),
        )

    def rate(self, turn_id: int, rating: str | None) -> None:
        self.store.write("UPDATE turns SET rating=? WHERE id=? AND role=?", (rating, turn_id, TARS))

    def correct(self, turn_id: int, text: str | None) -> None:
        """What they really said. Saving what TARS heard, or nothing, clears the correction."""
        self.store.write(
            "UPDATE turns SET corrected_text=CASE WHEN ?=text THEN NULL ELSE ? END WHERE id=? AND role=?",
            (text, text or None, turn_id, PERSON),
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
            # Ticking something off means someone has seen the list.
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
        rows = self.store.rows("SELECT * FROM items WHERE id=?", (item_id,))
        return self._items(rows)[0] if rows else None

    def item_file(self, item_id: int) -> str | None:
        """Where a sent file is stored (relative to the folder); the browser never sees this."""
        rows = self.store.rows("SELECT file FROM items WHERE id=?", (item_id,))
        return rows[0]["file"] if rows else None

    def items(self, person: str | None = None, unseen: bool = False, limit: int = 500) -> list[dict]:
        """Newest first. `person` keeps that person's items (after any voice correction) plus the household's."""
        sql = "SELECT * FROM items" + (" WHERE seen=0" if unseen else "") + " ORDER BY ts DESC"
        items = self._items(self.store.rows(sql + ("" if person else " LIMIT ?"), () if person else (limit,)))
        if person:
            key = person_key(person)
            items = [i for i in items if i["scope"] == HOUSEHOLD or person_key((i["for"] or {}).get("name")) == key]
        return items[:limit]

    def unseen_count(self) -> int:
        return self.store.rows("SELECT COUNT(*) AS n FROM items WHERE seen=0")[0]["n"]

    def conversations(self, limit: int = 200, turns: bool = True, ids: list[int] | None = None) -> list[dict]:
        """Newest first, each with its turns (the timeline shows them), or just a summary with `turns=False`."""
        where, args = (f"WHERE c.id IN ({','.join('?' * len(ids))}) ", list(ids)) if ids is not None else ("", [])
        rows = self.store.rows(
            "SELECT c.*, "
            " (SELECT COUNT(*) FROM turns t WHERE t.conversation_id=c.id) AS turn_count, "
            " (SELECT text FROM turns t WHERE t.conversation_id=c.id AND t.role=? ORDER BY ts, id LIMIT 1) AS preview, "
            " (SELECT COUNT(*) FROM items i WHERE i.conversation_id=c.id) AS item_count, "
            " (SELECT COUNT(*) FROM items i WHERE i.conversation_id=c.id AND i.seen=0) AS unseen_count "
            f"FROM conversations c {where}ORDER BY c.started DESC LIMIT ?",
            (PERSON, *args, limit),
        )
        people, voices = self._people(), self._voices([r["id"] for r in rows])
        wakes = self.events.wakes([r["wake_event_id"] for r in rows])
        out = [_conversation(r, wakes.get(r["wake_event_id"]), voices.get(r["id"]), people) for r in rows]
        if turns and out:
            self._add_turns(out, people, voices)
        return out

    def get(self, conversation_id: int) -> dict | None:
        found = self.conversations(turns=True, ids=[conversation_id])
        if not found:
            return None
        conv = found[0]
        conv["items"] = [i for t in conv["turns"] for i in t.get("items", [])]
        return conv

    def _add_turns(self, convs: list[dict], people: dict, voices: dict) -> None:
        ids = [c["id"] for c in convs]
        marks = ",".join("?" * len(ids))
        turns = self.store.rows(f"SELECT * FROM turns WHERE conversation_id IN ({marks}) ORDER BY ts, id", ids)
        items = self._items(
            self.store.rows(f"SELECT * FROM items WHERE conversation_id IN ({marks}) ORDER BY ts, id", ids),
            people,
            voices,
        )
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

    def _items(self, rows: list[dict], people: dict | None = None, voices: dict | None = None) -> list[dict]:
        if people is None:
            people = self._people()
        if voices is None:
            voices = self._voices([r["conversation_id"] for r in rows])
        return [_item(r, people, voices.get(r["conversation_id"])) for r in rows]

    def _voices(self, conversation_ids: list[int | None]) -> dict[int, "Voice"]:
        """For each conversation: speaker ID's guess for its first request, and the voice that wins over it."""
        ids = sorted({i for i in conversation_ids if i is not None})
        if not ids:
            return {}
        rows = self.store.rows(
            "SELECT c.id, c.wake_event_id, (SELECT t.speaker FROM turns t WHERE t.conversation_id = c.id "
            f" AND t.role = ? ORDER BY t.ts, t.id LIMIT 1) AS guess FROM conversations c "
            f"WHERE c.id IN ({','.join('?' * len(ids))})",
            (PERSON, *ids),
        )
        wakes = self.events.wakes([r["wake_event_id"] for r in rows])
        return {r["id"]: Voice(r["guess"], _wake_voice(wakes.get(r["wake_event_id"]), r["guess"])) for r in rows}

    def _people(self) -> dict[str, dict]:
        """Named voices by name key, to connect speaker ID's guesses to the voices in the web UI."""
        return {person_key(c["name"]): c for c in self.events.clusters() if person_key(c["name"])}


@dataclass
class Voice:
    """Who a conversation's first request was: speaker ID's guess at the time, and the wake's voice cluster when
    that should win over it. The request that came with the wake is the clip the wake's cluster is about, so the
    cluster wins when a person decided it ("that was Stacey"), and also when speaker ID had no guess at all. It
    then applies to every turn and item speaker ID guessed the same way; a turn it heard as someone else (an
    aside) keeps its own."""

    guess: str | None
    wake: dict | None

    def applies_to(self, guess: str | None) -> bool:
        return self.wake is not None and person_key(guess) == person_key(self.guess)


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
    out = {k: row.get(k) for k in ("id", "started", "ended", "turn_count", "preview", "item_count", "unseen_count")}
    out["wake"] = (
        {"event_id": wake["id"], "heard": wake["heard"], "confidence": wake["confidence"], "outcome": wake["outcome"]}
        if wake
        else None
    )
    out["speaker"] = _speaker(voice.guess if voice else None, people, voice)
    return out


def _turn(row: dict, items: list[dict], people: dict, voice: Voice | None) -> dict:
    out = {"id": row["id"], "ts": row["ts"], "role": row["role"], "text": row["text"]}
    if row["role"] == PERSON:
        out.update(
            has_audio=bool(row["audio"]),
            speaker=_speaker(row["speaker"], people, voice),
            corrected_text=row["corrected_text"],
            not_for_tars=bool(row["not_for_tars"]),
        )
    else:
        out.update(rating=row["rating"], items=items)
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
        # Files stored before file_name existed only have the name inside their storage path.
        name = row["file_name"] or (Path(row["file"]).name.split("_", 1)[-1] if row["file"] else None)
        out.update(name=name, mime=row["mime"], size=row["size"])
    return out
