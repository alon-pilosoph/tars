"""Where self-learning TARS keeps everything: one SQLite database plus the audio and files next to it.

The assistant and the web UI share the folder (voice_data/events/, gitignored) from two processes, so the database
runs in WAL mode and every multi-step change is one IMMEDIATE transaction. events.py (wakes, near-misses, voices),
conversations.py (turns, sent items), reminders.py (timers, reminders, messages), presence.py (who's home),
recording.py (recording sessions and their takes), browser/errands.py (the errands queued for TARS's browser) and
feeds.py (the feeds each person follows), skills.py (what TARS is taught: skills and preferences) and myself.py (the
wishes: what TARS was asked for and couldn't do) build on this.
"""

import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import numpy as np

from .audio import save_wav
from .config import ConfigError

DB_FILE = "events.db"  # in the event log's folder

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
    wake_event_id INTEGER,           -- the wake that started it (events.id), if any
    via TEXT                         -- telegram: a chat on Telegram; NULL = by voice
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
    answered_by TEXT,                -- tars turns: quick | look_up | ponder | fallback | openai | pi (llm.QUICK...)
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
    set_via TEXT NOT NULL,           -- voice | web | telegram
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
    acked_via TEXT,                  -- voice | web | telegram
    channel TEXT NOT NULL DEFAULT 'voice', -- where it's said: voice (aloud at home) | telegram (to their chat)
    reported TEXT,                   -- set in a chat, said aloud: the last status told back to that chat
    kept_aloud INTEGER NOT NULL DEFAULT 0  -- 1: Telegram couldn't post it, so it stays aloud whoever's out
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
    file_name TEXT,                  -- file: the name it downloads as
    delivered REAL                   -- when it was posted to its person's Telegram chat, if it was
)""",
    "remote_files": """CREATE TABLE remote_files (
    path TEXT PRIMARY KEY,           -- as the rows that keep it name it (relative to the folder)
    uploaded REAL NOT NULL,          -- when it went to the bucket ([backup], backup.py)
    size INTEGER NOT NULL,           -- as uploaded: a copy fetched back must be as long
    offloaded REAL,                  -- when it was removed from the machine, being in the bucket; NULL = on both
    fetched REAL                     -- when it was last brought back from the bucket, if it was
)""",
    "meta": """CREATE TABLE meta (
    key TEXT PRIMARY KEY,            -- "id": this database's own, which a backup marks its bucket with (backup.py);
                                     -- "presence_routers": how each router last answered the assistant (presence.py)
    value TEXT NOT NULL
)""",
    "errands": """CREATE TABLE errands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    person TEXT NOT NULL,            -- whose: their accounts, their chat
    goal TEXT NOT NULL,              -- the errand, as the brain put it
    asked_via TEXT NOT NULL,         -- voice (aloud at home) | telegram
    status TEXT NOT NULL,            -- queued | running | done | failed
    started REAL,                    -- when the Telegram service took it
    ended REAL,
    answer TEXT                      -- what the person was told
)""",
    "presence": """CREATE TABLE presence (
    person TEXT PRIMARY KEY,         -- as [presence] people names them
    home INTEGER,                    -- 1 home, 0 out; NULL until the routers have said (presence.py)
    since REAL,                      -- when they came home or went out, as far as the routers can tell
    last_seen REAL,                  -- when a router last saw their phone
    updated REAL NOT NULL,           -- when the assistant last wrote this row
    via TEXT                         -- home: the door they opened from outside to come in (doors.py); NULL: by phone
)""",
    "usage": """CREATE TABLE usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,                -- when the call ended
    service TEXT NOT NULL,           -- openai, cerebras, groq, anthropic, deepgram, cartesia
    kind TEXT NOT NULL,              -- llm, stt or tts (usage.py)
    model TEXT NOT NULL,             -- the model, or the voice
    person TEXT NOT NULL DEFAULT '', -- who it was for (person_key); '' when not known
    channel TEXT NOT NULL DEFAULT '',-- voice, telegram, web, errand, skill or background
    room TEXT NOT NULL DEFAULT '',   -- where it was heard, once there are rooms
    input INTEGER NOT NULL DEFAULT 0,      -- tokens, for llm
    output INTEGER NOT NULL DEFAULT 0,
    reasoning INTEGER NOT NULL DEFAULT 0,  -- of output
    cached INTEGER NOT NULL DEFAULT 0,     -- of input
    searches INTEGER NOT NULL DEFAULT 0,   -- web searches made in the call
    seconds REAL NOT NULL DEFAULT 0,       -- audio heard, for stt
    characters INTEGER NOT NULL DEFAULT 0, -- text spoken, for tts
    reported INTEGER NOT NULL DEFAULT 1    -- 0: the provider said nothing of it (a call cut off): counted, not measured
)""",
    "door_events": """CREATE TABLE door_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    door TEXT NOT NULL,              -- as [[presence.doors]] names it
    action INTEGER NOT NULL,         -- the lock's: 1 unlock, 2 lock, 3 unlatch, 4 Lock 'n' Go... (doors.ACTIONS)
    trigger INTEGER NOT NULL,        -- 0 an app (Auto Unlock too), 1 by hand, 2 its button, 6 auto lock, 172 MQTT...
    key TEXT NOT NULL,               -- key:<the app's auth id> or code:<the keypad code's id>
    auto_unlock INTEGER NOT NULL,    -- 1: the phone's Auto Unlock did it
    person TEXT,                     -- whose key it is (person_key), as known then; NULL: nobody's named it
    guess TEXT,                      -- an unnamed key's opening: whose phone came home around then, if just one did
    guess_at REAL,                   -- when that phone came home: two openings in one homecoming are one arrival
    settled INTEGER NOT NULL DEFAULT 0  -- 1: compared with who came home (guess is final)
)""",
    "door_keys": """CREATE TABLE door_keys (
    key TEXT PRIMARY KEY,            -- as door_events.key
    person TEXT NOT NULL,            -- person_key, learned from their phone coming home as the door opened
    learned REAL NOT NULL
)""",
    "sessions": """CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,      -- the link's key (recording.py)
    kind TEXT NOT NULL,              -- voice | house
    person TEXT NOT NULL,            -- whose voice, as TARS knows them
    created REAL NOT NULL,
    expires REAL NOT NULL,
    prompts TEXT NOT NULL,           -- JSON: what to say, in order (recording.Prompt)
    skipped TEXT NOT NULL DEFAULT '[]', -- JSON: the prompts skipped, by id
    finished REAL,                   -- when the last prompt was done; the link works no more
    cloud INTEGER NOT NULL DEFAULT 0 -- 1: a house session whose takes may also go to Deepgram, as its page said
)""",
    "takes": """CREATE TABLE takes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    prompt TEXT NOT NULL,            -- the prompt's id; one take a prompt (a redo replaces it)
    ts REAL NOT NULL,
    audio TEXT,                      -- NULL when nothing was heard
    seconds REAL NOT NULL,
    problem TEXT,                    -- nothing | quiet | loud | cut_off | short (recording.check); NULL = fine
    stopped TEXT NOT NULL            -- silence | tap | nobody: how the take ended (recording.py)
)""",
    "feeds": """CREATE TABLE feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL,            -- whose: as person_key has them (feeds.py)
    category TEXT NOT NULL,          -- theirs, in their words: "gaming", "israeli news"
    name TEXT NOT NULL,              -- the feed's, as it's told: "Ynet"
    url TEXT NOT NULL,               -- the feed itself, found from the site if a site was given
    added REAL NOT NULL
)""",
    "feed_seen": """CREATE TABLE feed_seen (
    person TEXT NOT NULL,
    link TEXT NOT NULL,              -- a story handed to the model for them: "what's new" is what isn't here
    at REAL NOT NULL,
    PRIMARY KEY (person, link)
)""",
    "skills": """CREATE TABLE skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,             -- a person's name, as TARS knows them, or "household" (skills.py)
    name TEXT NOT NULL,              -- lower case: what it's asked for by
    instructions TEXT NOT NULL,      -- what to do, in plain words
    purpose TEXT NOT NULL DEFAULT '', -- what it's for, so it's found by more than its name
    at TEXT NOT NULL DEFAULT '[]',   -- JSON: times of day it runs by itself, "19:55"
    days TEXT NOT NULL DEFAULT '[]', -- JSON: "mon"...; empty: every day
    say TEXT NOT NULL DEFAULT 'aloud', -- aloud | chat: where it says what it has to, run by itself
    paused INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    updated_by TEXT NOT NULL,        -- who taught it, or last changed it
    trigger TEXT,                    -- JSON: something happening it runs on (skills.trigger); NULL: none
    once REAL,                       -- when it runs, the one time, then it's gone; NULL: not a one-off
    tries TEXT                       -- JSON: a one-off's tries so far, and who has one in hand (skills.OneOffs)
)""",
    "skill_runs": """CREATE TABLE skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id INTEGER,                -- the skill's, which may have been deleted since
    name TEXT NOT NULL,
    owner TEXT NOT NULL,
    at REAL NOT NULL,
    why TEXT NOT NULL,               -- "at 19:55", "Stacey just came home"
    said TEXT NOT NULL,              -- what it said; "" when there was nothing to do
    failed INTEGER NOT NULL DEFAULT 0 -- a one-off that didn't happen, given up; said: what they were told
)""",
    "wishes": """CREATE TABLE wishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    person TEXT,                     -- who asked, as TARS knew them; NULL: not known
    via TEXT NOT NULL,               -- voice | chat
    asked TEXT NOT NULL,             -- what they wanted, in a sentence
    why TEXT NOT NULL,               -- what stopped TARS, and what would make it possible
    granted REAL,                    -- when it was done (marked, or its build installed); NULL: still wished for
    build TEXT,                      -- having it built (wishes.py): offered, building, ready, merged, failed, granted
    issue INTEGER,                   -- the GitHub issue asking for it
    pr INTEGER,                      -- the pull request that builds it
    sha TEXT,                        -- the pull request's merge commit
    tries INTEGER NOT NULL DEFAULT 0,  -- times its CI failed and it was sent back to be fixed
    checked TEXT,                    -- the commit it was last sent back for
    built_at REAL,                   -- when a maintainer said to build it
    head TEXT,                       -- the pull request's latest commit, as last seen
    moved REAL                       -- when its build last moved: a new commit, or sent back to be fixed
)""",
    "web_invites": """CREATE TABLE web_invites (
    token TEXT PRIMARY KEY,          -- sha256 of the one-time token in the link (signin.py)
    person TEXT NOT NULL,
    expires REAL NOT NULL,
    used REAL                        -- when a passkey was made with it; NULL: not yet
)""",
    "passkeys": """CREATE TABLE passkeys (
    id TEXT PRIMARY KEY,             -- the credential's id, base64url
    person TEXT NOT NULL,
    public_key BLOB NOT NULL,        -- COSE
    sign_count INTEGER NOT NULL,
    created REAL NOT NULL,
    last_used REAL,
    device TEXT                      -- the browser it was made in, as it said (its user agent)
)""",
    "web_sessions": """CREATE TABLE web_sessions (
    token TEXT PRIMARY KEY,          -- sha256 of the session cookie
    person TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL
)""",
    "settings_log": """CREATE TABLE settings_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    person TEXT,                     -- who asked TARS to change it; NULL: not known
    name TEXT NOT NULL,              -- section.key, as settings.ADJUSTABLE names it
    old TEXT NOT NULL,               -- JSON: what it was
    new TEXT NOT NULL                -- JSON: what it became
)""",
    "preferences": """CREATE TABLE preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL,            -- as person_key has them
    about TEXT NOT NULL DEFAULT '',  -- "gaming news", "the AC"
    text TEXT NOT NULL,              -- the preference, in plain words
    guessed INTEGER NOT NULL DEFAULT 0, -- noticed by the model, not said by them
    at REAL NOT NULL
)""",
}
INDEXES = [
    "CREATE INDEX events_ts ON events(ts)",
    "CREATE INDEX events_cluster ON events(cluster_id)",
    "CREATE INDEX turns_conversation ON turns(conversation_id)",
    "CREATE INDEX turns_audio ON turns(audio)",  # the first request's audio is shared with its wake
    "CREATE INDEX items_conversation ON items(conversation_id)",
    "CREATE INDEX reminders_status ON reminders(status)",
    "CREATE INDEX errands_status ON errands(status)",
    "CREATE INDEX takes_session ON takes(session_id)",
    "CREATE UNIQUE INDEX feeds_person_url ON feeds(person, url)",
    "CREATE INDEX preferences_person ON preferences(person)",
    "CREATE INDEX door_events_key ON door_events(key)",
    "CREATE INDEX usage_at ON usage(at)",
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
    [
        "ALTER TABLE conversations ADD COLUMN via TEXT",
        "ALTER TABLE reminders ADD COLUMN channel TEXT NOT NULL DEFAULT 'voice'",
        "ALTER TABLE reminders ADD COLUMN reported TEXT",
        "ALTER TABLE items ADD COLUMN delivered REAL",
        "UPDATE items SET delivered = ts",  # what was sent before Telegram isn't posted to a chat now
    ],
    [
        """CREATE TABLE remote_files (
    path TEXT PRIMARY KEY,
    uploaded REAL NOT NULL,
    size INTEGER NOT NULL,
    offloaded REAL,
    fetched REAL
)""",
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ],
    [
        (
            "CREATE TABLE presence (person TEXT PRIMARY KEY, home INTEGER, since REAL, last_seen REAL, "
            "updated REAL NOT NULL)"
        ),
        "ALTER TABLE reminders ADD COLUMN kept_aloud INTEGER NOT NULL DEFAULT 0",
    ],
    [
        (
            "CREATE TABLE errands (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, person TEXT NOT NULL, "
            "goal TEXT NOT NULL, asked_via TEXT NOT NULL, status TEXT NOT NULL, started REAL, ended REAL, answer TEXT)"
        ),
        "CREATE INDEX errands_status ON errands(status)",
    ],
    [
        (
            "CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL UNIQUE, "
            "kind TEXT NOT NULL, person TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, "
            "prompts TEXT NOT NULL, skipped TEXT NOT NULL DEFAULT '[]', finished REAL)"
        ),
        (
            "CREATE TABLE takes (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, "
            "prompt TEXT NOT NULL, ts REAL NOT NULL, audio TEXT, seconds REAL NOT NULL, problem TEXT, "
            "stopped TEXT NOT NULL)"
        ),
        "CREATE INDEX takes_session ON takes(session_id)",
    ],
    [
        (
            "CREATE TABLE feeds (id INTEGER PRIMARY KEY AUTOINCREMENT, person TEXT NOT NULL, category TEXT NOT NULL, "
            "name TEXT NOT NULL, url TEXT NOT NULL, added REAL NOT NULL)"
        ),
        (
            "CREATE TABLE feed_seen (person TEXT NOT NULL, link TEXT NOT NULL, at REAL NOT NULL, "
            "PRIMARY KEY (person, link))"
        ),
        "CREATE UNIQUE INDEX feeds_person_url ON feeds(person, url)",
    ],
    [
        (
            "CREATE TABLE skills (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, name TEXT NOT NULL, "
            "instructions TEXT NOT NULL, purpose TEXT NOT NULL DEFAULT '', at TEXT NOT NULL DEFAULT '[]', "
            "days TEXT NOT NULL DEFAULT '[]', say TEXT NOT NULL DEFAULT 'aloud', paused INTEGER NOT NULL DEFAULT 0, "
            "created REAL NOT NULL, updated REAL NOT NULL, updated_by TEXT NOT NULL)"
        ),
        (
            "CREATE TABLE preferences (id INTEGER PRIMARY KEY AUTOINCREMENT, person TEXT NOT NULL, "
            "about TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, guessed INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL)"
        ),
        "CREATE INDEX preferences_person ON preferences(person)",
    ],
    [
        "ALTER TABLE skills ADD COLUMN trigger TEXT",
        (
            "CREATE TABLE skill_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, skill_id INTEGER, name TEXT NOT NULL, "
            "owner TEXT NOT NULL, at REAL NOT NULL, why TEXT NOT NULL, said TEXT NOT NULL)"
        ),
    ],
    [
        (
            "CREATE TABLE wishes (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, person TEXT, "
            "via TEXT NOT NULL, asked TEXT NOT NULL, why TEXT NOT NULL, granted REAL)"
        ),
    ],
    [
        (
            "CREATE TABLE settings_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, person TEXT, "
            "name TEXT NOT NULL, old TEXT NOT NULL, new TEXT NOT NULL)"
        ),
    ],
    [
        "CREATE TABLE web_invites (token TEXT PRIMARY KEY, person TEXT NOT NULL, expires REAL NOT NULL, used REAL)",
        (
            "CREATE TABLE passkeys (id TEXT PRIMARY KEY, person TEXT NOT NULL, public_key BLOB NOT NULL, "
            "sign_count INTEGER NOT NULL, created REAL NOT NULL, last_used REAL, device TEXT)"
        ),
        (
            "CREATE TABLE web_sessions (token TEXT PRIMARY KEY, person TEXT NOT NULL, created REAL NOT NULL, "
            "expires REAL NOT NULL)"
        ),
    ],
    [
        "ALTER TABLE presence ADD COLUMN via TEXT",
        (
            "CREATE TABLE door_events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, door TEXT NOT NULL, "
            "action INTEGER NOT NULL, trigger INTEGER NOT NULL, key TEXT NOT NULL, auto_unlock INTEGER NOT NULL, "
            "person TEXT, guess TEXT, guess_at REAL, settled INTEGER NOT NULL DEFAULT 0)"
        ),
        "CREATE TABLE door_keys (key TEXT PRIMARY KEY, person TEXT NOT NULL, learned REAL NOT NULL)",
        "CREATE INDEX door_events_key ON door_events(key)",
    ],
    [
        "ALTER TABLE skills ADD COLUMN once REAL",
        "ALTER TABLE skills ADD COLUMN tries TEXT",
        "ALTER TABLE skill_runs ADD COLUMN failed INTEGER NOT NULL DEFAULT 0",
    ],
    [
        "ALTER TABLE wishes ADD COLUMN build TEXT",
        "ALTER TABLE wishes ADD COLUMN issue INTEGER",
        "ALTER TABLE wishes ADD COLUMN pr INTEGER",
        "ALTER TABLE wishes ADD COLUMN sha TEXT",
        "ALTER TABLE wishes ADD COLUMN tries INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE wishes ADD COLUMN checked TEXT",
        "ALTER TABLE wishes ADD COLUMN built_at REAL",
        "ALTER TABLE wishes ADD COLUMN head TEXT",
        "ALTER TABLE wishes ADD COLUMN moved REAL",
    ],
    ["ALTER TABLE sessions ADD COLUMN cloud INTEGER NOT NULL DEFAULT 0"],
    [
        """CREATE TABLE usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,                -- when the call ended
    service TEXT NOT NULL,           -- openai, cerebras, groq, anthropic, deepgram, cartesia
    kind TEXT NOT NULL,              -- llm, stt or tts (usage.py)
    model TEXT NOT NULL,             -- the model, or the voice
    person TEXT NOT NULL DEFAULT '', -- who it was for (person_key); '' when not known
    channel TEXT NOT NULL DEFAULT '',-- voice, telegram, web, errand, skill or background
    room TEXT NOT NULL DEFAULT '',   -- where it was heard, once there are rooms
    input INTEGER NOT NULL DEFAULT 0,      -- tokens, for llm
    output INTEGER NOT NULL DEFAULT 0,
    reasoning INTEGER NOT NULL DEFAULT 0,  -- of output
    cached INTEGER NOT NULL DEFAULT 0,     -- of input
    searches INTEGER NOT NULL DEFAULT 0,   -- web searches made in the call
    seconds REAL NOT NULL DEFAULT 0,       -- audio heard, for stt
    characters INTEGER NOT NULL DEFAULT 0, -- text spoken, for tts
    reported INTEGER NOT NULL DEFAULT 1    -- 0: the provider said nothing of it (a call cut off): counted, not measured
)""",
        "CREATE INDEX usage_at ON usage(at)",
    ],
]


class NewerDatabaseError(ConfigError):
    """The database was made by a newer TARS: this code can't read it, and mustn't change it."""


class Store:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            _migrate(db)

    def connect(self) -> sqlite3.Connection:
        # Autocommit: a single statement is atomic on its own, and transaction() opens the multi-step ones.
        db = sqlite3.connect(self.folder / DB_FILE, timeout=10, isolation_level=None)
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

    def row(self, sql: str, args: tuple | list = ()) -> dict | None:
        rows = self.rows(sql, args)
        return rows[0] if rows else None

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
    if version > len(MIGRATIONS):
        raise NewerDatabaseError(
            f"The event log's database is at version {version}, made by a newer TARS than this one (which knows "
            f"{len(MIGRATIONS)}): update the code to run it."
        )
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone():
        for statement in [*TABLES.values(), *INDEXES]:
            db.execute(statement)
    else:
        for steps in MIGRATIONS[version:]:
            for statement in steps:
                db.execute(statement)
    db.execute(f"PRAGMA user_version={len(MIGRATIONS)}")
