"""The brain. Qwen on Cerebras answers first (CerebrasChat), with its own tools for reminders and the TARS page when
`quick_tools` is on, and hands over the turns it can't do (the web, and whatever it has no tool for) to OpenAI's model
and its tools (OpenAIChat), and the questions that need real thinking to the thinking model. Each model is told
exactly what it can do, what it hands over, and what TARS can't do at all (abilities()). One shared history, kept
until the conversation goes quiet. Replies are streamed, and every reply can be stopped and forgotten."""

import itertools
import json
import re
import threading
import time
import types
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Protocol

from openai import OpenAI, OpenAIError

from .config import LLMConfig
from .conversations import FILE, HOUSEHOLD, KINDS, LINK, LIST, NOTE, PERSON, SentItem
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
    f"for something, like being reminded again later, just do it. If it isn't meant for you, reply with exactly "
    f"{SKIP} and nothing else."
)


def split_ack(pieces: Iterable[str]) -> tuple[list[int] | None, Iterator[str]]:
    """Peek at the start of a streamed reply: (the reminder numbers acknowledged, [] for a bare <ack>, the rest of
    the reply) if it starts with an ack, else (None, the whole reply). A bare <ack> with nothing after it says
    GOT_IT."""
    stream = iter(pieces)
    head = ""
    for piece in stream:
        head += piece
        start = head.lstrip()
        if not "<ack".startswith(start[:4]) or ">" in start or len(start) > 40:
            break
    if not (mark := ACK_MARK.match(head)):
        return None, itertools.chain([head], stream)
    ids = [int(n) for n in re.findall(r"\d+", mark.group(1))]
    return ids, _or_else(itertools.chain([head[mark.end() :]], stream), GOT_IT)


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
    'the [Speaker: name] tag; "remind Stacey" is Stacey), and a message always needs someone it\'s for. Give the '
    'time as in_minutes for "in ten minutes", or for a clock time, time as HH:MM on a 24-hour clock, with day as '
    'it was said ("friday", "tomorrow"; null if no day was said): never work out a date yourself. Morning is 9:00, '
    "afternoon 15:00, evening 19:00 and tonight 20:00, unless a time was said. Use when_back "
    "only when asked to wait until someone is back or next around. Set wait_for_ack, so it's said again until "
    "someone says they got it, unless they say once is enough; a timer always rings until someone turns it off. "
    "After setting one, confirm it in one short line with the time the tool says it's set for. To cancel one or "
    "put it off, use cancel_reminder or snooze_reminder with its number from the list of what's set now. If a tool "
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
    "when the answer is a link, a recipe, a list or anything too long to hear. Only send links you found with web "
    "search, never made-up ones. After sending, say in one short line that you sent it and that it's on the TARS "
    "page; never read a link out loud. In anything you send, write like a person: no middots (·) or em dashes."
)
# The quick model has no web search, so no real links: those turns are handed over.
QUICK_SEND_RULES = (
    "You can send things to the household's TARS page (a web app they open on their phone or laptop) with the "
    "send tool: a note, a list, or a text file. Use it when asked to send, save or share something, or when the "
    "answer is a recipe, a list or anything too long to hear. Never put a link or a web address in it: for a link, "
    "reply <look-up>. After sending, say in one short line that you sent it and that it's on the TARS page. In "
    "anything you send, write like a person: no middots (·) or em dashes."
)

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
CEREBRAS_URL = "https://api.cerebras.ai/v1"
# What the quick model replies when a turn needs OpenAI's tools; that turn is then OpenAI's to answer.
LOOK_UP = "<look-up>"
# What it replies when a question needs real thinking; the thinking model answers it. Not "<think>": Qwen 3 models
# write their own reasoning between <think> tags.
PONDER = "<ponder>"
# After Cerebras fails (down, slow, "too many requests"), OpenAI answers on its own for this long, so each turn
# doesn't wait out Cerebras's timeout first.
CEREBRAS_COOLDOWN_S = 120.0
# Who answered a reply (Brain.answered_by), kept with the turn: Qwen on Cerebras, OpenAI because Qwen handed the turn
# over, the thinking model because Qwen handed it a hard question, OpenAI because Cerebras failed or is cooling
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
            "Never say you can't do something you can hand over: hand it over. Answer everything else yourself."
        )
    parts.append(
        f"You can't {', '.join(cant)}. When asked to, say plainly in one short line that you can't do that yet, and "
        "offer what you can do instead if something fits. Never say you did something you can't do."
    )
    return "\n\n".join(parts)


