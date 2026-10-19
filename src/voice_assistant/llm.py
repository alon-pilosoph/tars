"""The brain. Qwen answers first (QuickChat), on Groq or else on Cerebras, with its own tools for reminders and the
TARS page when `quick_tools` is on, and hands over the turns it can't do (the web, and whatever it has no tool for) to
OpenAI's model and its tools (OpenAIChat), and the questions that need real thinking to the thinking model. Each model
is told exactly what it can do, what it hands over, and what TARS can't do at all (abilities()). One shared history,
kept until the conversation goes quiet. Replies are streamed, and every reply can be stopped and forgotten."""

import itertools
import json
import re
import threading
import time
import types
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from openai import OpenAI, OpenAIError, RateLimitError

from .config import LLMConfig
from .conversations import FILE, HOUSEHOLD, KINDS, LINK, LIST, NOTE, PERSON, SentItem
from .home import CAN_HOME, NEEDS_HOME, ONE_MOMENT, HomeTools, Request
from .reminders import Change, ReminderTools

# The whole conversation is sent until it goes quiet for `memory_minutes`; this only stops a marathon session from
# growing the prompt forever.
MAX_TURNS = 50

FOLLOW_UP_TAG = "[Follow-up, no wake word]"
ASKED_TAG = "[Reply to your 'Did you call me?']"
SKIP = "<skip>"
SKIP_RULES = (
    f"Messages starting with {FOLLOW_UP_TAG} were overheard right after your last reply, without anyone "
    f"addressing you. If one isn't meant for you (people talking to each other, or unrelated to your "
    f"conversation), reply with exactly {SKIP} and nothing else.\n"
    f"Messages starting with {ASKED_TAG} answer the question you just asked because you weren't sure someone "
    f"said your name. If they ask for something, just do it; if it's a bare yes, ask briefly what they need; "
    f"if it's a no, or clearly not meant for you, reply with exactly {SKIP} and nothing else."
)
REMINDER_TAG = "[Reply to the reminder you just said]"
# How a reply acknowledges reminders: "<ack>" for the ones just said, "<ack 12>" (or "<ack 12, 14>") by number.
ACK = "<ack>"
ACK_MARK = re.compile(r"\s*<ack((?:[\s,]*\d+)*)\s*>")
GOT_IT = "Got it."  # said for a bare <ack>
ACK_RULES = (
    f"Messages starting with {REMINDER_TAG} answer a reminder, timer or message you just said aloud, or a timer "
    f"that's ringing. If it acknowledges it (got it, okay, thanks, will do, on it; or stop, off, turn it off, for "
    f"a timer), reply with exactly {ACK} and nothing else. If it asks "
    f"for something, just do it; to be reminded of it again later, put that one off with snooze_reminder, rather "
    f"than setting a new one or acknowledging it. If it isn't meant for you, reply with exactly {SKIP} and nothing "
    f"else."
)


def _escaped(marker: str) -> str:
    """A marker as Qwen sometimes writes it, HTML-escaped ("&lt;look-up&gt;"): the same marker, and never to be said."""
    return marker.replace("<", "&lt;").replace(">", "&gt;")


def _unescaped(text: str) -> str:
    """`text` with its markers as written, whether or not Qwen HTML-escaped them."""
    return text.replace("&lt;", "<").replace("&gt;", ">")


ACK_STARTS = ("<ack", _escaped("<ack"))


def split_ack(pieces: Iterable[str]) -> tuple[list[int] | None, Iterator[str]]:
    """Peek at the start of a streamed reply: (the reminder numbers acknowledged, [] for a bare <ack>, the rest of
    the reply) if it starts with an ack, as written or HTML-escaped, else (None, the whole reply). A bare <ack> with
    nothing after it says GOT_IT."""
    stream = iter(pieces)
    head = ""
    for piece in stream:
        head += piece
        start = head.lstrip()
        maybe = any(form.startswith(start[: len(form)]) for form in ACK_STARTS)
        if not maybe or ">" in _unescaped(start) or len(start) > 40:
            break
    unescaped = _unescaped(head)
    if not (mark := ACK_MARK.match(unescaped)):
        return None, itertools.chain([head], stream)
    ids = [int(n) for n in re.findall(r"\d+", mark.group(1))]
    return ids, _or_else(itertools.chain([unescaped[mark.end() :]], stream), GOT_IT)


def _or_else(pieces: Iterable[str], default: str) -> Iterator[str]:
    """The reply without its leading whitespace, or `default` if that's all there was."""
    said = False
    for piece in pieces:
        if not said:
            piece = piece.lstrip()
            said = bool(piece)
        if piece:
            yield piece
    if not said:
        yield default


