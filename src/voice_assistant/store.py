"""Where self-learning TARS keeps everything: one SQLite database plus the audio and files next to it.

The assistant and the web UI share the folder (voice_data/events/, gitignored) from two processes, so the database
runs in WAL mode and every multi-step change is one IMMEDIATE transaction. events.py (wakes, near-misses, voices),
conversations.py (turns, sent items) and reminders.py (timers, reminders, messages) build on this.
"""

import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import numpy as np

from .audio import save_wav

# AUTOINCREMENT so an id is never reused: the web UI deletes rows while the assistant adds them, and a reused id
# would silently attach a new wake to an old conversation (or delete it along with one).
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
    not_for_tars INTEGER DEFAULT 0,  -- person turns: overheard, TARS stayed quiet
    corrected_text TEXT,             -- person turns: what they really said, typed in the web UI
    rating TEXT,                     -- tars turns: good | bad, from the web UI
    timings TEXT,                    -- tars turns: JSON seconds by stage, as in the assistant's log (see TIMINGS)
    answered_by TEXT,                -- tars turns: quick | look_up | ponder | fallback | openai (llm.QUICK...)
    failed_at TEXT,                  -- tars turns that failed: stt | llm | tts | other
    error TEXT,                      -- tars turns that failed: what went wrong
    quick_service TEXT               -- tars turns: the service whose quick reply was used (Groq, Cerebras)
)""",
    "reminders": """CREATE TABLE reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    kind TEXT NOT NULL,              -- timer | reminder | message
    text TEXT,                       -- what to say; a timer's optional label
    for_name TEXT,                   -- who it's for, by name; NULL = whoever is there
    from_name TEXT,                  -- who set it, if known
    set_via TEXT NOT NULL,           -- voice | web
    conversation_id INTEGER,         -- the conversation it was set in, by voice
    due REAL,                        -- when; NULL = when for_name's voice is next heard
    needs_ack INTEGER NOT NULL,      -- 1 = said again until acknowledged
    repeat_every_s REAL NOT NULL,
    max_tries INTEGER NOT NULL,
    status TEXT NOT NULL,            -- scheduled | waiting | acknowledged | said | missed | cancelled
    tries INTEGER NOT NULL DEFAULT 0,
    next_at REAL,                    -- when it's next said; NULL while waiting for someone's voice, or when over
    last_said REAL,
    acked_at REAL,
    acked_by TEXT,                   -- the recognized voice; NULL = unknown voice, or the web UI
    acked_via TEXT                   -- voice | web
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
INDEXES = [
    "CREATE INDEX events_ts ON events(ts)",
    "CREATE INDEX events_cluster ON events(cluster_id)",
    "CREATE INDEX turns_conversation ON turns(conversation_id)",
    "CREATE INDEX turns_audio ON turns(audio)",  # the first request's audio is shared with its wake
    "CREATE INDEX items_conversation ON items(conversation_id)",
    "CREATE INDEX reminders_status ON reminders(status)",
]
# Step n brings a database from version n - 1 (PRAGMA user_version) to n; a new database is made at the latest
# version from TABLES and INDEXES. A change to them needs a new step here, and a step that has shipped never changes.
MIGRATIONS = [
    [
        "ALTER TABLE turns DROP COLUMN speaker_score",
        "ALTER TABLE turns DROP COLUMN embedding",
        "CREATE INDEX events_cluster ON events(cluster_id)",
        "CREATE INDEX turns_audio ON turns(audio)",
    ],
    [
        "ALTER TABLE turns ADD COLUMN timings TEXT",
        "ALTER TABLE turns ADD COLUMN answered_by TEXT",
        "ALTER TABLE turns ADD COLUMN failed_at TEXT",
        "ALTER TABLE turns ADD COLUMN error TEXT",
    ],
    [
        (
            "CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, kind TEXT NOT NULL, "
            "text TEXT, for_name TEXT, from_name TEXT, set_via TEXT NOT NULL, conversation_id INTEGER, due REAL, "
            "needs_ack INTEGER NOT NULL, repeat_every_s REAL NOT NULL, max_tries INTEGER NOT NULL, "
            "status TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0, next_at REAL, last_said REAL, acked_at REAL, "
            "acked_by TEXT, acked_via TEXT)"
        ),
        "CREATE INDEX reminders_status ON reminders(status)",
    ],
    ["ALTER TABLE turns ADD COLUMN quick_service TEXT"],
]


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
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """IMMEDIATE takes the write lock up front, so a read-then-write (ticking a list) can't lose a race."""
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                if db.in_transaction:  # SQLite has already rolled back after some errors (a full disk)
                    db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def write(self, sql: str, args: tuple = ()) -> int:
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

    @contextmanager
    def removed_on_failure(self, rel: str | None) -> Iterator[None]:
        """A file just stored is deleted again if the row meant to point at it can't be written."""
        try:
            yield
        except BaseException:
            self.remove([rel])
            raise

    def remove(self, rels: list[str | None]) -> None:
        """Call only after the rows that pointed at these files are gone."""
        for rel in rels:
            if rel:
                (self.folder / rel).unlink(missing_ok=True)


def _migrate(db: sqlite3.Connection) -> None:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone():
        for statement in [*TABLES.values(), *INDEXES]:
            db.execute(statement)
    else:
        for steps in MIGRATIONS[version:]:
            for statement in steps:
                db.execute(statement)
    db.execute(f"PRAGMA user_version={len(MIGRATIONS)}")