def check_send(args: dict) -> tuple[SentItem | None, str]:
    """(the item to send, "sent") if the send call is complete, else (None, what's wrong) for the model to fix."""
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
    return item, "sent"


# A web address, in anything the quick model sends: it has no web search, so any link would be from memory.
WEB_ADDRESS = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|org|net|io|gov|edu|co\.uk)\b", re.IGNORECASE)


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
    changes: list[Change]  # what the last reply asked to change in the reminders, made once it's kept

    def stream_reply(self, text: str) -> Iterator[str]:
        """Yield the reply in pieces as it's generated."""

    def forget_last(self) -> None:
        """Drop the most recent exchange from the conversation history, finished or not. Once only."""

    def interrupt(self) -> None:
        """From another thread: stop writing the reply in progress; its stream ends with an error."""

    def warm(self) -> None:
        """Get a connection ready, when TARS wakes, so the reply doesn't wait to connect."""


def split_skip(pieces: Iterable[str]) -> tuple[bool, Iterator[str]]:
    """Peek at the start of a streamed reply: (True, empty) if the model chose to skip, else the full stream."""
    stream = iter(pieces)
    head = ""
    for piece in stream:
        head += piece
        start = head.lstrip()
        if len(start) >= len(SKIP) or not SKIP.startswith(start):
            break
    if head.strip().startswith(SKIP):
        for _ in stream:  # Finish the stream so the exchange is recorded before it's forgotten.
            pass
        return True, iter(())
    return False, itertools.chain([head], stream)


class _UpTo:
    """A streamed reply up to the first of `markers` (each starts with "<"), holding back only what might be the start
    of one. `found`: the marker that came, if one did."""

    def __init__(self, pieces: Iterable[str], markers: tuple[str, ...]):
        self._pieces, self._markers = pieces, markers
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
            hold = pending.rfind("<")
            if hold < 0 or not any(m.startswith(pending[hold:]) for m in self._markers):
                hold = len(pending)
            if hold:
                yield pending[:hold]
            pending = pending[hold:]
        if pending:
            yield pending


class _Writing:
    """One reply being written. Each has its own stop, so a stopped reply still waiting on the network can't
    touch the conversation after the next one has started."""

    def __init__(self, answered_by: str | None = None):
        self.stopped: str | None = None  # why, once it's stopped
        self.stream = None  # the response being read, so interrupt() can close it
        self.answered_by = answered_by
        # The quick model's tool calls and their results, as Responses API items, for a model it hands over to.
        self.quick_calls: list[dict] = []