# What TARS can't do. Without saying so, a model asked for a timer TARS can't set says "Twelve minutes, starting
# now." and nothing ever goes off.
CANT = "play music or sounds, call anyone, or control anything in the house"
CANT_BUT_HOME = "play music or sounds, call anyone, or work anything in the house the home tool doesn't list"
CANT_REMIND = "set timers, alarms or reminders"
CANT_WEB = "look anything up on the web"
CANT_SEND = "send anything to a phone or the TARS page"
# What a model can do itself, with its tools.
CAN_WEB = "look things up on the web, with web search"
CAN_SEND = "send a link, a note, a list or a text file to the household's TARS page, with the send tool"
CAN_SEND_NO_LINKS = "send a note, a list or a text file to the household's TARS page, with the send tool"
CAN_REMIND = "set, cancel and put off timers, reminders and messages for people in the house, with your tools"
REMIND_RULES = (
    "You can set timers, reminders and messages for people in the house with the remind tool; TARS says them aloud "
    'when they\'re due. A reminder or message is for the person named ("remind me" is whoever is speaking, by '
    'the [Speaker: name] tag; "remind Stacey" is Stacey). Something to tell someone ("tell Stacey dinner\'s at '
    "eight\") is a message, for them; a reminder is something to do. A message always needs someone it's for. Give the "
    'time as in_minutes for "in ten minutes", or for a clock time, time as HH:MM on a 24-hour clock, with day as '
    'it was said ("friday", "tomorrow"; null if no day was said): never work out a date yourself. Morning is 9:00, '
    "afternoon 15:00, evening 19:00 and tonight 20:00, unless a time was said. Use when_back "
    "only when asked to wait until someone is back or next around. Set wait_for_ack, so it's said again until "
    "someone says they got it, unless they say once is enough; a timer always rings until someone turns it off. "
    "After setting one, confirm it in one short line with the time the tool says it's set for. To cancel one or "
    "put it off, use cancel_reminder or snooze_reminder with its number from the list of what's set now; when more "
    'than one there could be meant (two timers, and "cancel the timer"), ask which, and change nothing yet. To have '
    "one said again in a while, put it off; to move one to another time, cancel it and set it again. Never set a "
    'second one for the same thing and the same person. The text is only what it\'s about ("call mum"), never the '
    "words TARS says around it in that list. If a tool "
    "says there's an error, fix it or ask, and never say it's set when it isn't."
)
REMIND_TOOLS = [
    {
        "type": "function",
        "name": "remind",
        "description": "Set a timer, a reminder or a message for someone, said aloud by TARS when it's due.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "text", "for", "in_minutes", "day", "time", "when_back", "wait_for_ack"],
            "properties": {
                "kind": {"type": "string", "enum": ["timer", "reminder", "message"]},
                "text": {
                    "type": ["string", "null"],
                    "description": 'What to say, short, as said to them ("call the bank"). A timer\'s optional '
                    'label ("pasta").',
                },
                "for": {"type": ["string", "null"], "description": "Who it's for, by name; null for whoever is there."},
                "in_minutes": {"type": ["number", "null"], "description": "Due this many minutes from now."},
                "day": {
                    "type": ["string", "null"],
                    "description": "With time: the day as said: today, tomorrow, a weekday (monday...sunday), or "
                    "YYYY-MM-DD for a date. null: the next time the clock shows that time.",
                },
                "time": {"type": ["string", "null"], "description": "Due at this clock time, HH:MM, 24-hour."},
                "when_back": {"type": "boolean", "description": "Wait until their voice is next heard instead."},
                "wait_for_ack": {"type": "boolean", "description": "Say it again until someone acknowledges it."},
            },
        },
    },
    {
        "type": "function",
        "name": "cancel_reminder",
        "description": "Cancel a timer, reminder or message, by its number.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id"],
            "properties": {"id": {"type": "integer"}},
        },
    },
    {
        "type": "function",
        "name": "snooze_reminder",
        "description": "Say a timer, reminder or message again later instead, by its number.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "minutes"],
            "properties": {"id": {"type": "integer"}, "minutes": {"type": "number"}},
        },
    },
]
SEND_RULES = (
    "You can send things to the household's TARS page (a web app they open on their phone or laptop) with the "
    "send tool: a link, a note, a list, or a text file. Use it when asked to send, save or share something, or "
    "when the answer is a link, a recipe, a list or anything too long to hear. Send links from your web search "
    "results, never made-up ones; one link, unless more were asked for. When the send tool answers that it sent "
    "something, it's on the TARS page: say so in one short line, and don't send it again. Never say a link or a web "
    "address out loud, not even a site's name with its .com: that's what the TARS page is for. In anything you send, "
    "write like a person: no middots (·) or em dashes."
)
# The quick model has no web search, so no real links: those turns are handed over.
QUICK_SEND_RULES = (
    "You can send things to the household's TARS page (a web app they open on their phone or laptop) with the "
    "send tool: a note, a list, or a text file. Use it when asked to send, save or share something, or when the "
    "answer is a recipe, a list or anything too long to hear. Never put a link or a web address in it{links}. After "
    "sending, say in one short line that you sent it and that it's on the TARS page. In anything you send, write like "
    "a person: no middots (·) or em dashes."
)
# How the quick model is told it can't send a link: hand the turn over, or say so when nothing can.
LINK_HAND_OFF = (
    ": when someone asks for a link, reply <look-up>, even for something you could write out yourself, like a recipe"
)
NO_LINKS = ": you can't send links"

# Strict mode needs every field listed as required; the ones a kind doesn't use are nullable instead.
SEND_TOOL = {
    "type": "function",
    "name": "send",
    "description": "Send a link, note, list or text file to the household's TARS page.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "title", "for", "url", "site", "description", "body", "entries", "file_name", "file_text"],
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "title": {"type": "string", "description": "Short, like a headline."},
            "for": {
                "type": "string",
                "enum": [PERSON, HOUSEHOLD],
                "description": "person: whoever asked. household: things the house shares, like a shopping list.",
            },
            "url": {"type": ["string", "null"], "description": "link: the address, found with web search."},
            "site": {"type": ["string", "null"], "description": "link: the site's name, e.g. BBC Good Food."},
            "description": {"type": ["string", "null"], "description": "link: one line on what it is."},
            "body": {"type": ["string", "null"], "description": "note: short markdown (headings, lists, bold)."},
            "entries": {"type": ["array", "null"], "items": {"type": "string"}, "description": "list: the items."},
            "file_name": {
                "type": ["string", "null"],
                "description": "file: a name with an extension: .txt, .md, .csv or .ics.",
            },
            "file_text": {"type": ["string", "null"], "description": "file: the whole content."},
        },
    },
}
# The send tool without links, for the quick model.
LINK_FIELDS = ("url", "site", "description")
QUICK_SEND_TOOL = {
    **SEND_TOOL,
    "description": "Send a note, list or text file to the household's TARS page.",
    "parameters": {
        **SEND_TOOL["parameters"],
        "required": [f for f in SEND_TOOL["parameters"]["required"] if f not in LINK_FIELDS],
        "properties": {
            **{k: v for k, v in SEND_TOOL["parameters"]["properties"].items() if k not in LINK_FIELDS},
            "kind": {"type": "string", "enum": [k for k in KINDS if k != LINK]},
        },
    },
}
# A web search takes several seconds: say something the moment one starts, instead of going silent.
SEARCHING = "Looking it up."
# Said the moment a question goes to the thinking model, which can take a while before its first word.
THINKING = "Let me think about that for a moment."
FILE_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv", ".ics": "text/calendar"}
MAX_TOOL_ROUNDS = 3
# What the quick model replies when a turn needs OpenAI's tools; that turn is then OpenAI's to answer.
LOOK_UP = "<look-up>"
# What it replies when a question needs real thinking; the thinking model answers it. Not "<think>": Qwen 3 models
# write their own reasoning between <think> tags.
PONDER = "<ponder>"
# How Qwen starts a tool call when it writes one out as text instead of making it, now and then in a long reply. Its
# words go to the voice as they come, so the reply is cut off there, unsaid, and OpenAI takes the turn.
TOOL_CALL_TEXT = ("<toolcall>", "<tool_call>", "<function=")


# After a quick service fails (down, slow), it's skipped for this long, so each turn doesn't wait out its timeout
# first. After "too many requests", it's skipped for as long as the service asks (Retry-After), or for the minute its
# limits are counted over if it doesn't say.
QUICK_COOLDOWN_S = 120.0
RATE_LIMITED_S = 60.0
# A quick reply's cap, reasoning included, unless its service has its own (config.QUICK_PROVIDERS). Groq refuses a
# request that doesn't say: it assumes 2,048 tokens, over its free key's 1,000 a minute.
QUICK_MAX_TOKENS = 500
# If the quick service hasn't started answering by then, the next one is asked too, and the first to answer is kept.
HEDGE_S = 0.5
# Who answered a reply (Brain.answered_by), kept with the turn: Qwen, OpenAI because Qwen handed the turn over, the
# thinking model because Qwen handed it a hard question, OpenAI because every quick service failed or is cooling
# down, or OpenAI as the only brain.
QUICK, LOOKED_UP, PONDERED, FALLBACK, OPENAI = "quick", "look_up", "ponder", "fallback", "openai"
NEEDS_THE_WEB = "anything current or live: the weather, news, sports results, prices or opening hours, or a real link"
NEEDS_SENDING = (
    "sending, saving or sharing something to the household's TARS page: a recipe, a list, a note, a link "
    "or a file, or any answer too long to hear"
)
NEEDS_REMINDING = "setting, cancelling or putting off a timer, a reminder or a message for someone"
NEEDS_THINKING = (
    "a question that needs real thinking: planning something, comparing options, or working through several steps "
    "or a tricky calculation; never small talk, a plain fact, or anything the web answers"
)
# A hand-off's first words are already said when OpenAI's model takes over ("Timer set. Let me think about that for
# a moment."): it carries on from them, rather than starting the reply again.
CARRY_ON = (
    'This reply has already begun, aloud: "{said}" Carry on from there, without repeating any of it or confirming '
    "again what it already says was done."
)
THINK_RULES = (
    "This question was handed to you because it needs real thinking. Work it through carefully, then answer in a "
    "few short sentences; send anything longer to the TARS page."
)


