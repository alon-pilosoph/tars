"""What TARS used of the paid services (docs/usage.md): a row for each call to a model, to speech to text or to a
voice, with who it was for and how they asked, and the cost worked out when it's read, from prices.toml, so a price
changed never means rewriting what was kept.

Each process installs one Meter at its start (install), and the code that calls a provider reports what the call used
through this module's own functions (llm, stt, tts), which do nothing without one: the evals and the tests run with
none. Who a call was for comes from the turn it's part of, set by the code that starts the turn (turn), not from the
provider's code, which knows nothing of people. A turn is one record shared by every thread it starts with its
context (carried): naming the speaker partway through (identified) reaches a draft already running on its own thread.
A call on a thread started without it is counted as nobody's, in the background.

Recording never costs a reply: rows go through a queue to one writer thread, and one that can't be written is said
and dropped. A call the provider said nothing about (cut off part-way) is counted with zeros, marked as such, so the
gap shows."""

import contextvars
import queue
import threading
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .events import person_key

LLM, STT, TTS = "llm", "stt", "tts"
VOICE, TELEGRAM, WEB, ERRAND, SKILL, BACKGROUND = "voice", "telegram", "web", "errand", "skill", "background"
PRICES = Path(__file__).with_name("prices.toml")
KEEP_DAYS = 400  # a little over a year: this month against the same one last year
COLUMNS = ("input", "output", "reasoning", "cached", "searches", "seconds", "characters")


@dataclass
class _Turn:
    person: str
    channel: str


_turn: contextvars.ContextVar[_Turn | None] = contextvars.ContextVar("usage_turn", default=None)
_meter: Meter | None = None


@contextmanager
def turn(person: str | None, channel: str) -> Iterator[None]:
    """What the calls made inside are counted for: `person` (as TARS knows them; None or "" when it doesn't, which
    identified can say later), asking by `channel` (VOICE, TELEGRAM, WEB, ERRAND, SKILL)."""
    token = _turn.set(_Turn(person_key(person) if person else "", channel))
    try:
        yield
    finally:
        _turn.reset(token)


def identified(person: str | None) -> None:
    """Who the turn under way is for, once it's known (the voice knows the speaker only once they've spoken)."""
    if (now := _turn.get()) is not None:
        now.person = person_key(person) if person else ""


def carried(target: Callable) -> Callable:
    """`target`, to run on another thread as part of the turn under way (a draft, a sentence spoken)."""
    context = contextvars.copy_context()
    return lambda *args, **kwargs: context.run(target, *args, **kwargs)


def install(meter: Meter | None) -> None:
    """This process's meter, which the functions below report to; None: nothing is counted."""
    global _meter
    _meter = meter


def llm(
    service: str,
    model: str,
    input: int = 0,
    output: int = 0,
    reasoning: int = 0,
    cached: int = 0,
    searches: int = 0,
    reported: bool = True,
) -> None:
    """A call to a model: its tokens (`reasoning` of the output, `cached` of the input), and the web searches it made.
    `reported`: False when the provider said nothing of what it used."""
    if _meter:
        _meter.add(LLM, service, model, input=input, output=output, reasoning=reasoning, cached=cached,
                   searches=searches, reported=reported)  # fmt: skip


def stt(service: str, model: str, seconds: float, reported: bool = True) -> None:
    """Speech written down: `seconds` of audio."""
    if _meter:
        _meter.add(STT, service, model, seconds=seconds, reported=reported)


def tts(service: str, model: str, characters: int, reported: bool = True) -> None:
    """A voice speaking `characters` of text."""
    if _meter:
        _meter.add(TTS, service, model, characters=characters, reported=reported)


