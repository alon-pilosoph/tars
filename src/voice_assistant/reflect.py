"""TARS looking back on each conversation, once it's over, for what's worth knowing about the people in it next time:
what memory.py keeps. This is where TARS picks things up by itself, so the reply in the moment never waits on it.

A conversation is looked back on once it has been quiet for [memory] look_back_minutes; one that goes on later (a chat
over days) is looked back on again for what's new. One request to [llm] model (structured output: a list of changes)
reads the conversation and what's kept about the people in it, and says what to remember, correct, retire (no longer
so) or mark asked (a thread TARS has now asked about). Code checks each change before it's made:

- Only what the people said to TARS counts: overheard turns (not for TARS) are never read, and turns by a voice TARS
  couldn't tell are read only as context, never kept from.
- A change is for someone who spoke in it, or the household. "Said" only when that person's own turn says it;
  anything else is kept as noticed (a guess). Nothing it keeps is private: only the person asking can make one so.
- At most MOST_CHANGES a conversation; most need none.

The household's people only: nothing outside the house reaches it, except the request itself (OpenAI, store off).
"""

import json
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime

from . import usage
from .events import person_key
from .memory import KINDS, LOOK_BACK, MOST_CHARS, THREAD, Memory, _line
from .store import Store

TICK_S = 60.0  # how often it looks for conversations to look back on
MOST_A_TICK = 5  # conversations looked back on in one go, oldest first
MOST_CHANGES = 8
MOST_TRIES = 3  # a conversation the look back keeps failing on is left
MOST_TURNS = 80  # of one conversation, the newest: a long chat is looked at in its latest part
REMEMBER, CORRECT, RETIRE, ASKED = "remember", "correct", "retire", "asked"
HOUSEHOLD_NAME = "household"