class OpenAIChat:
    """The conversation, through OpenAI's Responses API: streamed text, plus web search and the send tool."""

    first_answerer = OPENAI

    def __init__(self, client: OpenAI, cfg: LLMConfig, reminders: ReminderTools | None = None):
        """`reminders`: TARS's timers, reminders and messages, when it has them."""
        self._client = client
        self._cfg = cfg
        self._reminders = reminders
        self._history: list[dict] = []
        self._asked: dict | None = None  # the question of the reply in progress, or the last one
        self._writing = _Writing()
        # A reply is written on the voice's thread while the next can start, or be forgotten, on another.
        self._lock = threading.Lock()
        self._last_turn_at = self._previous_turn_at = 0.0
        self.sent: list[SentItem] = []
        self.changes: list[Change] = []
        self._tools = (
            ([{"type": "web_search"}] if cfg.web_search else [])
            + ([SEND_TOOL] if cfg.send else [])
            + (REMIND_TOOLS if reminders else [])
        )
        self._system_prompt = cfg.system_prompt.replace("{humor}", str(cfg.humor))
        can = [CAN_WEB] * cfg.web_search + [CAN_SEND] * cfg.send + [CAN_REMIND] * bool(reminders)
        rules = [SKIP_RULES, *self._reminder_rules()] + [SEND_RULES] * cfg.send + [abilities(can, [], self._cant())]
        self._instructions = "\n\n".join([self._system_prompt, *rules])
        # Only send optional settings that are configured; not every model accepts them.
        self._extra = {}
        if cfg.service_tier:
            self._extra["service_tier"] = cfg.service_tier
        if cfg.reasoning_effort:
            self._extra["reasoning"] = {"effort": cfg.reasoning_effort}

    def stream_reply(self, text: str) -> Iterator[str]:
        with self._lock:
            # After a long pause, start fresh: old context confuses more than it helps, and it's private.
            if self._history and time.monotonic() - self._last_turn_at > self._cfg.memory_minutes * 60:
                self._history.clear()
            self._previous_turn_at, self._last_turn_at = self._last_turn_at, time.monotonic()
            self._asked = {"role": "user", "content": text}
            self._history.append(self._asked)
            self.sent, self.changes = [], []
            self._writing = writing = _Writing(self.first_answerer)
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
                    context.append(
                        {"type": "function_call_output", "call_id": call.call_id, "output": self._run_tool(call)}
                    )
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
            + [CANT]
        )

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

    def _remember(self, answer: str, writing: _Writing) -> None:
        with self._lock:
            _check(writing)
            self._history.append({"role": "assistant", "content": answer.strip()})
            self._history = self._history[-MAX_TURNS * 2 :]

    def _run_tool(self, call, links: bool = True) -> str:
        """`links`: whether this model can send links, which need web search."""
        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            return "error: the arguments weren't valid JSON"
        if call.name == "send" and not links and isinstance(args, dict) and _has_link(args):
            return f"error: a link needs web search, which you can't do: reply {LOOK_UP} instead"
        if call.name == "send":
            item, result = check_send(args)
            if item:
                self.sent.append(item)
            return result
        if self._reminders and call.name in ("remind", "cancel_reminder", "snooze_reminder"):
            try:
                change, result = self._reminders.call(call.name, args if isinstance(args, dict) else {})
            except Exception as e:  # noqa: BLE001 - the table couldn't be read: the model says it didn't work
                return f"error: couldn't reach the reminders ({e!r})"
            if change:
                self.changes.append(change)
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

    def interrupt(self) -> None:
        _stop(self._writing, "interrupted")

    def warm(self) -> None:
        # The cheapest request there is; the OpenAI voice reuses the same connection.
        try:
            self._client.models.retrieve(self._cfg.model)
        except OpenAIError:
            pass  # only a head start: the real request reports a failure