class Meter:
    """Writes what's reported to the `usage` table of `store`, on a thread of its own."""

    def __init__(self, store, clock: Callable[[], float] = time.time):
        self.store, self._clock = store, clock
        self._queue: queue.Queue[dict | None] = queue.Queue()
        self._writer = threading.Thread(target=self._write, daemon=True, name="usage")
        self._writer.start()

    def add(self, kind: str, service: str, model: str, reported: bool = True, **used: float) -> None:
        now = _turn.get()
        person, channel = (now.person, now.channel) if now else ("", BACKGROUND)
        row = {"at": self._clock(), "service": service, "kind": kind, "model": model, "person": person,
               "channel": channel, "reported": int(reported), **used}  # fmt: skip
        self._queue.put(row)

    def flush(self) -> None:
        """Waits for what's been reported so far to be written."""
        self._queue.join()

    def _write(self) -> None:
        while True:
            row = self._queue.get()
            try:
                if row is not None:
                    names = ", ".join(row)
                    marks = ", ".join("?" * len(row))
                    self.store.write(f"INSERT INTO usage ({names}) VALUES ({marks})", tuple(row.values()))  # nosec B608  # our own column names
            except Exception as e:  # noqa: BLE001 - a row lost is said, and never stops a reply
                print(f"(usage: couldn't keep what a call used: {e!r})")
            finally:
                self._queue.task_done()

    def prune(self, keep_days: float = KEEP_DAYS, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        with self.store.transaction() as db:
            return db.execute("DELETE FROM usage WHERE at < ?", (now - keep_days * 86400,)).rowcount


# Reading it back


@dataclass
class Prices:
    """What prices.toml says each thing costs: per million tokens (models), per web search, per minute of speech heard
    (speech, by model) and per million characters spoken (voices, by the start of the voice's name)."""

    models: dict[str, dict[str, float]] = field(default_factory=dict)
    search: float = 0.0
    speech: dict[str, float] = field(default_factory=dict)
    voices: dict[str, float] = field(default_factory=dict)
    checked: str = "?"

    def of(self, row: dict) -> float | None:
        """What a row's use cost, in dollars; None if its model or voice has no price (not free: not known)."""
        if row["kind"] == LLM:
            p = self.models.get(row["model"])
            if not p:
                return None
            fresh = row["input"] - row["cached"]
            cached = row["cached"] * p.get("cache_read", p.get("input", 0.0))
            tokens = fresh * p.get("input", 0.0) + cached + row["output"] * p.get("output", 0.0)
            return tokens / 1e6 + row["searches"] * self.search
        if row["kind"] == STT:
            per_minute = self.speech.get(row["model"])
            return None if per_minute is None else row["seconds"] / 60 * per_minute
        name = max((v for v in self.voices if row["model"].startswith(v)), key=len, default=None)
        return None if name is None else row["characters"] / 1e6 * self.voices[name]


def load_prices(path: Path = PRICES) -> Prices:
    data = tomllib.loads(path.read_text())
    return Prices(
        models=data.get("models", {}),
        search=float(data.get("searches", {}).get("each", 0.0)),
        speech={m: float(p["per_minute"]) for m, p in data.get("speech", {}).items()},
        voices={v: float(p["per_million_characters"]) for v, p in data.get("voices", {}).items()},
        checked=data.get("checked", "?"),
    )


def month_bounds(month: str | None = None, now: float | None = None) -> tuple[float, float, str]:
    """(start, end, "2026-11") of `month` ("YYYY-MM"; None: this one), in this machine's time."""
    if month:
        start = datetime.strptime(month, "%Y-%m").astimezone()  # its first day, here
    else:
        today = datetime.fromtimestamp(time.time() if now is None else now).astimezone()
        start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
    return start.timestamp(), end.timestamp(), start.strftime("%Y-%m")


@dataclass
class Line:
    """What one thing (a service and model, or a person) used in the month, and what it cost as far as it's priced."""

    name: str
    calls: int = 0
    used: dict[str, float] = field(default_factory=lambda: dict.fromkeys(COLUMNS, 0))
    cost: float = 0.0
    unpriced: int = 0  # calls with no price: their cost isn't in `cost`
    unreported: int = 0  # calls the provider said nothing about


@dataclass
class Month:
    month: str
    by_service: list[Line]
    by_person: list[Line]
    cost: float
    unpriced: int
    unreported: int
    checked: str  # the date prices.toml's prices were checked


def month(store, month_name: str | None = None, prices: Prices | None = None, now: float | None = None) -> Month:
    """What was used in `month_name` ("YYYY-MM"; None: this month), by service and model, and by person."""
    prices = prices or load_prices()
    start, end, name = month_bounds(month_name, now)
    rows = store.rows("SELECT * FROM usage WHERE at >= ? AND at < ?", (start, end))
    services: dict[str, Line] = {}
    people: dict[str, Line] = {}
    total = unpriced = unreported = 0
    for row in rows:
        cost = prices.of(row)
        for line in (
            services.setdefault(f"{row['service']} {row['model']}", Line(f"{row['service']} {row['model']}")),
            people.setdefault(row["person"] or "", Line(row["person"] or "")),
        ):
            line.calls += 1
            for column in COLUMNS:
                line.used[column] += row[column]
            line.cost += cost or 0.0
            line.unpriced += cost is None
            line.unreported += not row["reported"]
        total += cost or 0.0
        unpriced += cost is None
        unreported += not row["reported"]
    by_cost = sorted(services.values(), key=lambda line: (-line.cost, line.name))
    return Month(name, by_cost, sorted(people.values(), key=lambda line: -line.cost), total, unpriced, unreported,
                 prices.checked)  # fmt: skip


def report(m: Month) -> str:
    """`m` as the terminal shows it (--usage)."""
    lines = [f"What TARS used in {m.month} (prices checked {m.checked}; estimates, not a bill):", ""]
    if not m.by_service:
        return lines[0] + "\n\nNothing yet."
    for line in m.by_service:
        lines.append(f"  {line.name:<40} {line.calls:>6} calls  {_used(line):<34} {_dollars(line)}")
    lines += ["", "  By person:"]
    for line in m.by_person:
        lines.append(f"  {line.name or '(nobody known)':<40} {line.calls:>6} calls  {'':<34} {_dollars(line)}")
    lines += ["", f"  In all: ${m.cost:.2f}"]
    if m.unpriced:
        lines.append(f"  {m.unpriced} calls to something with no price in prices.toml aren't in that.")
    if m.unreported:
        lines.append(
            f"  {m.unreported} calls were cut off before the provider said what they used: counted as nothing."
        )
    return "\n".join(lines)


def _used(line: Line) -> str:
    u = line.used
    parts = []
    if u["input"] or u["output"]:
        parts.append(f"{u['input'] / 1000:,.0f}k in, {u['output'] / 1000:,.0f}k out")
    if u["searches"]:
        parts.append(f"{u['searches']:.0f} searches")
    if u["seconds"]:
        parts.append(f"{u['seconds'] / 60:,.1f} min heard")
    if u["characters"]:
        parts.append(f"{u['characters'] / 1000:,.1f}k characters")
    return ", ".join(parts)


def _dollars(line: Line) -> str:
    said = f"${line.cost:,.2f}"
    return said + (" + not priced" if line.unpriced else "")


# The providers' streams, counted as they're read


class _Counted:
    """A provider's stream, passed through as it is, recording what it used once it ends or is closed: what the
    provider said, or zeros marked unreported if it said nothing (closed early, cut off)."""

    def __init__(self, stream, service: str, model: str):
        self._stream, self._service, self._model = stream, service, model
        self._used: dict | None = None
        self._done = False

    def __iter__(self):
        try:
            for event in self._stream:
                self._read(event)
                yield event
        finally:
            self._record()

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self._record()

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            return self._stream.__exit__(*exc)
        finally:
            self._record()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)

    def _read(self, event) -> None:
        raise NotImplementedError

    def _record(self) -> None:
        if self._done:
            return
        self._done = True
        if self._used:
            llm(self._service, self._model, **self._used)
        else:
            llm(self._service, self._model, reported=False)