INSTRUCTIONS = """You look back on a conversation between TARS, a household's assistant, and the people in the \
household, to keep what's worth knowing about them next time: so TARS answers like someone who knows them. You see \
the conversation, and what TARS already knows about the people in it and the household, each with its number.

Keep what lasts or is coming up:
- fact: about them or their life (work, family, the home, their things, plans): "Works from home on Fridays".
- person: someone around them, and who they are to them: "Yossi: the electrician".
- like: what they like or don't, in anything: "Doesn't want esports in the gaming news".
- style: how they talk and want to be answered: length, tone, humour, directness; their own words for things \
("Ari's room" is the nursery; "warm" means 24 degrees); when they want TARS to just do it and when to ask first. \
Keep style only from a clear signal: they said so, corrected TARS, cut it short, asked for more or less, undid what it \
did, or used their word for something.
- thread: something coming up that they mentioned, with when it happens ("due"): an interview, a trip, a delivery, \
a visit. TARS will ask how it went, once, after.

Never keep:
- what they asked for this once (turn on the light, the weather, a timer), small talk, a mood of the moment;
- codes, passwords, card or account numbers;
- anything about health, money, relationships, religion or politics that the person didn't plainly say about \
themselves (then it's said: true);
- anything from a turn by "someone TARS couldn't tell": that's only context.

Then:
- Check against what's kept. Don't repeat it. When something changed or was wrong, correct it (its id, the whole \
new note). When something is over or no longer so (a thread that has happened and been asked about, a fact that \
stopped being true), retire it (its id).
- A thread TARS asked about in this conversation: "asked" (its id). If they said how it went, keep what matters of \
that as a fact, and retire the thread.
- said: true only when the person said it themselves; false when you worked it out.
- turn: the number of the turn it comes from.
- Write each as a short note in the third person, in English, with dates written out from the conversation's \
dates ("born in March 2026", never "eight months old"; "on 2026-11-27", never "next Thursday").
- whose: the person's name as it appears in the conversation, or "household" for what's about the home or \
everyone in it.
- Few and good: most conversations need no change at all; keep at most {most}."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["changes"],
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["do", "whose", "kind", "about", "text", "said", "due", "until", "id", "turn"],
                "properties": {
                    "do": {"type": "string", "enum": [REMEMBER, CORRECT, RETIRE, ASKED]},
                    "whose": {"type": "string"},
                    "kind": {"type": ["string", "null"], "enum": [*KINDS, None]},
                    "about": {"type": ["string", "null"]},
                    "text": {"type": ["string", "null"]},
                    "said": {"type": "boolean"},
                    "due": {"type": ["string", "null"], "description": "a thread: YYYY-MM-DD or YYYY-MM-DD HH:MM"},
                    "until": {"type": ["string", "null"], "description": "YYYY-MM-DD it stops mattering, or null"},
                    "id": {"type": ["integer", "null"]},
                    "turn": {"type": ["integer", "null"]},
                },
            },
        }
    },
}


class LookBack:
    """Looks back on the conversations that have gone quiet. `ask(instructions, text)`: the model's answer, as JSON
    text matching SCHEMA. `quiet_s`: how long a conversation is quiet before it's looked back on. `clock`: seconds."""

    def __init__(
        self,
        store: Store,
        memory: Memory,
        ask: Callable[[str, str], str],
        quiet_s: float,
        clock: Callable[[], float] = time.time,
    ):
        self.store, self.memory, self._ask, self._quiet_s, self._clock = store, memory, ask, quiet_s, clock

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._forever, daemon=True, name="look-back")
        thread.start()
        return thread

    def _forever(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a look back gone wrong never stops the next
                traceback.print_exc()
            time.sleep(TICK_S)

    def due(self) -> list[int]:
        """Conversations with something new from someone TARS knows, quiet for long enough, oldest first."""
        rows = self.store.rows(
            "SELECT t.conversation_id AS id, MAX(t.id) AS last FROM turns t "
            "LEFT JOIN looked_back l ON l.conversation_id = t.conversation_id "
            "GROUP BY t.conversation_id "
            "HAVING MAX(t.ts) < ? AND MAX(t.id) > COALESCE(MAX(l.through_turn), 0) "
            "AND COALESCE(MAX(l.tries), 0) < ? "
            "AND SUM(t.role = 'person' AND t.speaker IS NOT NULL AND COALESCE(t.not_for_tars, 0) = 0 "
            "AND t.id > COALESCE(l.through_turn, 0)) > 0 "
            "ORDER BY MAX(t.ts) LIMIT ?",
            (self._clock() - self._quiet_s, MOST_TRIES, MOST_A_TICK),
        )
        return [r["id"] for r in rows]

    def tick(self) -> None:
        for conversation in self.due():
            self.look_back(conversation)

    def look_back(self, conversation_id: int) -> list[str]:
        """One conversation: asked, and the changes made. What each change came to, for the log and the tests."""
        conversation = self.store.row("SELECT * FROM conversations WHERE id=?", (conversation_id,))
        turns = self.store.rows(
            "SELECT id, ts, role, COALESCE(corrected_text, text) AS text, speaker FROM turns "
            "WHERE conversation_id=? AND COALESCE(not_for_tars, 0)=0 ORDER BY id DESC LIMIT ?",
            (conversation_id, MOST_TURNS),
        )[::-1]
        people = sorted({t["speaker"] for t in turns if t["role"] == "person" and t["speaker"]})
        last = max(t["id"] for t in turns) if turns else 0
        if conversation is None or not people:
            self._done(conversation_id, last)
            return []
        text = self._prompt(conversation, turns, people)
        try:
            with usage.turn(people[0], usage.BACKGROUND):
                answer = self._ask(INSTRUCTIONS.format(most=MOST_CHANGES), text)
            changes = json.loads(answer)["changes"]
        except Exception as e:  # noqa: BLE001 - tried again on the next tick, up to MOST_TRIES
            print(f"(couldn't look back on conversation {conversation_id}: {e!r})")
            self.store.write(
                "INSERT INTO looked_back (conversation_id, through_turn, at, tries) VALUES (?, 0, ?, 1) "
                "ON CONFLICT(conversation_id) DO UPDATE SET tries = tries + 1, at = excluded.at",
                (conversation_id, self._clock()),
            )
            return []
        whose = {person_key(p): p for p in people}
        theirs = {t["id"]: person_key(t["speaker"]) for t in turns if t["role"] == "person" and t["speaker"]}
        done = [self._apply(c, whose, theirs) for c in changes[:MOST_CHANGES]]
        self._done(conversation_id, last)
        for line in done:
            print(f"(looked back on conversation {conversation_id}: {line})")
        return done

    def _done(self, conversation_id: int, last: int) -> None:
        self.store.write(
            "INSERT INTO looked_back (conversation_id, through_turn, at, tries) VALUES (?, ?, ?, 0) "
            "ON CONFLICT(conversation_id) DO UPDATE SET through_turn = excluded.through_turn, at = excluded.at, "
            "tries = 0",
            (conversation_id, last, self._clock()),
        )

    def _prompt(self, conversation: dict, turns: list[dict], people: list[str]) -> str:
        now = self._clock()
        where = "in writing, on Telegram" if conversation["via"] == "telegram" else "aloud, at home"
        if conversation["room"]:
            where += f" (the {conversation['room']})"
        lines = [f"The conversation, {where}, {_when(conversation['started'], '%A %Y-%m-%d')}:"]
        for t in turns:
            who = "TARS" if t["role"] == "tars" else t["speaker"] or "someone TARS couldn't tell"
            lines.append(f"[turn {t['id']}, {_when(t['ts'], '%H:%M')}] {who}: {t['text'] or ''}")
        known = []
        for person in people:
            kept = [r for r in self.memory.current(person, private=True) if r["person"]]
            known.append(f"What TARS knows about {person}:\n" + ("\n".join(_line(r, now) for r in kept) or "nothing"))
        household = [r for r in self.memory.current(None) if not r["person"]]
        known.append("About the household:\n" + ("\n".join(_line(r, now) for r in household) or "nothing"))
        return f"Today is {_when(now, '%A %Y-%m-%d')}.\n\n" + "\n\n".join(known) + "\n\n" + "\n".join(lines)

    def _apply(self, change: dict, whose: dict[str, str], theirs: dict[int, str]) -> str:
        """One change, if it passes the checks: what it came to."""
        if not isinstance(change, dict):
            return "skipped: not a change"
        do, name = change.get("do"), str(change.get("whose") or "")
        household = name.strip().lower() == HOUSEHOLD_NAME
        person = None if household else whose.get(person_key(name) or "")
        if not household and person is None:
            return f"skipped: {name!r} didn't speak in it"
        turn = change.get("turn") if isinstance(change.get("turn"), int) else None
        if turn not in theirs:
            turn = None  # only a turn of someone in it is where a memory came from
        owner = person or (theirs[turn] if turn is not None else next(iter(whose)))  # someone who spoke
        # Said only when the turn it came from is that person's own; for the household's, anyone's in it.
        said = bool(change.get("said")) and turn is not None and (household or theirs[turn] == person_key(person))
        args = {
            "kind": change.get("kind"),
            "about": change.get("about"),
            "text": str(change.get("text") or "")[:MOST_CHARS],
            "household": household,
            "noticed": not said,
            "private": False,
            "due": change.get("due"),
            "until": change.get("until"),
            "id": change.get("id"),
        }
        try:
            if do == REMEMBER:
                return self.memory.remember(owner, args, LOOK_BACK, turn)
            if do == CORRECT:
                return self.memory.correct(owner, args, LOOK_BACK, turn)
            if do == RETIRE:
                return self.memory.retire(owner, args["id"])
            if do == ASKED:
                row = self.store.row("SELECT person, kind FROM memories WHERE id=?", (args["id"],))
                if not row or row["kind"] != THREAD or row["person"] not in (person_key(owner), ""):
                    return f"skipped: #{args['id']} is no thread of theirs"
                self.memory.asked(args["id"])
                return f"done: #{args['id']} asked"
            return f"skipped: {do!r} isn't a change"
        except ValueError as e:
            return f"skipped: {e}"


def _when(at: float, form: str) -> str:
    return datetime.fromtimestamp(at).astimezone().strftime(form)


def openai_asker(client, model: str, effort: str) -> Callable[[str, str], str]:
    """The look back's question, to OpenAI's `model` at `effort`, with SCHEMA's structured output; counted."""

    def ask(instructions: str, text: str) -> str:
        reply = client.responses.create(
            model=model,
            instructions=instructions,
            input=text,
            store=False,
            text={"format": {"type": "json_schema", "name": "look_back", "schema": SCHEMA, "strict": True}},
            **({"reasoning": {"effort": effort}} if effort else {}),
        )
        usage.response("openai", model, reply)
        return reply.output_text

    return ask
