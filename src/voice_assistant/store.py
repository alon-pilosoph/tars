"""Where self-learning TARS keeps everything: one SQLite database plus the audio and files next to it.

The assistant writes and the web UI reads and edits the same folder (voice_data/events/, gitignored) from two
processes, so the database runs in WAL mode and every multi-step change is one IMMEDIATE transaction.
events.py (wakes, near-misses, voices) and conversations.py (turns, sent items) are built on top of this.
"""

import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import numpy as np

from .audio import save_wav

# AUTOINCREMENT so an id is never handed out twice: the web UI deletes rows while the assistant keeps adding them,
# and a reused id would silently attach a new wake to an old conversation (or delete it along with one).
TABLES = {
    "events": """CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,              -- wake | near_miss
    wake_score REAL,
    outcome TEXT,                    -- answer | ask | ignore (wakes only)
    heard TEXT,                      -- what the double-check heard
    confidence REAL,                 -- the double-check's learned-layer confidence, if any
    wake_model TEXT,
    check_model TEXT,
    audio TEXT,                      -- the window the double-check heard (relative to the folder)
    utterance_audio TEXT,            -- the request that followed, if any
    transcript TEXT,
    follow TEXT,                     -- asked | said_nothing | not_for_us
    speaker TEXT,                    -- speaker ID's guess at the time
    speaker_score REAL,
    embedding BLOB,                  -- voice embedding of the request (for clustering)
    label TEXT,                      -- real | not_real, given by a person (wins over auto_label)
    cluster_id INTEGER,
    cluster_pinned INTEGER DEFAULT 0 -- 1 = a person put it there; re-clustering leaves it alone
)""",
    "clusters": """CREATE TABLE clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,                       -- a person's name once someone names the cluster
    kind TEXT DEFAULT 'unknown',     -- person | not_person | unknown
    created REAL NOT NULL
)""",
    "conversations": """CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started REAL NOT NULL,
    ended REAL,
    wake_event_id INTEGER            -- the wake that started it (events.id), if any
)""",
    "turns": """CREATE TABLE turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    role TEXT NOT NULL,              -- person | tars
    text TEXT,
    audio TEXT,                      -- person turns: what they said (the first request shares the wake's copy)
    speaker TEXT,                    -- person turns: speaker ID's guess at the time
    speaker_score REAL,
    embedding BLOB,                  -- person turns after the first: voice embedding, for clustering later
    not_for_tars INTEGER DEFAULT 0,  -- person turns: overheard, TARS stayed quiet
    corrected_text TEXT,             -- person turns: what they really said, typed in the web UI
    rating TEXT                      -- tars turns: good | bad, from the web UI
)""",
    "items": """CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER,
    turn_id INTEGER,                 -- the TARS turn that sent it
    ts REAL NOT NULL,
    kind TEXT NOT NULL,              -- link | note | list | file
    title TEXT NOT NULL,
    scope TEXT NOT NULL,             -- person | household
    for_name TEXT,                   -- scope person: who asked (speaker ID), if known
    seen INTEGER DEFAULT 0,
    url TEXT, site TEXT, description TEXT,  -- link
    body TEXT,                       -- note: short markdown
    entries TEXT,                    -- list: JSON [{"text": ..., "done": false}]
    file TEXT, mime TEXT, size INTEGER,     -- file: where it's stored (relative to the folder)
    file_name TEXT                   -- file: the name it downloads as
)""",
}
# Columns added after a table first shipped: an existing database gets them on startup.
ADDED_COLUMNS = {"items": {"file_name": "TEXT"}}
INDEXES = """
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS turns_conversation ON turns(conversation_id);
CREATE INDEX IF NOT EXISTS items_conversation ON items(conversation_id);
"""


class Store:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            _migrate(db)

    def connect(self) -> sqlite3.Connection:
        # Autocommit: a single statement is atomic on its own, and transaction() opens the multi-step ones.
        db = sqlite3.connect(self.folder / "events.db", timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")  # the web UI reads while the assistant writes
        return db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """IMMEDIATE takes the write lock up front, so a read-then-write (ticking a list) can't lose a race."""
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def write(self, sql: str, args: tuple = ()) -> int:
        """Run one statement; returns the new row's id (for an INSERT)."""
        with closing(self.connect()) as db:
            return db.execute(sql, args).lastrowid

    def rows(self, sql: str, args: tuple | list = ()) -> list[dict]:
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute(sql, args)]

    def save_audio(self, pcm: np.ndarray | bytes, name: str) -> str:
        rel = f"audio/{time.strftime('%Y%m%d')}/{name}.wav"
        save_wav(self.folder / rel, pcm)
        return rel

    def save_file(self, data: bytes, name: str) -> str:
        """Stored under a unique name (two files can share a name, even within a millisecond)."""
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:80] or "file"
        rel = f"files/{time.strftime('%Y%m%d')}/{int(time.time() * 1000)}_{secrets.token_hex(3)}_{safe}"
        path = self.folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return rel

    def remove(self, rels: list[str | None]) -> None:
        """Delete stored audio and files. Called after the rows that pointed at them are gone."""
        for rel in rels:
            if rel:
                (self.folder / rel).unlink(missing_ok=True)


def _migrate(db: sqlite3.Connection) -> None:
    """Create what's missing and bring an older database up to date, keeping every row."""
    for table, create in TABLES.items():
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row is None:
            db.execute(create)
        elif "AUTOINCREMENT" not in row["sql"].upper():
            old = [r["name"] for r in db.execute(f"PRAGMA table_info({table})")]
            db.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
            db.execute(create)
            new = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
            cols = ", ".join(c for c in old if c in new)
            # Explicit ids carry over, and AUTOINCREMENT's counter starts above the highest one.
            db.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {table}_old")
            db.execute(f"DROP TABLE {table}_old")
        else:
            have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
            for name, sql_type in ADDED_COLUMNS.get(table, {}).items():
                if name not in have:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
    for statement in filter(str.strip, INDEXES.split(";")):
        db.execute(statement)
    # Before AUTOINCREMENT, deleting a wake could leave its conversation pointing at a reused id.
    db.execute("UPDATE conversations SET wake_event_id=NULL WHERE wake_event_id NOT IN (SELECT id FROM events)")