class CountedChat(_Counted):
    """A chat completions stream (Cerebras, Groq): the usage comes on the last chunk, as `usage` or Groq's
    `x_groq.usage`, where the provider sends it unasked."""

    def _read(self, chunk) -> None:
        used = getattr(chunk, "usage", None) or getattr(getattr(chunk, "x_groq", None), "usage", None)
        if used:
            self._used = {
                "input": getattr(used, "prompt_tokens", 0) or 0,
                "output": getattr(used, "completion_tokens", 0) or 0,
                "reasoning": getattr(getattr(used, "completion_tokens_details", None), "reasoning_tokens", 0) or 0,
                "cached": getattr(getattr(used, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
            }


class CountedResponses(_Counted):
    """A Responses API stream (OpenAI): the usage, and the web searches made, come with the response's last event."""

    def _read(self, event) -> None:
        if event.type not in ("response.completed", "response.incomplete", "response.failed"):
            return
        response = getattr(event, "response", None)
        used = getattr(response, "usage", None)
        searches = sum(getattr(item, "type", "") == "web_search_call" for item in getattr(response, "output", []) or [])
        if used:
            self._used = {
                "input": getattr(used, "input_tokens", 0) or 0,
                "output": getattr(used, "output_tokens", 0) or 0,
                "reasoning": getattr(getattr(used, "output_tokens_details", None), "reasoning_tokens", 0) or 0,
                "cached": getattr(getattr(used, "input_tokens_details", None), "cached_tokens", 0) or 0,
                "searches": searches,
            }


def claude_message(service: str, model: str, message) -> None:
    """A finished Claude message's usage: its input in all (fresh, read from the cache and written to it) and what of
    it came from the cache."""
    used = getattr(message, "usage", None)
    if used is None:
        llm(service, model, reported=False)
        return
    read = getattr(used, "cache_read_input_tokens", 0) or 0
    written = getattr(used, "cache_creation_input_tokens", 0) or 0
    llm(service, model, input=(used.input_tokens or 0) + read + written, output=used.output_tokens or 0, cached=read)
