"""What TARS knows about each person and the household, kept over time, so it answers as someone who knows them.

Five kinds of knowing, each a memory: a short note in plain words, whose it is (a person, or the household), what it's
about, and how it's known:

- FACT: about them or their life ("Ari was born in March 2026", "the car is a 2019 Corolla").
- LIKE: what they like or don't ("skip esports in the gaming news"): what skills.py called preferences.
- STYLE: how they talk, and want to be answered ("short answers in the morning"; "'warm' means 24°"; "just do it
  for the AC, ask first for the TV"), aloud or in writing.
- THREAD: something coming up that they mentioned (an interview on Thursday): `due` is when it happens, and TARS asks
  how it went once, after; `until` is when it stops mattering, and it's gone.
- PERSON: someone around them ("Yossi: the electrician"), so "call Yossi" or "before mum's visit" just work.

Each says whether they said it or TARS noticed it (`said`), where it came from (a voice or chat turn, or the nightly
look back), and when; dates are written out, never relative ("born in March 2026", not "eight months old").
Correcting one keeps the old one as history, replaced (`replaced`, `replaced_by`): what was true when stays known.
Forgetting deletes it, and its history, for good.

Every request is told what's kept for whoever is asking, and the household's (note()); a person's own only when TARS
knows who's asking (their chat, their signed-in page, or speaker ID sure of their voice), and a private one only in
their own chat. The household hears each other's otherwise: that's the owner's choice (2026-11-19).

Writing, in a turn, is the memory tool's: what they ask TARS to remember, correct or forget, and nothing else, so a
reply never waits on a save it wasn't asked for. What TARS picks up by itself (facts, likes, how they talk, threads)
is written after the conversation, by the look back (reflect.py), never from what was overheard. A memory is never
written in a reply that read the web or has a photo in front of it (a page could plant one), nor in a turn a door's
lock set off.
"""

import re
import time
from collections.abc import Callable
from datetime import datetime

from . import toolbox
from .events import person_key
from .store import Store

DEVICE = "memory"  # the tool's name
FACT, LIKE, STYLE, THREAD, PERSON = "fact", "like", "style", "thread", "person"
KINDS = (STYLE, LIKE, FACT, PERSON, THREAD)  # as the card lists them: how to answer first
HOUSEHOLD = ""  # whose a household memory is
VOICE, CHAT, WEB, LOOK_BACK, MOVED = "voice", "chat", "web", "look back", "moved"  # where one came from
REMEMBER, CORRECT, FORGET, RECALL, LIST = "remember", "correct", "forget", "recall", "list"
ACTIONS = (REMEMBER, CORRECT, FORGET, RECALL, LIST)
MOST_TOLD = 60  # a person's memories on every request; past this, the rest are recalled by the tool
MOST_HOUSEHOLD = 40
MOST_RECALLED = 12
MOST_CHARS = 300  # one memory: a note, not a story
THREAD_KEPT_S = 7 * 24 * 3600  # a thread with no until of its own matters for this long after it happens