def local_time() -> str:
    """The date and time here, which no model knows. Goes last in the instructions, so the rest can be cached."""
    now = datetime.now().astimezone()
    clock = f"{now:%I:%M %p}".lstrip("0")
    return f"It's {now:%A, %B} {now.day}, {now.year}, {clock} here ({now.tzname()}, UTC{now:%:z})."


# How each hand-off is introduced: the quick model can't do the first at all, and does the second less well.
LOOK_UP_INTRO = "You can't do these yourself, but another model can."
PONDER_INTRO = "A stronger model, which takes longer, does these better than you."


def home_rule(cfg: LLMConfig) -> list[str]:
    """Where the household is, for the instructions, if it's set."""
    place = ", ".join(p for p in (cfg.home_city, cfg.home_country) if p)
    if not place:
        return []
    rule = (
        f"The household is in {place}. Questions about the weather, the time, opening hours, prices or anything "
        "nearby are about there, unless another place is named; never ask where they are."
    )
    return [rule]


def web_search_tool(cfg: LLMConfig) -> dict:
    """OpenAI's web search, told roughly where home is when it's set, so results are local."""
    where = {k: v for k, v in (("city", cfg.home_city), ("country", cfg.home_country)) if v}
    if not where:
        return {"type": "web_search"}
    return {"type": "web_search", "user_location": {"type": "approximate", **where}}


def abilities(can: list[str], hand_offs: list[tuple[str, str, list[str], list[str]]], cant: list[str]) -> str:
    """What a model can do itself (`can`), what it hands over and how ((marker, intro, what for, examples)), and what
    TARS can't do at all, from one place, so it never claims what it can't do or hands over what it could have done."""
    parts = [f"What you can do yourself: {'; '.join(can)}."] if can else []
    for marker, intro, needs, examples in hand_offs:
        quoted = " or ".join(f'"{e}"' for e in examples)
        parts.append(
            f"{intro} When a message needs any of them, reply with exactly {marker} and nothing else, and it "
            "answers instead:\n"
            + "\n".join(f"- {n}" for n in needs)
            + f"\nFor example, {quoted} get{'s' * (len(examples) == 1)} {marker}."
        )
    if hand_offs:
        parts.append(
            "Never say you can't do something you can hand over, or that you'll look it up or think it over: hand it "
            "over. Answer everything else yourself."
        )
    if hand_offs and can:
        markers = " or ".join(marker for marker, *_ in hand_offs)
        parts.append(
            "When one message asks for something you can do and something you hand over, do your part first, with your "
            f"tools, then reply with exactly {markers} and nothing else, without starting on the rest yourself: "
            "whoever answers next sees what you did, and answers the rest."
        )
    parts.append(
        f"You can't {', '.join(cant)}. When asked to, say plainly in one short line that you can't do that yet, and "
        "offer what you can do instead if something fits. Never say you did something you can't do."
    )
    return "\n\n".join(parts)


def check_send(args: dict) -> tuple[SentItem | None, str]:
    """(the item to send, "sent: ...") if the send call is complete, else (None, what's wrong) for the model to fix.
    The answer says plainly that it went through: told only "sent", OpenAI's model now and then sent another, and
    said it couldn't."""
    if not isinstance(args, dict):
        return None, "error: the arguments must be an object"
    kind, title = args.get("kind"), _text(args.get("title"))
    if not title:
        return None, "error: a title is required"
    item = SentItem(kind, title, scope=HOUSEHOLD if args.get("for") == HOUSEHOLD else PERSON)
    if kind == LINK:
        url = _text(args.get("url"))
        if not url.startswith(("https://", "http://")) or len(url.split("//", 1)[1]) < 3:
            return None, "error: a link needs a full http(s) url from web search"
        item.url, item.site = url, _text(args.get("site")) or None
        item.description = _text(args.get("description")) or None
    elif kind == NOTE:
        if not (body := _text(args.get("body"))):
            return None, "error: a note needs a body"
        item.body = body
    elif kind == LIST:
        entries = args.get("entries")
        entries = [_text(e) for e in entries if _text(e)] if isinstance(entries, list) else []
        if not entries:
            return None, "error: a list needs entries"
        item.entries = entries
    elif kind == FILE:
        name, text = _text(args.get("file_name")), args.get("file_text")
        mime = next((m for ext, m in FILE_TYPES.items() if name.lower().endswith(ext)), None)
        if not mime or not isinstance(text, str) or not text:
            return None, f"error: a file needs a name ending in {', '.join(FILE_TYPES)} and its text"
        item.file_name, item.file_bytes, item.mime = name, text.encode(), mime
    else:
        return None, f"error: kind must be one of {', '.join(KINDS)}"
    return item, f"sent: {item.title!r} is on the TARS page; it needs sending only once"


# A web address, in anything the quick model sends: it has no web search, so any link would be from memory.
WEB_ADDRESS = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|org|net|io|gov|edu|co\.uk)\b", re.IGNORECASE)


def _same_send(a: SentItem, b: SentItem) -> bool:
    """The same thing sent twice: the same kind, and one title is the other with words added ("Three days in
    Lisbon", "Three days in Lisbon, for two")."""
    ta, tb = (" ".join(re.sub(r"[^\w\s]", " ", t.lower()).split()) for t in (a.title, b.title))
    return a.kind == b.kind and bool(ta and tb) and (ta in tb or tb in ta)


def _has_link(args: dict) -> bool:
    """Whether a send call is a link or has a web address anywhere in it."""
    return args.get("kind") == LINK or bool(WEB_ADDRESS.search(json.dumps(args, ensure_ascii=False)))


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


class ReplyFailed(OpenAIError):
    pass


class Brain(Protocol):
    sent: list[SentItem]  # what the last reply sent to the web UI
    answered_by: str | None  # who wrote the last reply (QUICK, LOOKED_UP, PONDERED, FALLBACK or OPENAI), once known
    quick_service: str | None  # the service whose quick reply was used (Groq, Cerebras), hand-offs included
    changes: list[Change]  # what the last reply asked to change in the reminders, made once it's kept

    def stream_reply(self, text: str, request: Request | None = None) -> Iterator[str]:
        """Yield the reply in pieces as it's generated. `request`: who asked, how, and whether the turn is over yet,
        for the home tool; None for a typed turn, over already, from nobody in particular."""

    def forget_last(self) -> None:
        """Drop the most recent exchange from the conversation history, finished or not. Once only."""

    def remember_said(self, text: str) -> None:
        """TARS said `text` without being asked (a reminder posted in a chat): part of the conversation from now."""

    def start_over(self) -> None:
        """Forget the whole conversation: the next question starts a new one."""

    def interrupt(self) -> None:
        """From another thread: stop writing the reply in progress; its stream ends with an error."""

    def warm(self) -> None:
        """Get a connection ready, when TARS wakes, so the reply doesn't wait to connect."""


