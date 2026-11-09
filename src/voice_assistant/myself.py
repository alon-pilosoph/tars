"""The brain's `tars` tool: TARS knowing itself. What it is and how it's running, what changed in it lately, how it
works (its own docs), what it said and did (its records), and what its log says.

So "what's new?", "how do I teach you a skill?", "why did you set that timer?" and "you didn't answer me earlier" are
answered from what's true, not guessed, and "I can't" isn't said about something TARS could simply look up.

Kept in code:
- A Telegram chat's conversations are only its own person's to read; what was said aloud at home is the household's.
- The log's lines with chat messages in them are left out: the records have those, under the rule above.
- In a reply that read web search results, the records and the log aren't read: a page could ask for them.
"""

import re
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from . import history as when
from .config import QUICK_PROVIDERS, Config
from .health import PROCESSES, expected, processes

ABOUT, CHANGES, DOCS, RECORDS, LOG = "about", "changes", "docs", "records", "log"
WISH, WISHES, GRANTED = "wish", "wishes", "granted"
SETTINGS, SET = "settings", "set"
ACTIONS = (ABOUT, CHANGES, DOCS, RECORDS, LOG, WISH, WISHES, GRANTED, SETTINGS, SET)
MAX_WISHES = 40
PRIVATE = (RECORDS, LOG)  # what was said and done: not read in a reply that read the web
MAX_CHANGES, MAX_SECTIONS, SECTION_CHARS, MAX_TURNS, MAX_LOG, MAX_CHARS = 40, 3, 1500, 60, 80, 6000
CHANGES_SPAN_DAYS = 14  # "what's new?" with no date: the last two weeks
CHAT_LINE = re.compile(r"\((Telegram|telegram)\):")  # a chat message in the log: "Stacey (Telegram): ..."
JOURNAL_PREFIX = re.compile(r"^\w{3} \d\d \d\d:\d\d:\d\d \S+ \S+: ")  # "Nov 08 19:40:41 homepi uv[60064]: "

RULES = (
    "You know yourself, with the tars tool: never guess about TARS, and never say you can't tell something about "
    "yourself before you've looked.\n"
    "- about: your version, what's set up (devices, people, models, what's on) and whether each part is running.\n"
    '- changes: what changed in you lately, from your own history ("what\'s new?", "what can you do now?"): tell '
    "it in plain words, as what someone can now ask for, never as code.\n"
    '- docs: how you work, from your own documentation ("how do I teach you a skill?", "can you do X?"): '
    "answer from it.\n"
    '- records: what was said and what you did, with errors and which model answered ("why did you do that?", '
    '"what did I ask you this morning?"); log: your service logs, every tool call and its result, for what went '
    "wrong. Someone's chats are only theirs to read.\n"
    "- wish: when you really can't do what someone asked, note it (what they wanted, in query; what stopped you and "
    'what would make it possible, in why), so whoever looks after TARS sees it; wishes lists them ("what did people '
    "ask for that you couldn't do?\"), granted marks one done, by its number in query.\n"
    '- settings: what about you can be changed by asking ("be less jokey", "you cut me off", "remember our chats '
    "longer\"), with each one's value now; set changes one (its name in query, the new value in value). Say what "
    "changed and that you'll be back in a moment; to put one back, set it to what it was."
)
CAN = "know yourself: your version, what changed lately, how you work, and what you said and did, with the tars tool"
NEEDS = "anything about TARS itself: what's new in it, how it works, what it did or why"

TOOL = {
    "type": "function",
    "name": "tars",
    "description": "TARS itself: what it is and how it's running, what changed in it, its own docs, its records of "
    "what was said and done, and its log.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "query", "since", "until", "why", "value"],
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "query": {
                "type": ["string", "null"],
                "description": "docs: what to look up, in a few words. records, log: words to look for; null: all. "
                "wish: what they wanted, in a sentence. granted: the wish's number. set: the setting's name, as "
                "settings lists it.",
            },
            "since": {
                "type": ["string", "null"],
                "description": "changes, records, log: from when, local time, as 2026-11-08T07:00 or 2026-11-01; "
                "null: changes, the last two weeks; records and log, the last day.",
            },
            "until": {"type": ["string", "null"], "description": "records, log: until when, the same way; null: now."},
            "value": {"type": ["string", "null"], "description": "set: the new value; null otherwise."},
            "why": {
                "type": ["string", "null"],
                "description": "wish: what stopped you, and what would make it possible; null otherwise.",
            },
        },
    },
}