MEMORY_TOOL = {
    "type": "function",
    "name": DEVICE,
    "description": "What TARS knows about the person asking and the household: remember something they ask you to, "
    "correct what changed or was wrong, forget something, or look up what's kept beyond what you were told.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "kind", "about", "text", "household", "noticed", "private", "due", "until", "id"],
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": "remember: a new memory. correct: one that changed or was wrong (by id), in full, as it "
                "is now; the old one is kept as history. forget: one they want gone (by id), for good. recall: look "
                "up memories and their history by words in text. list: everything kept for them and the household.",
            },
            "kind": {
                "type": ["string", "null"],
                "enum": [*KINDS, None],
                "description": "remember, correct: fact (about them or their life), like (what they like or don't), "
                "style (how they want to be answered, their words for things, when to just do it), person (someone "
                "around them, and who), thread (something coming up: due is when).",
            },
            "about": {
                "type": ["string", "null"],
                "description": "remember, correct: its topic, in two or three words.",
            },
            "text": {
                "type": ["string", "null"],
                "description": "remember, correct: the memory, as a short note, with dates written out "
                "('born in March 2026', never 'eight months old'). recall: the words to look for.",
            },
            "household": {
                "type": ["boolean", "null"],
                "description": "remember: the household's (about the home, or someone everyone knows), not only "
                "theirs.",
            },
            "noticed": {
                "type": ["boolean", "null"],
                "description": "remember, correct: true if you worked it out rather than heard them say it.",
            },
            "private": {
                "type": ["boolean", "null"],
                "description": "remember: only when they ask that nobody else hear it (a surprise): then only ever "
                "in their own chat.",
            },
            "due": {
                "type": ["string", "null"],
                "description": "remember, correct a thread: when it happens, 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'.",
            },
            "until": {
                "type": ["string", "null"],
                "description": "remember, correct: when it stops mattering, 'YYYY-MM-DD'; null: it lasts.",
            },
            "id": {"type": ["integer", "null"], "description": "correct, forget: the memory's number (#12 is 12)."},
        },
    },
}
CAN_MEMORY = (
    "remember, correct and forget what people tell you about themselves and the household, with the memory tool"
)
NEEDS_MEMORY = "remembering, correcting or forgetting something about them or the household, or looking up what's kept"
# With what's kept, on every request, for whichever model answers: how to use it.
USE = (
    "Use what you know about them, unprompted, whenever it makes the answer better: their words for things, how they "
    "like answers, who people are, what's coming up. Answer as someone who knows them would, without saying you know "
    "it, how, or that you're adapting: never 'as you told me', 'I remember', 'I noticed', 'I'll keep that in mind'. "
    "Asked how you know, say plainly. How they like to be answered changes how you say things, never what's true: "
    "don't agree with them, or shade an answer, because of what you know of their views. A thread marked 'ask once' "
    "is something to ask about briefly, once, when the conversation allows it, not while they're in the middle of "
    "something. Guesses are guesses: never tell one back as something they said."
)
RULES = (
    "The memory tool is only for what they ask: 'remember that...', 'don't forget...', 'forget...', or a correction "
    "('we sold the Corolla', 'no, Yossi's the plumber'): correct the one that changed rather than adding another. "
    "Answer in a few words ('Got it.'). Everything else you pick up is remembered later by itself: don't call the "
    "tool for it, and don't say you'll remember it. \"What do you know about me?\": tell them plainly, guesses as "
    "guesses; they can correct or remove any of it. When you don't know who's asking, you're told only the "
    "household's; don't guess who it is."
)
FORGOTTEN_TOO = "error: there's no memory #{id} of theirs or the household's; list or recall shows them"