class CerebrasChat(OpenAIChat):
    """Answers on Cerebras (about 0.3 s to the first sentence, against about 0.7 s for OpenAI's), with its own tools
    when `quick_tools` is on, and hands a turn to OpenAI's model, tools and all, when it needs the web (or anything
    it has no tool for), to the thinking model when it needs real thinking, and to OpenAI when Cerebras fails. One
    conversation: each sees what the other said."""

    first_answerer = QUICK

    def __init__(self, client: OpenAI, cfg: LLMConfig, cerebras: OpenAI, reminders: ReminderTools | None = None):
        super().__init__(client, cfg, reminders)
        self._cerebras = cerebras
        self._cerebras_back_at = 0.0  # monotonic time; until then, Cerebras failed recently and OpenAI answers
        own_remind, own_send = cfg.quick_tools and bool(reminders), cfg.quick_tools and cfg.send
        tools = REMIND_TOOLS * own_remind + [QUICK_SEND_TOOL] * own_send
        self._quick_tool_defs = tools  # as the Responses API takes them
        self._quick_tools = [_chat_tool(t) for t in tools]
        can = [CAN_REMIND] * own_remind + [CAN_SEND_NO_LINKS] * own_send
        look_up = [NEEDS_THE_WEB] * cfg.web_search + [NEEDS_SENDING] * (cfg.send and not own_send)
        look_up += [NEEDS_REMINDING] * (bool(reminders) and not own_remind)
        examples = ["how much is a flight to Rome?"] + ["add batteries to my to-do list"] * (cfg.send and not own_send)
        hand_offs = [(LOOK_UP, LOOK_UP_INTRO, look_up, examples)] if look_up else []
        if cfg.think_effort:
            ponder = ["plan three days in Rome for us", "should we lease the car or buy it?"]
            hand_offs.append((PONDER, PONDER_INTRO, [NEEDS_THINKING], ponder))
        self._markers = tuple(marker for marker, *_ in hand_offs)
        rules = [SKIP_RULES, *self._reminder_rules(tools=own_remind)] + [QUICK_SEND_RULES] * own_send
        rules.append(abilities(can, hand_offs, self._cant()))
        self._quick_instructions = "\n\n".join([self._system_prompt, *rules])
        effort = cfg.quick_reasoning_effort or cfg.reasoning_effort
        self._quick_extra = {"reasoning_effort": effort} if effort else {}

    def _stream_reply(self, context: list, writing: _Writing) -> Iterator[str]:
        if time.monotonic() < self._cerebras_back_at:
            writing.answered_by = FALLBACK
            yield from super()._stream_reply(context, writing)
            return
        answer = ""
        quick = self._quick_reply(context, writing)
        # Usually the whole reply is the marker; now and then it comes after a sentence ("I can't check that.
        # <look-up>").
        reply = _UpTo(quick, self._markers)
        handed_off = None
        try:
            for piece in reply:
                answer += piece
                yield piece
            handed_off = reply.found
            writing.answered_by = {LOOK_UP: LOOKED_UP, PONDER: PONDERED}.get(handed_off, QUICK)
        except OpenAIError as e:  # ReplyFailed is one too: an interrupted reply stays interrupted
            if isinstance(e, ReplyFailed):
                raise
            self._cerebras_back_at = time.monotonic() + CEREBRAS_COOLDOWN_S
            if answer.strip():
                raise
            print(f"(Cerebras failed: {e!r}; OpenAI answers instead, and for the next {CEREBRAS_COOLDOWN_S:.0f} s)")
            handed_off, writing.answered_by = LOOK_UP, FALLBACK
        finally:
            quick.close()
        if not handed_off and not answer.strip():
            print("(Cerebras said nothing; OpenAI answers instead)")
            handed_off, writing.answered_by = LOOK_UP, FALLBACK
        if not handed_off:
            self._remember(answer, writing)
            return
        context.extend(writing.quick_calls)  # what Qwen's tools already did, so it isn't done twice
        if handed_off == PONDER:
            thinking = f"{' ' * bool(answer.strip())}{THINKING} "
            answer += thinking
            yield thinking
        if answer.strip():  # OpenAI carries on from what TARS already said, rather than repeating it
            context.append({"role": "assistant", "content": answer.strip()})
        if handed_off != PONDER:
            yield from super()._stream_reply(context, writing, said_first=answer)
            return
        limit = self._cfg.think_timeout_s
        timer = threading.Timer(limit, _stop, (writing, f"no answer within {limit:.0f} s"))
        timer.daemon = True
        timer.start()
        try:
            yield from super()._stream_reply(context, writing, said_first=answer, think=True)
        finally:
            timer.cancel()

    def _quick_reply(self, context: list, writing: _Writing) -> Iterator[str]:
        """Qwen's reply, streamed, with as many rounds of its tools as it needs (up to MAX_TOOL_ROUNDS)."""
        messages = [{"role": "system", "content": f"{self._quick_instructions}\n\n{self._now()}"}, *context]
        for round_ in range(MAX_TOOL_ROUNDS):
            tools = {"tools": self._quick_tools} if self._quick_tools else {}
            if tools and round_ == MAX_TOOL_ROUNDS - 1:
                tools["tool_choice"] = "none"  # the last round has to say something, or TARS goes silent
            _check(writing)
            stream = writing.stream = self._cerebras.chat.completions.create(
                model=self._cfg.cerebras_model, stream=True, messages=messages, **self._quick_extra, **tools
            )
            said, calls = "", {}  # calls: index -> {"id", "name", "arguments"}, put together from the stream
            with stream:
                for event in stream:
                    _check(writing)
                    if not event.choices:
                        continue
                    delta = event.choices[0].delta
                    if delta.content:
                        said += delta.content
                        yield delta.content
                    for part in getattr(delta, "tool_calls", None) or []:
                        call = calls.setdefault(part.index, {"id": "", "name": "", "arguments": ""})
                        call["id"] = part.id or call["id"]
                        if part.function:
                            call["name"] = part.function.name or call["name"]
                            call["arguments"] += part.function.arguments or ""
            writing.stream = None
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
            with self._lock:
                _check(writing)
                for c in calls:
                    output = self._run_tool(types.SimpleNamespace(**c), links=False)
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": output})
                    if output.startswith("error"):
                        continue
                    writing.quick_calls += [
                        {"type": "function_call", "call_id": c["id"], "name": c["name"], "arguments": c["arguments"]},
                        {"type": "function_call_output", "call_id": c["id"], "output": output},
                    ]

    def warm(self) -> None:
        try:
            self._cerebras.models.list()
        except OpenAIError:
            pass  # only a head start
        super().warm()