SKIPS = (SKIP, _escaped(SKIP))


def split_skip(pieces: Iterable[str]) -> tuple[bool, Iterator[str]]:
    """Peek at the start of a streamed reply: (True, empty) if the model chose to skip, as written or HTML-escaped,
    else the full stream."""
    stream = iter(pieces)
    head = ""
    for piece in stream:
        head += piece
        start = head.lstrip()
        if not any(len(start) < len(form) and form.startswith(start) for form in SKIPS):
            break
    if head.strip().startswith(SKIPS):
        for _ in stream:  # Finish the stream so the exchange is recorded before it's forgotten.
            pass
        return True, iter(())
    return False, itertools.chain([head], stream)


class _UpTo:
    """A streamed reply up to the first of `markers`, holding back only what might be the start of one. `found`: the
    marker that came, if one did."""

    def __init__(self, pieces: Iterable[str], markers: tuple[str, ...]):
        self._pieces, self._markers = pieces, markers
        self._longest = max(map(len, markers), default=0)
        self.found: str | None = None

    def __iter__(self) -> Iterator[str]:
        pending = ""
        for piece in self._pieces:
            pending += piece
            if hits := [(at, m) for m in self._markers if (at := pending.find(m)) >= 0]:
                at, self.found = min(hits)
                if at:
                    yield pending[:at]
                return
            hold = next(
                (
                    i
                    for i in range(max(0, len(pending) - self._longest), len(pending))
                    if any(m.startswith(pending[i:]) for m in self._markers)
                ),
                len(pending),
            )
            if hold:
                yield pending[:hold]
            pending = pending[hold:]
        if pending:
            yield pending


class _NoRepeat:
    """A tool round's words, without what an earlier round of the same reply already said: Qwen now and then says
    "Timer's set for 12:03." with its tool call, and again once it has the result. Held back only while they could
    still be that repeat (compared without case or spacing); a round that ends while they still could, repeated them
    and says nothing."""

    def __init__(self, before: str):
        self._before, self._held, self._passing = _squeeze(before), "", not before.strip()

    def feed(self, piece: str) -> str:
        if self._passing:
            return piece
        self._held += piece
        held = _squeeze(self._held)
        if self._before.startswith(held):
            return ""  # still the same words, so far
        self._passing = True
        if held.startswith(self._before):  # the repeat, then something new: only the new part
            return _after_squeezed(self._held, len(self._before))
        return self._held  # something else: what was held back is said after all


def _squeeze(text: str) -> str:
    return " ".join(text.lower().split())


def _after_squeezed(text: str, n: int) -> str:
    """What's left of `text` once its start, squeezed, is `n` characters long. Short texts: a plain scan is fine."""
    for i in range(len(text) + 1):
        if len(_squeeze(text[:i])) >= n:
            return text[i:]
    return ""


class _Writing:
    """One reply being written. Each has its own stop, so a stopped reply still waiting on the network can't
    touch the conversation after the next one has started."""

    def __init__(self, answered_by: str | None = None):
        self.stopped: str | None = None  # why, once it's stopped
        self.stream = None  # the response being read, so interrupt() can close it
        self.answered_by = answered_by
        self.quick_service: str | None = None
        # The quick model's tool calls and their results, as Responses API items, for a model it hands over to.
        self.quick_calls: list[dict] = []
        self.request = Request()  # the turn it answers, for the home tool