class Memory:
    """The memories in the event database, the brain's memory tool over them (tool, rules, can, needs, as the other
    providers give them, llm.py), and note() for each request. `clock`: seconds."""

    tool, rules, can, needs = MEMORY_TOOL, RULES, CAN_MEMORY, NEEDS_MEMORY

    def __init__(self, store: Store, clock: Callable[[], float] = time.time):
        self.store, self._clock = store, clock

    # Reading.

    def current(self, person: str | None, private: bool = False) -> list[dict]:
        """`person`'s memories (None: nobody's) and the household's, as they are now: not replaced, not past their
        `until`; their private ones only with `private` (it's their own chat)."""
        key = person_key(person) or HOUSEHOLD
        rows = self.store.rows(
            "SELECT * FROM memories WHERE replaced IS NULL AND person IN (?, ?) AND (until IS NULL OR until > ?) "
            "ORDER BY person = ?, changed DESC",
            (key, HOUSEHOLD, self._clock(), HOUSEHOLD),
        )
        return [r for r in rows if private or not r["private"]]

    def note(self, person: str | None, private: bool = False) -> str:
        """For the model, on each request: what's kept about whoever is asking, and the household's. `person`: who
        it surely is; None when TARS isn't sure."""
        rows = self.current(person, private)
        now = self._clock()
        mine = [r for r in rows if r["person"] != HOUSEHOLD][:MOST_TOLD]
        ours = [r for r in rows if r["person"] == HOUSEHOLD][:MOST_HOUSEHOLD]
        parts = []
        if mine:
            parts.append(f"What you know about {person}:\n" + _by_kind(mine, now))
        if ours:
            parts.append("What you know about the household:\n" + _by_kind(ours, now))
        if not parts:
            return ""
        more = len([r for r in rows if r["person"] != HOUSEHOLD]) - len(mine)
        tail = f"\n({more} more of theirs: the memory tool's recall finds them.)" if more > 0 else ""
        return f"{USE}\n\n" + "\n\n".join(parts) + tail

    def recall(self, person: str | None, words: str, private: bool = False) -> str:
        """Memories of theirs and the household's, history included, with the most of `words` in them first."""
        key = person_key(person) or HOUSEHOLD
        wanted = {w for w in _words(words) if len(w) > 2 or w.isdigit()}
        if not wanted:
            return "error: recall needs words to look for, in text"
        scored = []
        for r in self.store.rows(
            "SELECT * FROM memories WHERE person IN (?, ?) ORDER BY changed DESC", (key, HOUSEHOLD)
        ):
            hits = len(wanted & _words(f"{r['about']} {r['text']}"))
            if hits and (private or not r["private"]):
                scored.append((hits, r))
        if not scored:
            return "Nothing kept with those words."
        scored.sort(key=lambda hit: -hit[0])  # stable: the newest first among equals
        return "\n".join(_line(r, self._clock(), history=True) for _, r in scored[:MOST_RECALLED])

    # The tool.

    def toolset(self) -> toolbox.Toolset:
        """The memory tool for the brain (toolbox.py). Only someone TARS knows can change what it knows of them, and
        every change is held back as any is (toolbox.held_back): refused in a reply that read the web or has a photo in
        front of it, and in a turn a door's lock set off, and made only once the turn goes ahead. Reading isn't."""
        failed = "error: couldn't reach what TARS knows ({e!r}); nothing changed"
        return toolbox.one_tool(self, toolbox.ITS_OWN, failed)

    def call(self, args: dict, request=None) -> str:
        args = args if isinstance(args, dict) else {}
        action, person = args.get("action"), getattr(request, "speaker", None)
        private = bool(getattr(request, "by_chat", False))  # their own chat: their private ones too
        if action == LIST:
            return self.note(person, private) or "Nothing kept yet, about them or the household."
        if action == RECALL:
            return self.recall(person, _clean(args.get("text")), private)
        if action not in ACTIONS:
            return f"error: action is one of {', '.join(ACTIONS)}"
        if not person_key(person):
            return "refused: TARS doesn't know who's asking, and only someone it knows can change what it knows: say so"
        if refused := toolbox.held_back(request):
            return refused
        source = CHAT if getattr(request, "by_chat", False) else WEB if getattr(request, "by_web", False) else VOICE
        try:
            if action == REMEMBER:
                return self.remember(person, args, source)
            if action == CORRECT:
                return self.correct(person, args, source)
            return self.forget(person, args.get("id"))
        except ValueError as e:
            return f"error: {e}"

    # Changing: the tool's, the look back's, and the web UI's.

    def remember(self, person: str, args: dict, source: str, turn_id: int | None = None) -> str:
        new = self._fields(args)
        whose = HOUSEHOLD if args.get("household") else person_key(person)
        now = self._clock()
        with self.store.transaction() as db:
            same = db.execute(
                "SELECT id, said FROM memories WHERE person=? AND replaced IS NULL AND lower(text)=lower(?)",
                (whose, new["text"]),
            ).fetchone()
            if same:  # said again: no longer only noticed, if it was
                db.execute(
                    "UPDATE memories SET said=MAX(said, ?), changed=? WHERE id=?", (new["said"], now, same["id"])
                )
                return f"done: already known (#{same['id']})"
            memory_id = db.execute(
                "INSERT INTO memories (person, kind, about, text, said, private, due, until, source, turn_id, created, "
                "changed) VALUES (:person, :kind, :about, :text, :said, :private, :due, :until, :source, :turn_id, "
                ":now, :now)",
                {**new, "person": whose, "source": source, "turn_id": turn_id, "now": now},
            ).lastrowid
        return f"done: remembered (#{memory_id})"

    def correct(self, person: str, args: dict, source: str, turn_id: int | None = None) -> str:
        new = self._fields(args)
        now = self._clock()
        with self.store.transaction() as db:
            old = self._mine(db, person, args.get("id"))
            if old is None:
                return FORGOTTEN_TOO.format(id=args.get("id"))
            memory_id = db.execute(
                "INSERT INTO memories (person, kind, about, text, said, private, due, until, source, turn_id, created, "
                "changed) VALUES (:person, :kind, :about, :text, :said, :private, :due, :until, :source, :turn_id, "
                ":now, :now)",
                {
                    **new,
                    "person": old["person"],
                    "kind": args.get("kind") or old["kind"],
                    "about": new["about"] or old["about"],
                    "private": old["private"],
                    "source": source,
                    "turn_id": turn_id,
                    "now": now,
                },
            ).lastrowid
            db.execute("UPDATE memories SET replaced=?, replaced_by=? WHERE id=?", (now, memory_id, old["id"]))
        return f"done: #{old['id']} is now #{memory_id}; the old one is kept as history"

    def forget(self, person: str | None, memory_id) -> str:
        with self.store.transaction() as db:
            old = self._mine(db, person, memory_id)
            if old is None:
                return FORGOTTEN_TOO.format(id=memory_id)
            gone = self._with_history(db, old["id"])
            db.executemany("DELETE FROM memories WHERE id=?", [(i,) for i in gone])
        return f"done: forgot #{old['id']}, and what it replaced"

    def delete(self, memory_id: int) -> bool:
        """The web UI's: one memory and its history, by id, whoever's it is."""
        with self.store.transaction() as db:
            gone = self._with_history(db, memory_id)
            db.executemany("DELETE FROM memories WHERE id=?", [(i,) for i in gone])
        return bool(gone)

    def retire(self, person: str | None, memory_id) -> str:
        """One that's no longer so (a thread that's over, a fact that stopped being true), with nothing in its
        place: kept as history."""
        with self.store.transaction() as db:
            old = self._mine(db, person, memory_id)
            if old is None:
                return FORGOTTEN_TOO.format(id=memory_id)
            db.execute("UPDATE memories SET replaced=? WHERE id=?", (self._clock(), old["id"]))
        return f"done: #{old['id']} is history"

    def asked(self, memory_id: int) -> None:
        """A thread TARS has asked about: not again."""
        self.store.write("UPDATE memories SET asked=? WHERE id=? AND asked IS NULL", (self._clock(), memory_id))

    def _mine(self, db, person: str | None, memory_id):
        """Memory `memory_id`, current, if it's `person`'s or the household's."""
        if not isinstance(memory_id, int) or isinstance(memory_id, bool):
            raise ValueError("this needs the memory's number, as id")  # noqa: TRY004 - told to the model, as any bad argument
        row = db.execute("SELECT * FROM memories WHERE id=? AND replaced IS NULL", (memory_id,)).fetchone()
        if row is None or row["person"] not in (person_key(person), HOUSEHOLD):
            return None
        return row

    @staticmethod
    def _with_history(db, memory_id: int) -> list[int]:
        """`memory_id`, and every memory it replaced, however far back."""
        ids, todo = [], [memory_id]
        while todo:
            i = todo.pop()
            if db.execute("SELECT 1 FROM memories WHERE id=?", (i,)).fetchone():
                ids.append(i)
                todo += [r["id"] for r in db.execute("SELECT id FROM memories WHERE replaced_by=?", (i,)).fetchall()]
        return ids

    def _fields(self, args: dict) -> dict:
        text = _clean(args.get("text"))
        kind = args.get("kind")
        if not text:
            raise ValueError("this needs the memory, in text")
        if len(text) > MOST_CHARS:
            raise ValueError(f"keep it to a short note (under {MOST_CHARS} characters)")
        if kind not in KINDS:
            raise ValueError(f"kind is one of {', '.join(KINDS)}")
        due, until = _when(args.get("due")), _when(args.get("until"))
        if kind == THREAD and due is None:
            raise ValueError("a thread needs when it happens, in due")
        if kind == THREAD and until is None:
            until = due + THREAD_KEPT_S  # asked about or not, it stops mattering
        return {
            "kind": kind,
            "about": _clean(args.get("about")).lower(),
            "text": text,
            "said": not args.get("noticed"),
            "private": bool(args.get("private")),
            "due": due,
            "until": until,
        }