class Myself:
    """`events`: the event database (records), None without one. `run`: how commands run (git, journalctl)."""

    tool, rules, can, needs = TOOL, RULES, CAN, NEEDS

    def __init__(
        self,
        cfg: Config,
        root: Path,
        events=None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        unit_state: Callable[[str], str | None] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ):
        from .check import systemd_state

        self._cfg, self._root, self._events, self._run, self._now = cfg, root, events, run, now
        self._unit_state = unit_state or systemd_state

    def call(self, args: dict, request) -> str:
        args = args if isinstance(args, dict) else {}
        action, query = args.get("action"), str(args.get("query") or "").strip()
        if action in PRIVATE and getattr(request, "read_the_web", False):
            return "refused: this reply searched the web, so it doesn't read TARS's records; they can ask on its own"
        try:
            if action == ABOUT:
                return self.about()
            if action == CHANGES:
                return self.changes(args.get("since"))
            if action == DOCS:
                return self.docs(query)
            if action == RECORDS:
                return self.records(query, args.get("since"), args.get("until"), request)
            if action == LOG:
                return self.log(query, args.get("since"), args.get("until"))
            if action == WISH:
                return self.wish(query, str(args.get("why") or "").strip(), request)
            if action == WISHES:
                return self.wishes()
            if action == GRANTED:
                return self.granted(query)
            if action == SETTINGS:
                return self._settings().listing()
            if action == SET:
                return self.set(query, str(args.get("value") or ""), request)
        except when.WhenError as e:
            return f"error: {e}"
        return f"error: action is one of {', '.join(ACTIONS)}"

    # What it is.

    def about(self) -> str:
        cfg = self._cfg
        version = self._git("log", "-1", "--format=%h, from %cd: %s", "--date=format:%Y-%m-%d %H:%M")
        lines = [f"version: {version or 'unknown (not a git checkout)'}"]
        quick = [f"{getattr(cfg.llm, p.setting)} on {p.name}" for p in QUICK_PROVIDERS if getattr(cfg.llm, p.setting)]
        models = f"{cfg.llm.model} (OpenAI)" + (f"; quick replies by {', '.join(quick)}" if quick else "")
        lines.append(f"models: {models}")
        devices = [d["name"] for d in cfg.home.devices]
        if devices or cfg.home.url:
            house = ", ".join(devices) or "none listed"
            lines.append(f"the house: {house}" + ("; and all of Home Assistant" if cfg.home.url else ""))
        if cfg.telegram.enabled:
            lines.append(f"Telegram: for {', '.join(cfg.telegram.people) or 'nobody yet'}")
        on = {
            "reminders": cfg.reminders.enabled,
            "who's home": cfg.presence.enabled,
            "the backup": cfg.backup.enabled,
            "the browser": cfg.browser.enabled,
            "speaker ID": cfg.speaker.enabled,
            "web search": cfg.llm.web_search,
        }
        lines.append("on: " + ", ".join(k for k, v in on.items() if v))
        if off := [k for k, v in on.items() if not v]:
            lines.append("off: " + ", ".join(off))
        if self._events is not None:
            now = time.time()
            for p in processes(self._events.folder, expected(cfg), self._unit_state, now):
                since = time.strftime("%Y-%m-%d %H:%M", time.localtime(p["started"])) if p.get("started") else "?"
                state = f"running since {since}" if p["running"] else "NOT running"
                lines.append(f"{p['label']}: {state}")
        return "\n".join(lines)

    def changes(self, since: str | None) -> str:
        start = when.when(since) or self._now().replace(hour=0, minute=0) - timedelta(days=CHANGES_SPAN_DAYS)
        out = self._git("log", "--first-parent", f"--since={start.isoformat()}", "--format=%cs %s", f"-{MAX_CHANGES}")
        if not out:
            return (
                f"no changes since {start:%Y-%m-%d}" if self._git("rev-parse", "HEAD") else "error: not a git checkout"
            )
        return f"changes since {start:%Y-%m-%d}, newest first (a PR's title each):\n{out}"

    def docs(self, query: str) -> str:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        if not words:
            return "error: docs needs what to look up, in a few words"
        files = [self._root / "README.md", *sorted((self._root / "docs").glob("*.md"))]
        scored = []
        for path in files:
            if not path.exists():
                continue
            for heading, body in _sections(path.read_text()):
                text = f"{heading}\n{body}".lower()
                score = sum(text.count(w) for w in words) + 5 * sum(w in heading.lower() for w in words)
                if score and all(w in text for w in words[:2]):
                    scored.append((score, f"{path.name} — {heading}", body.strip()))
        if not scored:
            return f"the docs say nothing about {query!r}; try other words"
        best = sorted(scored, key=lambda s: -s[0])[:MAX_SECTIONS]
        return "\n\n".join(f"[{where}]\n{_cut(body, SECTION_CHARS)}" for _, where, body in best)

    # What it said and did.

    def records(self, query: str, since: str | None, until: str | None, request) -> str:
        if self._events is None:
            return "error: TARS keeps no records here ([learning] log_events is off)"
        start, end, _ = when.span(since, until, None, self._now())
        rows = self._events.store.rows(
            "SELECT t.conversation_id, t.ts, t.role, t.text, t.speaker, t.answered_by, t.failed_at, t.error, "
            "t.not_for_tars, c.via FROM turns t JOIN conversations c ON c.id = t.conversation_id "
            "WHERE t.ts >= ? AND t.ts < ? ORDER BY t.ts",
            (start.timestamp(), end.timestamp()),
        )
        owners: dict[int, str] = {}
        for r in rows:
            if r["via"] and r["role"] == "person" and r["speaker"]:
                owners.setdefault(r["conversation_id"], r["speaker"])
        asker = str(getattr(request, "speaker", "") or "").strip().lower()
        shown, hidden = [], set()
        for r in rows:
            owner = owners.get(r["conversation_id"])
            if r["via"] and (not owner or owner.strip().lower() != asker):
                hidden.add(r["conversation_id"])
                continue
            line = _turn(r)
            if not query or query.lower() in line.lower():
                shown.append(line)
        said = []
        if hidden:
            said.append(f"({len(hidden)} other people's chats in that span left out: they're theirs)")
        if not shown:
            said.append("nothing in that span" + (f" mentions {query!r}" if query else ""))
        else:
            if len(shown) > MAX_TURNS:
                said.append(f"({len(shown) - MAX_TURNS} earlier lines left out: narrow the span)")
            said += shown[-MAX_TURNS:]
        return _cut("\n".join(said), MAX_CHARS)

    # What it was asked for and couldn't do.

    def wish(self, asked: str, why: str, request) -> str:
        if self._events is None:
            return "error: TARS keeps no wishes here ([learning] log_events is off)"
        if not asked or not why:
            return "error: wish needs what they wanted (query) and what stopped you (why)"
        via = "chat" if getattr(request, "by_chat", False) else "voice"
        number = self._events.store.write(
            "INSERT INTO wishes (at, person, via, asked, why) VALUES (?, ?, ?, ?, ?)",
            (time.time(), getattr(request, "speaker", None), via, asked, why),
        )
        return (
            f"noted ({number}) for whoever looks after TARS; tell them in plain words you've passed it on, never "
            "that it's a wish or a list"
        )

    def wishes(self) -> str:
        if self._events is None:
            return "error: TARS keeps no wishes here ([learning] log_events is off)"
        rows = self._events.store.rows(
            "SELECT * FROM wishes WHERE granted IS NULL ORDER BY id DESC LIMIT ?", (MAX_WISHES,)
        )
        if not rows:
            return "no wishes: nothing asked for lately that TARS couldn't do"
        lines = []
        for r in rows:
            at = time.strftime("%Y-%m-%d", time.localtime(r["at"]))
            who = f"{r['person'] or 'someone'} ({r['via']})"
            lines.append(f"{r['id']}. {at}, {who}: {r['asked']} (stopped by: {r['why']})")
        return "\n".join(lines)

    def granted(self, number: str) -> str:
        if self._events is None:
            return "error: TARS keeps no wishes here ([learning] log_events is off)"
        if not number.strip().isdigit():
            return "error: granted needs the wish's number, as wishes lists them"
        with self._events.store.transaction() as db:
            done = db.execute(
                "UPDATE wishes SET granted=? WHERE id=? AND granted IS NULL", (time.time(), int(number))
            ).rowcount
        return f"wish {number} marked done" if done else f"error: there's no open wish {number}"

    # Its own settings.

    def _settings(self):
        from .settings import Settings

        return Settings(self._root, self._events.store if self._events is not None else None, run=self._run)

    def set(self, name: str, value: str, request) -> str:
        from .settings import SettingsError

        if getattr(request, "read_the_web", False):
            return "refused: this reply searched the web, so it changes nothing about TARS; they can ask on its own"
        if not (getattr(request, "speaker", None) or getattr(request, "by_voice", False)):
            return "refused: TARS changes its settings only for someone it knows, or someone at home asking aloud"
        try:
            return self._settings().set(name, value, getattr(request, "speaker", None))
        except SettingsError as e:
            return f"error: {e}"

    def log(self, query: str, since: str | None, until: str | None) -> str:
        if not shutil.which("journalctl"):
            return "error: there's no system log to read here (no journalctl); records has what was said and done"
        start, end, _ = when.span(since, until, None, self._now())
        units = [arg for _, unit in PROCESSES.values() for arg in ("--user-unit", unit)]
        done = self._run(
            ["journalctl", *units, "--since", f"{start:%Y-%m-%d %H:%M:%S}", "--until", f"{end:%Y-%m-%d %H:%M:%S}",
             "--no-pager", "-o", "short"],
            capture_output=True, text=True, timeout=20, check=False,
        )  # fmt: skip
        lines = []
        for raw in (done.stdout or "").splitlines():
            line = JOURNAL_PREFIX.sub("", raw)
            if CHAT_LINE.search(line) or (query and query.lower() not in line.lower()):
                continue
            stamp = raw[7:15] if JOURNAL_PREFIX.match(raw) else ""
            lines.append(f"{stamp} {line}".strip())
        if not lines:
            return "the log has nothing in that span" + (f" with {query!r}" if query else "")
        more = f"({len(lines) - MAX_LOG} earlier lines left out: narrow the span or the words)\n"
        return _cut((more if len(lines) > MAX_LOG else "") + "\n".join(lines[-MAX_LOG:]), MAX_CHARS)

    def version(self) -> str:
        """The commit TARS runs, short; "" outside a checkout."""
        return self._git("rev-parse", "--short", "HEAD")

    def between(self, old: str, new: str) -> str:
        """What changed from one version to another, newest first, a PR's title each: what a skill that runs on TARS
        being updated is told. A history rewritten since (old isn't before new) gives the latest few instead."""
        out = self._git("log", "--first-parent", "--format=%cs %s", f"-{MAX_CHANGES}", f"{old}..{new}")
        return out or self._git("log", "--first-parent", "--format=%cs %s", "-5", new)

    def _git(self, *args: str) -> str:
        try:
            done = self._run(
                ["git", "-C", str(self._root), *args], capture_output=True, text=True, timeout=10, check=False
            )
        except OSError, subprocess.TimeoutExpired:
            return ""
        return done.stdout.strip() if done.returncode == 0 else ""


def _sections(text: str) -> list[tuple[str, str]]:
    """A Markdown file as (heading, body) pairs, split at its ## and ### headings."""
    parts, heading, body = [], "(top)", []
    for line in text.splitlines():
        if line.startswith(("## ", "### ")):
            parts.append((heading, "\n".join(body)))
            heading, body = line.lstrip("#").strip(), []
        else:
            body.append(line)
    parts.append((heading, "\n".join(body)))
    return [(h, b) for h, b in parts if b.strip()]


def _turn(r: dict) -> str:
    at = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
    where = "chat" if r["via"] else "aloud"
    if r["role"] == "person":
        who = r["speaker"] or "someone"
        quiet = " (overheard; TARS stayed quiet)" if r["not_for_tars"] else ""
        return f"{at} [{where} #{r['conversation_id']}] {who}: {r['text']}{quiet}"
    by = f" (answered by {r['answered_by']})" if r["answered_by"] else ""
    failed = f" (FAILED at {r['failed_at']}: {r['error']})" if r["failed_at"] else ""
    return f"{at} [{where} #{r['conversation_id']}] TARS: {r['text'] or ''}{by}{failed}"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}\n(cut short)"
