"""The brain. Qwen on Cerebras answers first (CerebrasChat) and hands the turns it can't do (the web, the TARS page)
to OpenAI's model and its tools (OpenAIChat). One shared history, kept until the conversation goes quiet. Replies are
streamed, and every reply can be stopped and forgotten."""

import itertools
import json
import threading
import time
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Protocol

from openai import OpenAI, OpenAIError

from .config import LLMConfig
from .conversations import FILE, HOUSEHOLD, KINDS, LINK, LIST, NOTE, PERSON, SentItem

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
# TARS can only talk, search the web and send to the TARS page. Without this, a model asked for a timer says "Twelve
# minutes, starting now." and nothing ever goes off.
CANT_RULES = (
    "You can only talk, look things up, and send things to the TARS page. You can't set timers, alarms or "
    "reminders, play music or sounds, call or message anyone, or control anything in the house. When asked to, say "
    "plainly in one short line that you can't do that yet, and offer what you can do instead if something fits "
    "(for example, sending a note). Never say you did something you can't do."
)
SEND_RULES = (
    "You can send things to the household's TARS page (a web app they open on their phone or laptop) with the "
    "send tool: a link, a note, a list, or a text file. Use it when asked to send, save or share something, or "
    "when the answer is a link, a recipe, a list or anything too long to hear. Only send links you found with web "
    "search, never made-up ones. After sending, say in one short line that you sent it and that it's on the TARS "
    "page; never read a link out loud. In anything you send, write like a person: no middots (·) or em dashes."
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
# A web search takes several seconds: say something the moment one starts, instead of going silent.
SEARCHING = "Looking it up."
FILE_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv", ".ics": "text/calendar"}
MAX_TOOL_ROUNDS = 3
CEREBRAS_URL = "https://api.cerebras.ai/v1"
# What the quick model replies when a turn needs OpenAI's tools; that turn is then OpenAI's to answer.
LOOK_UP = "<look-up>"
# After Cerebras fails (down, slow, "too many requests"), OpenAI answers on its own for this long, so each turn
# doesn't wait out Cerebras's timeout first.
CEREBRAS_COOLDOWN_S = 120.0
# Who answered a reply (Brain.answered_by), kept with the turn: Qwen on Cerebras, OpenAI because Qwen handed the turn
# over, OpenAI because Cerebras failed or is cooling down, or OpenAI as the only brain.
QUICK, LOOKED_UP, FALLBACK, OPENAI = "quick", "look_up", "fallback", "openai"
NEEDS_THE_WEB = "anything current or live: the weather, news, sports results, prices or opening hours, or a real link"
NEEDS_SENDING = (
    "sending, saving or sharing something to the household's TARS page: a recipe, a list, a note, a link "
    "or a file, or any answer too long to hear"
)


def local_time() -> str:
    """The date and time here, which no model knows. Goes last in the instructions, so the rest can be cached."""
    now = datetime.now().astimezone()
    clock = f"{now:%I:%M %p}".lstrip("0")
    return f"It's {now:%A, %B} {now.day}, {now.year}, {clock} here ({now.tzname()}, UTC{now:%:z})."


def look_up_rules(web_search: bool, send: bool) -> str:
    needs = [need for need, can in [(NEEDS_THE_WEB, web_search), (NEEDS_SENDING, send)] if can]
    return (
        f"You can't do these yourself, but another model can. When a message needs any of them, reply with "
        f"exactly {LOOK_UP} and nothing else, and it answers instead:\n"
        + "\n".join(f"- {n}" for n in needs)
        + f"\nNever say you can't do one of these: reply {LOOK_UP}. For example, \"add batteries to my to-do "
        f'list" or "how much is a flight to Rome?" get {LOOK_UP}. Answer everything else yourself.'
    )


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


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


class ReplyFailed(OpenAIError):
    pass


class Brain(Protocol):
    sent: list[SentItem]  # what the last reply sent to the web UI
    answered_by: str | None  # who wrote the last reply (QUICK, LOOKED_UP, FALLBACK or OPENAI), once known

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
    """A streamed reply up to `marker`, holding back only what might be the start of it. `found`: whether it came."""

    def __init__(self, pieces: Iterable[str], marker: str):
        self._pieces, self._marker = pieces, marker
        self.found = False

    def __iter__(self) -> Iterator[str]:
        pending = ""
        for piece in self._pieces:
            pending += piece
            if (at := pending.find(self._marker)) >= 0:
                self.found = True
                if at:
                    yield pending[:at]
                return
            hold = pending.rfind(self._marker[0])
            if hold < 0 or not self._marker.startswith(pending[hold:]):
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
        self.stopped = False
        self.stream = None  # the response being read, so interrupt() can close it
        self.answered_by = answered_by


class OpenAIChat:
    """The conversation, through OpenAI's Responses API: streamed text, plus web search and the send tool."""

    first_answerer = OPENAI

    def __init__(self, client: OpenAI, cfg: LLMConfig):
        self._client = client
        self._cfg = cfg
        self._history: list[dict] = []
        self._asked: dict | None = None  # the question of the reply in progress, or the last one
        self._writing = _Writing()
        # A reply is written on the voice's thread while the next can start, or be forgotten, on another.
        self._lock = threading.Lock()
        self._last_turn_at = self._previous_turn_at = 0.0
        self.sent: list[SentItem] = []
        self._tools = ([{"type": "web_search"}] if cfg.web_search else []) + ([SEND_TOOL] if cfg.send else [])
        self._system_prompt = cfg.system_prompt.replace("{humor}", str(cfg.humor))
        rules = [SKIP_RULES, CANT_RULES] + ([SEND_RULES] if cfg.send else [])
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
            self.sent = []
            self._writing = writing = _Writing(self.first_answerer)
            # Set up now rather than on the first read, so an interrupt() in between isn't lost.
            return self._stream_reply(list(self._history), writing)

    def _stream_reply(self, context: list, writing: _Writing, said_first: str = "") -> Iterator[str]:
        """`said_first`: what TARS already said of this reply (a hand-off's first sentence), for the history."""
        answer = said_first
        for round_ in range(MAX_TOOL_ROUNDS):
            calls, said = [], ""
            tools = {"tools": self._tools} if self._tools else {}
            if tools and round_ == MAX_TOOL_ROUNDS - 1:
                tools["tool_choice"] = "none"  # the last round has to say something, or TARS goes silent
            _check(writing)
            stream = writing.stream = self._client.responses.create(
                model=self._cfg.model,
                instructions=f"{self._instructions}\n\n{local_time()}",
                input=context,
                stream=True,
                store=False,  # nothing kept on OpenAI's side beyond the request itself
                **self._extra,
                **tools,
            )
            for event in stream:
                _check(writing)
                if event.type == "response.output_text.delta" and event.delta:
                    said += event.delta
                    yield event.delta
                elif event.type == "response.web_search_call.in_progress" and not (answer or said):
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

    @property
    def answered_by(self) -> str | None:
        return self._writing.answered_by

    def _remember(self, answer: str, writing: _Writing) -> None:
        with self._lock:
            _check(writing)
            self._history.append({"role": "assistant", "content": answer.strip()})
            self._history = self._history[-MAX_TURNS * 2 :]

    def _run_tool(self, call) -> str:
        if call.name != "send":
            return f"error: no tool called {call.name}"
        try:
            item, result = check_send(json.loads(call.arguments or "{}"))
        except json.JSONDecodeError:
            return "error: the arguments weren't valid JSON"
        if item:
            self.sent.append(item)
        return result

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
            self.sent = []

    def interrupt(self) -> None:
        writing = self._writing
        writing.stopped = True
        if (stream := writing.stream) is not None:
            try:
                stream.close()
            except Exception as e:  # noqa: BLE001 - a stream already closed is as good as closing it
                print(f"(couldn't close the reply's stream: {e!r})")

    def warm(self) -> None:
        # The cheapest request there is; the OpenAI voice reuses the same connection.
        try:
            self._client.models.retrieve(self._cfg.model)
        except OpenAIError:
            pass  # only a head start: the real request reports a failure


class CerebrasChat(OpenAIChat):
    """Answers on Cerebras (about 0.3 s to the first sentence, against about 0.7 s for OpenAI's), and hands a turn to
    OpenAI's model, tools and all, when it needs the web or the TARS page, or when Cerebras fails. One conversation:
    each sees what the other said."""

    first_answerer = QUICK

    def __init__(self, client: OpenAI, cfg: LLMConfig, cerebras: OpenAI):
        super().__init__(client, cfg)
        self._cerebras = cerebras
        self._cerebras_back_at = 0.0  # monotonic time; until then, Cerebras failed recently and OpenAI answers
        hand_off = [look_up_rules(cfg.web_search, cfg.send)] if cfg.web_search or cfg.send else []
        self._quick_instructions = "\n\n".join([self._system_prompt, SKIP_RULES, CANT_RULES, *hand_off])
        self._quick_extra = {"reasoning_effort": cfg.reasoning_effort} if cfg.reasoning_effort else {}

    def _stream_reply(self, context: list, writing: _Writing) -> Iterator[str]:
        if time.monotonic() < self._cerebras_back_at:
            writing.answered_by = FALLBACK
            yield from super()._stream_reply(context, writing)
            return
        answer = ""
        quick = self._quick_reply(context, writing)
        # Usually the whole reply is the marker; now and then it comes after a sentence ("I can't check that.
        # <look-up>").
        reply = _UpTo(quick, LOOK_UP)
        handed_off = False
        try:
            for piece in reply:
                answer += piece
                yield piece
            handed_off = reply.found
            writing.answered_by = LOOKED_UP if handed_off else QUICK
        except OpenAIError as e:  # ReplyFailed is one too: an interrupted reply stays interrupted
            if isinstance(e, ReplyFailed):
                raise
            self._cerebras_back_at = time.monotonic() + CEREBRAS_COOLDOWN_S
            if answer.strip():
                raise
            print(f"(Cerebras failed: {e!r}; OpenAI answers instead, and for the next {CEREBRAS_COOLDOWN_S:.0f} s)")
            handed_off, writing.answered_by = True, FALLBACK
        finally:
            quick.close()
        if not handed_off and not answer.strip():
            print("(Cerebras said nothing; OpenAI answers instead)")
            handed_off, writing.answered_by = True, FALLBACK
        if not handed_off:
            self._remember(answer, writing)
            return
        if answer.strip():  # OpenAI carries on from what TARS already said, rather than repeating it
            context.append({"role": "assistant", "content": answer.strip()})
        yield from super()._stream_reply(context, writing, said_first=answer)

    def _quick_reply(self, context: list, writing: _Writing) -> Iterator[str]:
        _check(writing)
        stream = writing.stream = self._cerebras.chat.completions.create(
            model=self._cfg.cerebras_model,
            stream=True,
            messages=[{"role": "system", "content": f"{self._quick_instructions}\n\n{local_time()}"}, *context],
            **self._quick_extra,
        )
        with stream:
            for event in stream:
                _check(writing)
                if event.choices and (delta := event.choices[0].delta.content):
                    yield delta
        writing.stream = None

    def warm(self) -> None:
        try:
            self._cerebras.models.list()
        except OpenAIError:
            pass  # only a head start
        super().warm()


def _check(writing: _Writing) -> None:
    if writing.stopped:
        raise ReplyFailed("interrupted")


def _why(event) -> str:
    response = getattr(event, "response", None)
    details = getattr(response, "error", None) or getattr(response, "incomplete_details", None)
    return str(
        getattr(event, "message", None)
        or getattr(details, "message", None)
        or getattr(details, "reason", None)
        or "no details"
    )