def _when(value) -> float | None:
    """'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM', local time, as seconds; None for nothing. Anything else is an error."""
    if not value:
        return None
    for form in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).strip(), form).astimezone().timestamp()
        except ValueError:
            continue
    raise ValueError(f"{value!r} isn't a date: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'")


def _day(at: float) -> str:
    return datetime.fromtimestamp(at).astimezone().strftime("%Y-%m-%d")


def _line(r: dict, now: float, history: bool = False) -> str:
    """One memory as the model reads it: its number, what it is, and how it's known."""
    about = f"({r['about']}) " if r["about"] else ""
    how = "said" if r["said"] else "a guess"
    when = f"{how} {_day(r['created'])}"
    if r["kind"] == THREAD and r["due"] is not None:
        happens = datetime.fromtimestamp(r["due"]).astimezone().strftime("%Y-%m-%d %H:%M").removesuffix(" 00:00")
        when += f", happens {happens}"
        if r["due"] <= now and r["asked"] is None:
            when += ", ask once"
        elif r["asked"] is not None:
            when += ", asked already"
    if r["private"]:
        when += ", private"
    if history and r["replaced"] is not None:
        when += f", replaced {_day(r['replaced'])} by #{r['replaced_by']}"
    return f"- #{r['id']} {about}{r['text']} [{when}]"


HEADINGS = {
    STYLE: "How they like to be answered",
    LIKE: "Likes and dislikes",
    FACT: "Facts",
    PERSON: "People",
    THREAD: "Coming up",
}


def _by_kind(rows: list[dict], now: float) -> str:
    out = []
    for kind in KINDS:
        these = [r for r in rows if r["kind"] == kind]
        if these:
            out.append(f"{HEADINGS[kind]}:\n" + "\n".join(_line(r, now) for r in these))
    return "\n".join(out)


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _clean(value) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""