class OpenAIChat:
    """The conversation, through OpenAI's Responses API: streamed text, plus web search and the send tool."""

    first_answerer = OPENAI

    def __init__(
        self,
        client: OpenAI | None,
        cfg: LLMConfig,
        reminders: ReminderTools | None = None,
        clock: Callable[[], float] | None = None,
        channel_rules: str = "",
        remind_tools: list[dict] | None = None,
        home: HomeTools | None = None,
    ):
        """`reminders`: TARS's timers, reminders and messages, when it has them. No `client` (no OpenAI key): only
        QuickChat, whose Qwen then answers everything. `clock`: seconds, for when the conversation went quiet and when
        a failed service is back (time.monotonic; the evals pass their own, to skip ahead). `channel_rules`: how
        this conversation differs from one aloud (a Telegram chat, written), last in the instructions so they win.
        `remind_tools`: the reminder tools, if not REMIND_TOOLS (a chat's can choose to have one said aloud).
        `home`: the devices in the house, when there are any."""
        self._clock = clock or time.monotonic
        self._channel_rules = [channel_rules] if channel_rules else []
        self._remind_tools = remind_tools or REMIND_TOOLS
        self._client = client
        self._cfg = cfg
        self._reminders = reminders
        self._home = home if home and home.devices else None
        self._history: list[dict] = []
        self._asked: dict | None = None  # the question of the reply in progress, or the last one
        self._writing = _Writing()
        # A reply is written on the voice's thread while the next can start, or be forgotten, on another.
        self._lock = threading.Lock()
        self._last_turn_at = self._previous_turn_at = 0.0
        self.sent: list[SentItem] = []
        self.changes: list[Change] = []
        self._tools = (
            ([web_search_tool(cfg)] if cfg.web_search else [])
            + ([SEND_TOOL] if cfg.send else [])
            + (self._remind_tools if reminders else [])
            + ([self._home.tool] if self._home else [])
        )
        self._system_prompt = cfg.system_prompt.replace("{humor}", str(cfg.humor))
        can = [CAN_WEB] * cfg.web_search + [CAN_SEND] * cfg.send + [CAN_REMIND] * bool(reminders)
        can += [CAN_HOME] * bool(self._home)
        rules = [*home_rule(cfg), SKIP_RULES, *self._reminder_rules()] + [SEND_RULES] * cfg.send
        rules += [self._home.rules()] if self._home else []
        rules.append(abilities(can, [], self._cant()))
        self._instructions = "\n\n".join([self._system_prompt, *rules, *self._channel_rules])
        # Only send optional settings that are configured; not every model accepts them.
        self._extra = {}
        if cfg.service_tier:
            self._extra["service_tier"] = cfg.service_tier
        if cfg.reasoning_effort:
            self._extra["reasoning"] = {"effort": cfg.reasoning_effort}

    def stream_reply(self, text: str, request: Request | None = None) -> Iterator[str]:
        with self._lock:
            self._forget_if_quiet()
            self._previous_turn_at, self._last_turn_at = self._last_turn_at, self._clock()
            self._asked = {"role": "user", "content": text}
            self._history.append(self._asked)
            self.sent, self.changes = [], []
            self._writing = writing = _Writing(self.first_answerer)
            writing.request = request or Request()
            # Set up now rather than on the first read, so an interrupt() in between isn't lost.
            return self._stream_reply(list(self._history), writing)

    def _stream_reply(
        self, context: list, writing: _Writing, said_first: str = "", think: bool = False
    ) -> Iterator[str]:
        """`said_first`: what TARS already said of this reply (a hand-off's first sentence), for the history.
        `think`: a hard question, for the thinking model at its reasoning effort."""
        answer = said_first
        model, extra, instructions = self._cfg.model, self._extra, self._instructions
        if think:
            model = self._cfg.think_model or self._cfg.model
            extra = {
                **self._extra,
                "reasoning": {"effort": self._cfg.think_effort},
                "timeout": self._cfg.think_timeout_s,
            }
            instructions = f"{self._instructions}\n\n{THINK_RULES}"
        if said_first.strip():
            instructions = f"{instructions}\n\n{CARRY_ON.format(said=said_first.strip())}"
        for round_ in range(MAX_TOOL_ROUNDS):
            calls, said = [], ""
            tools = {"tools": self._tools} if self._tools else {}
            if tools and round_ == MAX_TOOL_ROUNDS - 1:
                tools["tool_choice"] = "none"  # the last round has to say something, or TARS goes silent
            _check(writing)
            stream = writing.stream = self._client.responses.create(
                model=model,
                instructions=f"{instructions}\n\n{self._now()}",
                input=context,
                stream=True,
                store=False,  # nothing kept on OpenAI's side beyond the request itself
                **extra,
                **tools,
            )
            for event in stream:
                _check(writing)
                if event.type == "response.output_text.delta" and event.delta:
                    said += event.delta
                    yield event.delta
                elif event.type == "response.web_search_call.in_progress" and not (answer.strip() or said):
                    said = f"{SEARCHING} "
                    yield said
                elif event.type == "response.output_item.done" and event.item.type == "function_call":
                    calls.append(event.item)
                elif event.type in ("error", "response.failed", "response.incomplete"):
                    # The SDK only raises for some of these; the rest would otherwise end the reply in silence.
                    raise ReplyFailed(f"{event.type}: {_why(event)}")
            writing.stream = None
            answer += said
            if not calls:
                break
            if said:
                context.append({"role": "assistant", "content": said})
            if moment := self._one_moment([(c.name, c.arguments) for c in calls], answer):
                answer += moment
                yield moment
            with self._lock:
                _check(writing)
                for call in calls:
                    context.append(
                        {
                            "type": "function_call",
                            "call_id": call.call_id,
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                    )
                    output = self._run_tool(call, writing=writing)
                    context.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
        if not answer.strip() and not self.sent:
            raise ReplyFailed("the reply was empty")
        self._remember(answer, writing)

    def _reminder_rules(self, tools: bool = True) -> list[str]:
        """`tools`: this model sets them itself, rather than handing the turn over."""
        if not self._reminders:
            return []
        return [ACK_RULES] + ([REMIND_RULES] if tools else [])

    def _cant(self) -> list[str]:
        """What TARS can't do at all, whichever model answers."""
        cfg = self._cfg
        return (
            [CANT_REMIND] * (not self._reminders)
            + [CANT_WEB] * (not cfg.web_search)
            + [CANT_SEND] * (not cfg.send)
            + [CANT_BUT_HOME if self._home else CANT]
        )

    def _one_moment(self, calls: list[tuple[str, str]], said: str) -> str:
        """ONE_MOMENT, to say before tool calls that take a few seconds (a lock turning), if nothing's been said."""
        slow = self._home and any(name == "home" and self._home.slow(arguments) for name, arguments in calls)
        return f"{ONE_MOMENT} " if slow and not said.strip() else ""

    def _now(self) -> str:
        """What changes from one request to the next, so it goes after the instructions that can be cached."""
        parts = [local_time()]
        if self._reminders:
            try:
                parts.append(self._reminders.note())
            except Exception as e:  # noqa: BLE001 - a reminder list that can't be read never costs a reply
                print(f"(couldn't read the reminders: {e!r})")
        return "\n\n".join(p for p in parts if p)

    @property
    def answered_by(self) -> str | None:
        return self._writing.answered_by

    @property
    def quick_service(self) -> str | None:
        return self._writing.quick_service

    def _remember(self, answer: str, writing: _Writing) -> None:
        with self._lock:
            _check(writing)
            self._history.append({"role": "assistant", "content": answer.strip()})
            self._history = self._history[-MAX_TURNS * 2 :]

    def _run_tool(self, call, no_links: str = "", writing: _Writing | None = None) -> str:
        """Called with the lock held. `no_links`: what this model should do instead of sending a link, which needs
        web search; empty if it can. `writing`: the reply the call is part of, whose request the home tool goes by.
        Raises ReplyFailed if that reply was stopped while the house answered."""
        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            return "error: the arguments weren't valid JSON"
        if call.name == "send" and no_links and isinstance(args, dict) and _has_link(args):
            return f"error: a link needs web search, which you can't do: {no_links}"
        if call.name == "send":
            item, result = check_send(args)
            if item and any(_same_send(s, item) for s in self.sent):
                return "already sent: it's on the TARS page once"  # asked twice in one reply
            if item:
                self.sent.append(item)
            return result
        if self._reminders and call.name in ("remind", "cancel_reminder", "snooze_reminder"):
            try:
                args = args if isinstance(args, dict) else {}
                change, result = self._reminders.call(call.name, args, pending=self.changes)
            except Exception as e:  # noqa: BLE001 - the table couldn't be read: the model says it didn't work
                return f"error: couldn't reach the reminders ({e!r})"
            if change:
                self.changes.append(change)
            return result
        if self._home and call.name == "home":
            # A device can take seconds (a bolt turning), and a change first waits for the turn to be over: the
            # conversation isn't held meanwhile, so nothing else the brain does waits on the house.
            writing = writing or self._writing
            self._lock.release()
            try:
                result = self._home.call(args, writing.request)
            except Exception as e:  # noqa: BLE001 - a device that fails never costs the reply: the model says so
                result = f"error: couldn't reach the device ({e!r}); nothing is known to have changed"
            finally:
                self._lock.acquire()
            # Stopped meanwhile, its other calls aren't made: what they sent or set would land on the next reply.
            _check(writing)
            return result
        return f"error: no tool called {call.name}"

    def forget_last(self) -> None:
        with self._lock:
            if self._asked is None:
                return
            # From the question on, whether or not the answer got written; found by identity, since the history
            # may have been trimmed from the front since.
            for i in range(len(self._history) - 1, -1, -1):
                if self._history[i] is self._asked:
                    del self._history[i:]
                    break
            self._asked = None
            # Overheard chatter shouldn't keep the memory alive either.
            self._last_turn_at = self._previous_turn_at
            self.sent, self.changes = [], []

    def remember_said(self, text: str) -> None:
        with self._lock:
            self._forget_if_quiet()
            self._history.append({"role": "assistant", "content": text})
            self._history = self._history[-MAX_TURNS * 2 :]
            # Both: forget_last() for a question asked before this goes back to when this was said, not before.
            self._previous_turn_at = self._last_turn_at = self._clock()

    def start_over(self) -> None:
        with self._lock:
            self._history.clear()
            self._asked = None
            self._last_turn_at = self._previous_turn_at = 0.0

    def _forget_if_quiet(self) -> None:
        """After a long pause, start fresh: old context confuses more than it helps, and it's private. Call with the
        lock held."""
        if self._history and self._clock() - self._last_turn_at > self._cfg.memory_minutes * 60:
            self._history.clear()

    def interrupt(self) -> None:
        _stop(self._writing, "interrupted")

    def warm(self) -> None:
        if self._client is None:
            return
        # The cheapest request there is; the OpenAI voice reuses the same connection.
        try:
            self._client.models.retrieve(self._cfg.model)
        except OpenAIError:
            pass  # only a head start: the real request reports a failure


@dataclass
class QuickService:
    name: str
    client: OpenAI
    model: str
    max_tokens: int = QUICK_MAX_TOKENS
    back_at: float = 0.0


type _Opened = tuple[object, Iterator]


def _cooldown(e: Exception) -> float:
    try:
        return float(e.response.headers["retry-after"])
    except AttributeError, KeyError, ValueError:
        return RATE_LIMITED_S if isinstance(e, RateLimitError) else QUICK_COOLDOWN_S


class QuickChat(OpenAIChat):
    """Answers with Qwen on the first of its services that isn't failing (at low reasoning with its tools, Groq about
    0.4 s to the first sentence, Cerebras about 0.6 s, against about 0.7 s for OpenAI's), with its own tools when
    `quick_tools` is on, and hands a turn to OpenAI's model, tools and all, when it needs the web (or anything it has
    no tool for), to the thinking model when it needs real thinking, and to OpenAI when every service fails. One
    conversation: each sees what the others said."""

    first_answerer = QUICK

    def __init__(
        self,
        client: OpenAI | None,
        cfg: LLMConfig,
        quick: list[QuickService],
        reminders: ReminderTools | None = None,
        clock: Callable[[], float] | None = None,
        channel_rules: str = "",
        remind_tools: list[dict] | None = None,
        home: HomeTools | None = None,
    ):
        super().__init__(client, cfg, reminders, clock, channel_rules, remind_tools, home)
        self._quick = quick
        own_remind, own_send = cfg.quick_tools and bool(reminders), cfg.quick_tools and cfg.send
        # Qwen works the house itself with its own tools, or when there's no OpenAI to hand it to.
        own_home = (cfg.quick_tools or client is None) and bool(self._home)
        tools = self._remind_tools * own_remind + [QUICK_SEND_TOOL] * own_send
        tools += [self._home.tool] if own_home else []
        self._quick_tool_defs = tools  # as the Responses API takes them
        self._quick_tools = [_chat_tool(t) for t in tools]
        can = [CAN_REMIND] * own_remind + [CAN_SEND_NO_LINKS] * own_send + [CAN_HOME] * own_home
        look_up = [NEEDS_THE_WEB] * cfg.web_search + [NEEDS_SENDING] * (cfg.send and not own_send)
        look_up += [NEEDS_REMINDING] * (bool(reminders) and not own_remind and client is not None)
        look_up += [NEEDS_HOME] * (bool(self._home) and not own_home)
        examples = ["how much is a flight to Rome?"] + ["add batteries to my to-do list"] * (cfg.send and not own_send)
        hand_offs = [(LOOK_UP, LOOK_UP_INTRO, look_up, examples)] if look_up else []
        if cfg.think_effort:
            ponder = ["plan three days in Rome for us", "should we lease the car or buy it?"]
            hand_offs.append((PONDER, PONDER_INTRO, [NEEDS_THINKING], ponder))
        self._markers = tuple(marker for marker, *_ in hand_offs)
        # What Qwen's reply is watched for, as written or HTML-escaped, and the marker each one is.
        self._watch = {form: m for m in self._markers + TOOL_CALL_TEXT for form in (m, _escaped(m))}
        self._no_links = f"reply {LOOK_UP} instead" if LOOK_UP in self._markers else "say you can't send links"
        links = LINK_HAND_OFF if LOOK_UP in self._markers else NO_LINKS
        rules = [*home_rule(cfg), SKIP_RULES, *self._reminder_rules(tools=own_remind)]
        rules += [QUICK_SEND_RULES.format(links=links)] * own_send
        rules += [self._home.rules()] if own_home else []
        cant = self._cant() + [CANT_REMIND] * (bool(reminders) and not own_remind and client is None)
        rules.append(abilities(can, hand_offs, cant))
        self._quick_instructions = "\n\n".join([self._system_prompt, *rules, *self._channel_rules])
        effort = cfg.quick_reasoning_effort or cfg.reasoning_effort
        self._quick_extra = {"reasoning_effort": effort} if effort else {}

    def _stream_reply(self, context: list, writing: _Writing) -> Iterator[str]:
        answer, handed_off, cut_off = "", None, False
        ready = [service for service in self._quick if self._clock() >= service.back_at]
        if not ready and self._client is None:
            ready = list(self._quick)
        while ready:
            service, opened, failed = self._first_to_answer(ready, context, writing)
            for gone, e in failed:
                self._cool_down(gone, e)
                ready.remove(gone)
            if service is None:
                break
            writing.quick_service = service.name
            quick = self._quick_reply(service, context, writing, opened)
            # Usually the whole reply is the marker; now and then it comes after a sentence ("I can't check that.
            # <look-up>").
            reply = _UpTo(quick, tuple(self._watch))
            try:
                for piece in reply:
                    answer += piece
                    yield piece
                found = self._watch.get(reply.found)
                if found in TOOL_CALL_TEXT:
                    print(f"({service.name} wrote a tool call out as text)")
                    cut_off = True
                else:
                    handed_off = found
                writing.answered_by = {LOOK_UP: LOOKED_UP, PONDER: PONDERED}.get(handed_off, QUICK)
            except _CutOff:
                print(f"({service.name}'s reply ran into its {service.max_tokens} tokens)")
                roomier = [s for s in ready if s.max_tokens > service.max_tokens]
                if roomier and not answer.strip() and not writing.quick_calls:
                    # Nothing said or done yet (a long note, say): the same turn, where there's room for it.
                    ready, writing.quick_service = roomier, None
                    continue
                cut_off = True
            except OpenAIError as e:
                _check(writing)  # an interrupted reply stays interrupted: closing its stream makes it fail
                self._cool_down(service, e)
                ready.remove(service)
                if answer.strip():
                    raise
                writing.quick_service = None
                if writing.quick_calls:
                    break
                continue
            finally:
                quick.close()
            if not handed_off and not answer.strip():
                print(f"({service.name} said nothing)")
                writing.quick_service = None
            break
        if not handed_off and answer.strip() and not cut_off:
            self._remember(answer, writing)
            return
        if self._client is None:
            if answer.strip():
                self._remember(answer, writing)
                return
            raise QuickFailed("no quick service answered, and there's no OpenAI to fall back on")
        if not ready:
            print("(no quick service can answer; OpenAI answers)")
        if not handed_off:
            handed_off, writing.answered_by = LOOK_UP, FALLBACK
        context.extend(writing.quick_calls)  # what Qwen's tools already did, so it isn't done twice
        if handed_off == PONDER:
            thinking = f"{' ' * bool(answer.strip() and not answer[-1].isspace())}{THINKING} "
            answer += thinking
            yield thinking
        if answer.strip():  # OpenAI carries on from what TARS already said, rather than repeating it
            context.append({"role": "assistant", "content": answer.strip()})
        if handed_off != PONDER:
            yield from super()._stream_reply(context, writing, said_first=answer)
            return
        limit = self._cfg.think_timeout_s
        timer = threading.Timer(limit, _stop, (writing, f"no answer within {limit:.0f} s"))
        timer.daemon, timer.name = True, "think-timeout"
        timer.start()
        try:
            yield from super()._stream_reply(context, writing, said_first=answer, think=True)
        finally:
            timer.cancel()

    def _quick_messages(self, context: list) -> list:
        return [{"role": "system", "content": f"{self._quick_instructions}\n\n{self._now()}"}, *context]

    def _quick_tools_for(self, round_: int) -> dict:
        tools = {"tools": self._quick_tools} if self._quick_tools else {}
        if tools and round_ == MAX_TOOL_ROUNDS - 1:
            tools["tool_choice"] = "none"  # the last round has to say something, or TARS goes silent
        return tools

    def _create(self, service: QuickService, messages: list, round_: int):
        return service.client.chat.completions.create(
            model=service.model,
            stream=True,
            messages=messages,
            max_completion_tokens=service.max_tokens,
            **self._quick_extra,
            **self._quick_tools_for(round_),
        )

    def _open(self, service: QuickService, messages: list, streams: _Streams) -> _Opened:
        """`service`'s first round, read up to its first sign of an answer."""
        stream = self._create(service, messages, 0)
        streams.add(stream)
        events, seen = iter(stream), []
        for event in events:
            seen.append(event)
            if _says_something(event):
                return stream, itertools.chain(seen, events)
        raise QuickFailed(f"{service.name} said nothing")

    def _first_to_answer(
        self, services: list[QuickService], context: list, writing: _Writing
    ) -> tuple[QuickService | None, _Opened | None, list[tuple[QuickService, Exception]]]:
        """(the service that started answering first, its opened stream, the services that failed meanwhile). The
        next service is asked too when the one before fails, or hasn't started answering within HEDGE_S; the first to
        start answering is kept, before any of its tools run, and the others are closed."""
        messages, streams = self._quick_messages(context), _Streams()
        writing.stream = streams
        pending, failed, todo = {}, [], iter(services)
        pool = ThreadPoolExecutor(len(services), thread_name_prefix="quick-open")
        try:
            while True:
                _check(writing)
                if service := next(todo, None):
                    pending[pool.submit(self._open, service, messages, streams)] = service
                if not pending:
                    return None, None, failed
                done, _ = wait(pending, timeout=HEDGE_S, return_when=FIRST_COMPLETED)
                _check(writing)
                winner = None
                for future in done:
                    service = pending.pop(future)
                    if error := future.exception():
                        failed.append((service, error))
                    elif winner is None:
                        winner = service, future.result()
                if winner:
                    service, opened = winner
                    streams.close(keep=opened[0])
                    writing.stream = opened[0]
                    return service, opened, failed
        except BaseException:
            streams.close()
            raise
        finally:
            pool.shutdown(wait=False)

    def _cool_down(self, service: QuickService, e: Exception) -> None:
        cooldown = _cooldown(e)
        service.back_at = self._clock() + cooldown
        print(f"({service.name} failed: {e!r}; skipped for the next {cooldown:.0f} s)")

    def _quick_reply(
        self, service: QuickService, context: list, writing: _Writing, opened: _Opened | None = None
    ) -> Iterator[str]:
        """Qwen's reply, streamed, with as many rounds of its tools as it needs (up to MAX_TOOL_ROUNDS). `opened`:
        its first round, already started by _first_to_answer."""
        messages, earlier = self._quick_messages(context), ""
        for round_ in range(MAX_TOOL_ROUNDS):
            _check(writing)
            if round_ == 0 and opened:
                stream, events = opened
            else:
                stream = writing.stream = self._create(service, messages, round_)
                events = iter(stream)
            said, calls, finish = "", {}, None  # calls: index -> {"id", "name", "arguments"}, from the stream
            fresh = _NoRepeat(earlier)
            with stream:
                for event in events:
                    _check(writing)
                    if not event.choices:
                        continue
                    finish = getattr(event.choices[0], "finish_reason", None) or finish
                    delta = event.choices[0].delta
                    if delta.content:
                        said += delta.content
                        if new := fresh.feed(delta.content):
                            yield new
                    for part in getattr(delta, "tool_calls", None) or []:
                        call = calls.setdefault(part.index, {"id": "", "name": "", "arguments": ""})
                        call["id"] = part.id or call["id"]
                        if part.function:
                            call["name"] = part.function.name or call["name"]
                            call["arguments"] += part.function.arguments or ""
            writing.stream = None
            earlier += said
            if finish == "length":
                raise _CutOff
            if not calls:
                return
            calls = [{**c, "id": c["id"] or f"call_{round_}_{i}"} for i, c in sorted(calls.items())]
            messages.append(
                {
                    "role": "assistant",
                    "content": said or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"]},
                        }
                        for c in calls
                    ],
                }
            )
            if moment := self._one_moment([(c["name"], c["arguments"]) for c in calls], earlier):
                earlier += moment
                yield moment
            with self._lock:
                _check(writing)
                for c in calls:
                    output = self._run_tool(types.SimpleNamespace(**c), self._no_links, writing)
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": output})
                    if output.startswith("error"):
                        continue
                    writing.quick_calls += [
                        {"type": "function_call", "call_id": c["id"], "name": c["name"], "arguments": c["arguments"]},
                        {"type": "function_call_output", "call_id": c["id"], "output": output},
                    ]

    def warm(self) -> None:
        for service in self._quick:
            try:
                service.client.models.list()
            except OpenAIError:
                pass  # only a head start
        super().warm()