class QuickFailed(OpenAIError):
    """The quick model's service failed (whichever it is), so OpenAI answers instead."""


class ResponsesQuickChat(CerebrasChat):
    """CerebrasChat with an OpenAI model (Luna) as the quick model, through the Responses API: unlike Chat Completions,
    it lets the model reason and use tools in the same request. `quick` is an OpenAI client (it can be the same one);
    `cerebras_model` names the model, and service_tier applies to it as to OpenAI's other requests."""

    def __init__(self, client: OpenAI, cfg: LLMConfig, quick: OpenAI, reminders: ReminderTools | None = None):
        super().__init__(client, cfg, quick, reminders)
        effort = cfg.quick_reasoning_effort or cfg.reasoning_effort
        self._quick_extra = {"reasoning": {"effort": effort}} if effort else {}
        if cfg.service_tier:
            self._quick_extra["service_tier"] = cfg.service_tier

    def _quick_reply(self, context: list, writing: _Writing) -> Iterator[str]:
        items = list(context)
        for round_ in range(MAX_TOOL_ROUNDS):
            tools = {"tools": self._quick_tool_defs} if self._quick_tool_defs else {}
            if tools and round_ == MAX_TOOL_ROUNDS - 1:
                tools["tool_choice"] = "none"  # the last round has to say something
            _check(writing)
            stream = writing.stream = self._cerebras.responses.create(
                model=self._cfg.cerebras_model,
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
                    yield event.delta
                elif event.type == "response.output_item.done" and event.item.type == "function_call":
                    calls.append(event.item)
                elif event.type in ("error", "response.failed", "response.incomplete"):
                    raise QuickFailed(f"{event.type}: {_why(event)}")
            writing.stream = None
            if not calls:
                return
            with self._lock:
                _check(writing)
                for call in calls:
                    output = self._run_tool(call, links=False)
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


class ClaudeQuickChat(CerebrasChat):
    """CerebrasChat with a Claude model (Haiku) as the quick model, through Anthropic's Messages API: the same tools,
    hand-offs and fallback to OpenAI. `claude` is an anthropic.Anthropic client; `cerebras_model` names the model."""

    MAX_TOKENS = 8000  # a spoken reply is short, but thinking counts too

    def __init__(self, client: OpenAI, cfg: LLMConfig, claude, reminders: ReminderTools | None = None):
        super().__init__(client, cfg, claude, reminders)
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

    def _quick_reply(self, context: list, writing: _Writing) -> Iterator[str]:
        import anthropic

        # The instructions are cached; the time and the reminders change every request, so they come after.
        system = [
            {"type": "text", "text": self._quick_instructions, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": self._now()},
        ]
        messages = list(context)
        for round_ in range(MAX_TOOL_ROUNDS):
            tools = {"tools": self._claude_tools} if self._claude_tools else {}
            if tools and round_ == MAX_TOOL_ROUNDS - 1:
                tools["tool_choice"] = {"type": "none"}  # the last round has to say something
            _check(writing)
            try:
                with self._cerebras.messages.stream(
                    model=self._cfg.cerebras_model,
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
            results = []
            with self._lock:
                _check(writing)
                for use in uses:
                    arguments = json.dumps(use.input)
                    output = self._run_tool(types.SimpleNamespace(name=use.name, arguments=arguments), links=False)
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


def _check(writing: _Writing) -> None:
    if writing.stopped:
        raise ReplyFailed(writing.stopped)


def _stop(writing: _Writing, why: str) -> None:
    """Stops a reply being written, from any thread: its stream ends with ReplyFailed(why)."""
    writing.stopped = writing.stopped or why
    if (stream := writing.stream) is not None:
        try:
            stream.close()
        except Exception as e:  # noqa: BLE001 - a stream already closed is as good as closing it
            print(f"(couldn't close the reply's stream: {e!r})")


def _why(event) -> str:
    response = getattr(event, "response", None)
    details = getattr(response, "error", None) or getattr(response, "incomplete_details", None)
    return str(
        getattr(event, "message", None)
        or getattr(details, "message", None)
        or getattr(details, "reason", None)
        or "no details"
    )
