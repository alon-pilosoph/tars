import itertools
import json
import threading
import time
from collections.abc import Iterable, Iterator
from typing import Protocol

from openai import OpenAI, OpenAIError

from .config import LLMConfig
from .conversations import FILE, HOUSEHOLD, KINDS, LINK, LIST, NOTE, PERSON, SentItem

# The whole conversation is sent until it goes quiet for `memory_minutes`; this only stops a
# marathon session from growing the prompt forever.
MAX_TURNS = 50

# Follow-ups are heard without the wake word, so they may just be people talking to each other.
FOLLOW_UP_TAG = "[Follow-up, no wake word]"
# When the wake word sounded close but not quite, the assistant asks "Did you call me?" and tags the reply.
ASKED_TAG = "[Reply to your 'Did you call me?']"
SKIP = "<skip>"
PROTOCOL = (
    f"Messages starting with {FOLLOW_UP_TAG} were overheard right after your last reply, without anyone "
    f"addressing you. If one isn't meant for you (people talking to each other, or unrelated to your "
    f"conversation), reply with exactly {SKIP} and nothing else.\n"
    f"Messages starting with {ASKED_TAG} answer the question you just asked because you weren't sure someone "
    f"said your name. If they ask for something, just do it; if it's a bare yes, ask briefly what they need; "
    f"if it's a no, or clearly not meant for you, reply with exactly {SKIP} and nothing else."
)
SEND_PROTOCOL = (
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
        item.url, item.site, item.description = (
            url,
            _text(args.get("site")) or None,
            _text(args.get("description")) or None,
        )
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
        return None, "error: kind must be link, note, list or file"
    return item, "sent"


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


class ReplyFailed(OpenAIError):
    pass


class Brain(Protocol):
    sent: list[SentItem]  # what the last reply sent to the web UI

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


class _Writing:
    """One reply being written. Each has its own stop, so a stopped reply still waiting on the network can't
    touch the conversation after the next one has started."""

    def __init__(self):
        self.stopped = False
        self.stream = None  # the response being read, so interrupt() can close it


class OpenAIChat:
    """The conversation, through OpenAI's Responses API: streamed text, plus web search and the send tool."""

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
        prompt = cfg.system_prompt.replace("{humor}", str(cfg.humor))
        self._instructions = "\n\n".join([prompt, PROTOCOL] + ([SEND_PROTOCOL] if cfg.send else []))
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
            self._writing = writing = _Writing()
            # Set up now rather than on the first read, so an interrupt() in between isn't lost.
            return self._stream_reply(list(self._history), writing)

    def _stream_reply(self, context: list, writing: _Writing) -> Iterator[str]:
        answer = ""
        for round_ in range(MAX_TOOL_ROUNDS):
            calls, said = [], ""
            tools = {"tools": self._tools} if self._tools else {}
            if tools and round_ == MAX_TOOL_ROUNDS - 1:
                tools["tool_choice"] = "none"  # the last round has to say something, or TARS goes silent
            _check(writing)
            stream = writing.stream = self._client.responses.create(
                model=self._cfg.model,
                instructions=self._instructions,
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