class QuickFailed(OpenAIError):
    """The quick model's service failed (whichever it is), so OpenAI answers instead."""


class _CutOff(Exception):
    """The quick model's reply ran into its service's max_tokens."""


class ResponsesQuickChat(QuickChat):
    """QuickChat with an OpenAI model (Luna) as the quick model, through the Responses API: unlike Chat Completions,
    it lets the model reason and use tools in the same request. Each QuickService's client is an OpenAI client (it can
    be the same one), and service_tier applies to it as to OpenAI's other requests."""

    def __init__(
        self,
        client: OpenAI | None,
        cfg: LLMConfig,
        quick: list[QuickService],
        reminders: ReminderTools | None = None,
        clock: Callable[[], float] | None = None,
        home: HomeTools | None = None,
    ):
        super().__init__(client, cfg, quick, reminders, clock, home=home)
        effort = cfg.quick_reasoning_effort or cfg.reasoning_effort
        self._quick_extra = {"reasoning": {"effort": effort}} if effort else {}
        if cfg.service_tier:
            self._quick_extra["service_tier"] = cfg.service_tier

    def _first_to_answer(self, services: list[QuickService], context: list, writing: _Writing) -> tuple:
        return services[0], None, []

    def _quick_reply(
        self, service: QuickService, context: list, writing: _Writing, opened: _Opened | None = None
    ) -> Iterator[str]:
        items, said = list(context), ""
        for round_ in range(MAX_TOOL_ROUNDS):
            tools = {"tools": self._quick_tool_defs} if self._quick_tool_defs else {}
            if tools and round_ == MAX_TOOL_ROUNDS - 1:
                tools["tool_choice"] = "none"  # the last round has to say something
            _check(writing)
            stream = writing.stream = service.client.responses.create(
                model=service.model,
                instructions=f"{self._quick_instructions}\n\n{self._now()}",
                input=items,
                stream=True,
                store=False,
                **self._quick_extra,
                **tools,
            )
            calls = []
            for event in stream:
                _check(writing)
                if event.type == "response.output_text.delta" and event.delta:
                    said += event.delta
                    yield event.delta
                elif event.type == "response.output_item.done" and event.item.type == "function_call":
                    calls.append(event.item)
                elif event.type in ("error", "response.failed", "response.incomplete"):
                    raise QuickFailed(f"{event.type}: {_why(event)}")
            writing.stream = None
            if not calls:
                return
            if moment := self._one_moment([(c.name, c.arguments) for c in calls], said):
                said += moment
                yield moment
            with self._lock:
                _check(writing)
                for call in calls:
                    output = self._run_tool(call, self._no_links, writing)
                    done = [
                        {
                            "type": "function_call",
                            "call_id": call.call_id,
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                        {"type": "function_call_output", "call_id": call.call_id, "output": output},
                    ]
                    items += done
                    if not output.startswith("error"):
                        writing.quick_calls += done


class ClaudeQuickChat(QuickChat):
    """QuickChat with a Claude model (Haiku) as the quick model, through Anthropic's Messages API: the same tools,
    hand-offs and fallback to OpenAI. Each QuickService's client is an anthropic.Anthropic client."""

    MAX_TOKENS = 8000  # a spoken reply is short, but thinking counts too

    def __init__(
        self,
        client: OpenAI | None,
        cfg: LLMConfig,
        quick: list[QuickService],
        reminders: ReminderTools | None = None,
        clock: Callable[[], float] | None = None,
        home: HomeTools | None = None,
    ):
        super().__init__(client, cfg, quick, reminders, clock, home=home)
        self._claude_tools = [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "input_schema": t["function"]["parameters"],
                "strict": True,
            }
            for t in self._quick_tools
        ]
        effort = cfg.quick_reasoning_effort or cfg.reasoning_effort
        # Claude's thinking can't be turned off; at low effort it skips it on simple requests.
        effort = "low" if effort in ("none", "minimal") else effort
        self._quick_extra = {"output_config": {"effort": effort}} if effort else {}

    def _first_to_answer(self, services: list[QuickService], context: list, writing: _Writing) -> tuple:
        return services[0], None, []

    def _quick_reply(
        self, service: QuickService, context: list, writing: _Writing, opened: _Opened | None = None
    ) -> Iterator[str]:
        import anthropic

        # The instructions are cached; the time and the reminders change every request, so they come after.
        system = [
            {"type": "text", "text": self._quick_instructions, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": self._now()},
        ]
        messages, said = list(context), ""
        for round_ in range(MAX_TOOL_ROUNDS):
            tools = {"tools": self._claude_tools} if self._claude_tools else {}
            if tools and round_ == MAX_TOOL_ROUNDS - 1:
                tools["tool_choice"] = {"type": "none"}  # the last round has to say something
            _check(writing)
            try:
                with service.client.messages.stream(
                    model=service.model,
                    max_tokens=self.MAX_TOKENS,
                    system=system,
                    messages=messages,
                    **self._quick_extra,
                    **tools,
                ) as stream:
                    writing.stream = stream
                    for event in stream:
                        _check(writing)
                        if event.type == "text" and event.text:
                            said += event.text
                            yield event.text
                    final = stream.get_final_message()
            except anthropic.APIError as e:
                raise QuickFailed(f"Claude: {e!r}") from e
            writing.stream = None
            if final.stop_reason == "refusal":
                raise QuickFailed("Claude declined")
            uses = [b for b in final.content if b.type == "tool_use"]
            if not uses:
                return
            messages.append({"role": "assistant", "content": final.content})
            if moment := self._one_moment([(u.name, json.dumps(u.input)) for u in uses], said):
                said += moment
                yield moment
            results = []
            with self._lock:
                _check(writing)
                for use in uses:
                    arguments = json.dumps(use.input)
                    call = types.SimpleNamespace(name=use.name, arguments=arguments)
                    output = self._run_tool(call, self._no_links, writing)
                    result = {"type": "tool_result", "tool_use_id": use.id, "content": output}
                    results.append(result | ({"is_error": True} if output.startswith("error") else {}))
                    if output.startswith("error"):
                        continue
                    writing.quick_calls += [
                        {"type": "function_call", "call_id": use.id, "name": use.name, "arguments": arguments},
                        {"type": "function_call_output", "call_id": use.id, "output": output},
                    ]
            messages.append({"role": "user", "content": results})

    def warm(self) -> None:
        OpenAIChat.warm(self)  # Claude has no request cheap enough to be worth it


def _chat_tool(tool: dict) -> dict:
    """A Responses API function tool, as the Chat Completions API (Cerebras's) takes it."""
    return {"type": "function", "function": {k: v for k, v in tool.items() if k != "type"}}


class _Streams:
    """The streams _first_to_answer opens, closed together but for the one kept; any that opens after is closed."""

    def __init__(self):
        self._lock, self._open, self._done = threading.Lock(), [], False

    def add(self, stream) -> None:
        with self._lock:
            if not self._done:
                self._open.append(stream)
                return
        _close(stream, "a quick stream")

    def close(self, keep=None) -> None:
        with self._lock:
            streams, self._open, self._done = self._open, [], True
        for stream in streams:
            if stream is not keep:
                _close(stream, "a quick stream")


def _close(stream, what: str) -> None:
    try:
        stream.close()
    except Exception as e:  # noqa: BLE001 - a stream already closed is as good as closing it
        print(f"(couldn't close {what}: {e!r})")


def _says_something(event) -> bool:
    if not getattr(event, "choices", None):
        return False
    delta = event.choices[0].delta
    return bool(
        getattr(delta, "content", None)
        or getattr(delta, "tool_calls", None)
        or getattr(delta, "reasoning", None)
        or getattr(delta, "reasoning_content", None)
    )


def _check(writing: _Writing) -> None:
    if writing.stopped:
        raise ReplyFailed(writing.stopped)


def _stop(writing: _Writing, why: str) -> None:
    """Stops a reply being written, from any thread: its stream ends with ReplyFailed(why)."""
    writing.stopped = writing.stopped or why
    if (stream := writing.stream) is not None:
        _close(stream, "the reply's stream")


def _why(event) -> str:
    response = getattr(event, "response", None)
    details = getattr(response, "error", None) or getattr(response, "incomplete_details", None)
    return str(
        getattr(event, "message", None)
        or getattr(details, "message", None)
        or getattr(details, "reason", None)
        or "no details"
    )
