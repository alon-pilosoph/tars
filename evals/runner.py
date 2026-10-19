"""Plays a scenario through TARS's real brain (QuickChat: Qwen first, hand-offs to OpenAI's model and the thinking
model), turn by turn, the way the assistant does: the prompt tagged as it would be, a skipped turn forgotten, an <ack>
marking the reminder acknowledged, and the reminder changes a turn asked for made once it's over, so a later turn can
cancel what an earlier one set. Then each turn is checked (evals.scenarios)."""

import dataclasses
import re
import shutil
import tempfile
import threading
import time
import types
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from openai import APIConnectionError, AuthenticationError, PermissionDeniedError, RateLimitError

from voice_assistant.config import LLMConfig
from voice_assistant.events import EventLog
from voice_assistant.home import Request
from voice_assistant.llm import (
    ACK_MARK,
    ASKED_TAG,
    FOLLOW_UP_TAG,
    GOT_IT,
    QUICK_MAX_TOKENS,
    REMINDER_TAG,
    SKIP,
    QuickChat,
    QuickService,
)
from voice_assistant.reminders import ADD, VOICE, WEB, NewReminder, Reminders, ReminderTools
from voice_assistant.speech import split_sentences

from .house import House, make_house
from .scenarios import (
    ACKED,
    ROUTE_OF,
    SKIPPED,
    Scenario,
    Turn,
    change_matches,
    check_reminder_text,
    check_route,
    check_spoken,
    check_text,
    describe,
    describe_change,
    describe_row,
    did,
    pair,
    row_matches,
)

# A service refusing the account rather than the request: out of credits, a bad key, no access. Every run after it
# would fail the same way, so it isn't a result: the eval stops and says so.
ACCOUNT_CODES = {"insufficient_quota", "billing_hard_limit_reached", "invalid_api_key", "account_deactivated"}
ACCOUNT_WORDS = re.compile(
    r"no credits|insufficient[_ ]quota|exceeded your current quota|billing|payment required|invalid api key",
    re.IGNORECASE,
)


def account_problem(e: BaseException) -> str:
    """What's wrong with the account, if `e` is a service refusing it; "" if it's an ordinary failure."""
    if isinstance(e, (AuthenticationError, PermissionDeniedError)) or getattr(e, "status_code", None) == 402:
        return f"{type(e).__name__}: {e}"
    if getattr(e, "code", None) in ACCOUNT_CODES or ACCOUNT_WORDS.search(str(e)):
        return f"{type(e).__name__}: {e}"
    return ""


class Unavailable(Exception):
    """A service refused the account; the run is abandoned, not scored."""


# The voices TARS knows in every scenario: a message can wait for either.
VOICES = ["alon", "stacey"]
TAG_TEXT = {"follow-up": FOLLOW_UP_TAG, "asked": ASKED_TAG, "reminder": REMINDER_TAG}


@dataclass
class TurnResult:
    prompt: str  # what the brain was given
    raw: str  # the reply as written, hand-off included
    said: str  # what TARS would say aloud
    route: str
    wrong: list[str]
    first_s: float | None  # from the request to the first words
    total_s: float
    service: str | None = None  # the quick service that answered, if one did
    changes: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    scenario: str
    turns: list[TurnResult]
    limited: bool = False  # the quick service really said "too many requests": not a result, run it again
    usage: dict[str, dict[str, int]] = field(default_factory=dict)  # by model: requests and tokens
    unavailable: str = ""  # a service refused the account (account_problem): not a result, and the eval stops

    @property
    def counted(self) -> bool:
        return not self.limited and not self.unavailable

    @property
    def ok(self) -> bool:
        return self.counted and all(not t.wrong for t in self.turns)


class Usage:
    """Requests, tokens and web searches by model, from what the services report as each streamed response ends."""

    def __init__(self):
        self.by_model: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def _row(self, model: str) -> dict[str, int]:
        return self.by_model.setdefault(model, {"requests": 0, "input": 0, "output": 0, "reasoning": 0, "searches": 0})

    def add(self, model: str, prompt: int, completion: int, reasoning: int) -> None:
        with self._lock:
            row = self._row(model)
            row["requests"] += 1
            row["input"] += prompt
            row["output"] += completion
            row["reasoning"] += reasoning

    def searched(self, model: str) -> None:
        """A web search: billed by the search, besides its results' tokens, and the dearest thing in a run."""
        with self._lock:
            self._row(model)["searches"] += 1


class _Counted:
    """A streamed response, passed through unchanged, with its usage noted when it comes (Chat Completions: on the
    last chunk; the Responses API: on response.completed). Closing it closes the stream underneath."""

    def __init__(self, stream, usage: Usage, model: str):
        self._stream, self._usage, self._model = stream, usage, model

    def __enter__(self):
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *exc):
        return self._stream.__exit__(*exc) if hasattr(self._stream, "__exit__") else None

    def close(self):
        if hasattr(self._stream, "close"):
            self._stream.close()

    def __iter__(self):
        for event in self._stream:
            if u := getattr(event, "usage", None):  # a chat completion chunk
                details = getattr(u, "completion_tokens_details", None)
                reasoning = getattr(details, "reasoning_tokens", None) or 0
                self._usage.add(self._model, u.prompt_tokens or 0, u.completion_tokens or 0, reasoning)
            elif getattr(event, "type", "") == "response.web_search_call.completed":
                self._usage.searched(self._model)
            elif getattr(event, "type", "") == "response.completed" and (r := getattr(event, "response", None)):
                if u := getattr(r, "usage", None):
                    details = getattr(u, "output_tokens_details", None)
                    reasoning = getattr(details, "reasoning_tokens", None) or 0
                    self._usage.add(self._model, u.input_tokens, u.output_tokens, reasoning)
            yield event


class QuickClient:
    """The quick model's Chat Completions client, failing on purpose when a turn asks for it (`fail`), noting a real
    "too many requests" (which would send a turn to OpenAI for a reason that's the eval's, not TARS's), and counting
    tokens. The requests themselves are TARS's, unchanged."""

    def __init__(self, client, usage: Usage):
        self._client, self._usage, self.fail, self.limited, self.unavailable = client, usage, "", False, ""
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kw):
        request = httpx.Request("POST", "https://quick.invalid/chat/completions")
        if self.fail == "down":
            raise APIConnectionError(request=request)
        if self.fail == "limited":
            response = httpx.Response(429, request=request)
            raise RateLimitError("too many requests (on purpose)", response=response, body=None)
        try:
            return _Counted(self._client.chat.completions.create(**kw), self._usage, kw.get("model", "quick"))
        except RateLimitError:
            self.limited = True
            raise
        except Exception as e:  # the brain falls back to OpenAI on any failure: note why, if it's the account
            self.unavailable = self.unavailable or account_problem(e)
            raise


class OpenAIClient:
    """OpenAI's client as the brain uses it (the Responses API), counting tokens."""

    def __init__(self, client, usage: Usage):
        self._client, self._usage = client, usage
        self.responses = types.SimpleNamespace(create=self._create)

    def _create(self, **kw):
        return _Counted(self._client.responses.create(**kw), self._usage, kw.get("model", "openai"))


class Clock:
    """time.monotonic, plus however long the scenario says passed between turns (Turn.after_minutes)."""

    def __init__(self):
        self.skipped = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.skipped


def prompt(turn: Turn) -> str:
    """As the assistant builds it: the speaker, then the tag."""
    text = f"[Speaker: {turn.speaker}] {turn.say}"
    return f"{TAG_TEXT[turn.tag]} {text}" if turn.tag else text


def play(
    scenario: Scenario,
    llm: LLMConfig,
    quick_client,
    model: str,
    openai_client,
    services: Sequence[tuple[str, int]] = (("Cerebras", QUICK_MAX_TOKENS),),
) -> RunResult:
    """One run of `scenario`, with Qwen (`model`) on `quick_client` and OpenAI on `openai_client`, against reminders
    made for the run. `services`: the quick services TARS asks, in order, as (name, reply cap), all served here by
    `quick_client`; a failure a turn asks for takes them all down."""
    folder = Path(tempfile.mkdtemp(prefix="tars-eval-"))
    try:
        reminders = Reminders(EventLog(folder / "events").store)
        tools = ReminderTools(reminders, voices=lambda: VOICES)
        refs: dict[str, int] = {}
        for s in scenario.seeds:
            new = NewReminder(s.kind, s.text, s.for_name, due=time.time() + s.in_minutes * 60)
            refs[s.ref] = reminders.add(new, WEB, voices=VOICES)
            if s.said:
                reminders.said(refs[s.ref])
        usage, clock = Usage(), Clock()
        quick = QuickClient(quick_client, usage)
        openai = OpenAIClient(openai_client, usage) if openai_client is not None else None
        quick_services = [QuickService(name, quick, model, cap) for name, cap in services]
        house = make_house(scenario.house) if scenario.house is not None else None
        home = house.tools if house else None
        brain = QuickChat(openai, llm, quick_services, reminders=tools, clock=clock, home=home)
        turns, unavailable = [], ""
        for turn in scenario.turns:
            clock.skipped += turn.after_minutes * 60
            try:
                turns.append(_play_turn(brain, quick, tools, turn, refs, house))
            except Unavailable as e:
                unavailable = str(e)
            unavailable = unavailable or quick.unavailable
            if quick.limited or unavailable:
                break
        return RunResult(scenario.id, turns, quick.limited, usage.by_model, unavailable)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _play_turn(
    brain: QuickChat, quick: QuickClient, tools: ReminderTools, turn: Turn, refs: dict, house: House | None = None
) -> TurnResult:
    quick.fail, asked = turn.quick, prompt(turn)
    asked_at, start, first, said = time.time(), time.monotonic(), None, ""
    known = turn.speaker != "unknown"
    # Heard by voice, as at home; a voice speaker ID knows is named surely, an unknown one isn't named at all.
    request = Request(turn.speaker if known else None, 0.9 if known else None, by_voice=True, heard=turn.say)
    before = len(house.changes) if house else 0
    try:
        for piece in brain.stream_reply(asked, request):
            if first is None and piece.strip():
                first = time.monotonic() - start
            said += piece
    except Exception as e:  # a failed turn is a result, unless the account was refused
        if problem := account_problem(e):
            raise Unavailable(problem) from e
        brain.forget_last()  # as the assistant does with a question that got no answer
        why = f"failed: {type(e).__name__}: {e}"
        return TurnResult(asked, said, said.strip(), "failed", [why], first, time.monotonic() - start)
    finally:
        quick.fail = ""
    total = time.monotonic() - start

    route, spoken, acks = _heard(said, brain.answered_by)
    wrong = [w for w in [check_route(turn, route)] if w] + check_spoken(spoken, route, said) + check_text(turn, spoken)
    changes = list(brain.changes)
    wrong += check_reminder_text(changes)
    if turn.reminders is not None:
        paired, missing, extra = pair(turn.reminders, changes, lambda e, c: change_matches(e, c, refs))
        wrong += [f"didn't {'add ' * (e.action == ADD)}{describe(e)}" for e in missing]
        wrong += [f"also did: {describe_change(c)}" for c in extra]
    else:
        paired = {}
    if turn.sent is not None:
        _, missing, extra = pair(
            turn.sent,
            brain.sent,
            lambda e, s: s.kind in e.kind and (e.entries is None or len(s.entries or []) == e.entries),
        )
        wrong += [f"didn't send a {' or '.join(e.kind)}" for e in missing]
        wrong += [f"also sent a {s.kind}: {s.title!r}" for s in extra]
    home_changes = house.changed_since(before) if house else []
    if turn.home is not None:
        _, missing, extra = pair(turn.home, home_changes, lambda e, c: e.matches(c))
        wrong += [f"didn't {e.said()}" for e in missing]
        wrong += [f"also did: {did(c['action'])} the {c['device']} ({c['result']})" for c in extra]

    if route == SKIPPED:
        brain.forget_last()  # overheard: nothing kept, nothing done
    else:
        wrong += _keep(tools, turn, changes, paired, acks, refs)
        rows = tools.reminders.active()
        slack = total + 5.0
        if turn.active is not None:
            _, missing, extra = pair(turn.active, rows, lambda e, r: row_matches(e, r, asked_at, slack))
            wrong += [f"should be set after it: {describe(e)}" for e in missing]
            wrong += [f"also set after it: {describe_row(r)}" for r in extra]
        _, missing, _ = pair(turn.active_has, rows, lambda e, r: row_matches(e, r, asked_at, slack))
        wrong += [f"should be set after it: {describe(e)}" for e in missing]
    return TurnResult(
        asked,
        said,
        spoken.strip(),
        route,
        wrong,
        first,
        total,
        brain.quick_service,
        [describe_change(c) for c in changes] + [f"home: {did(c['action'])} the {c['device']}" for c in home_changes],
        [f"{s.kind}: {s.title}" for s in brain.sent],
    )


def _heard(said: str, answered_by: str | None) -> tuple[str, str, list[int] | None]:
    """(where the turn went, what TARS says aloud, the reminders an <ack> acknowledged), as the assistant reads a
    reply: a <skip> says nothing, an <ack> is taken off what's said (a bare one says "Got it."), and what's left is
    cleaned for the voice (sources cited, links and markdown symbols aren't read out)."""
    if said.lstrip().startswith(SKIP):
        return SKIPPED, "", None
    route, acks = ROUTE_OF.get(answered_by or "", str(answered_by)), None
    if m := ACK_MARK.match(said):
        route, acks, said = ACKED, [int(n) for n in m.group(1).replace(",", " ").split()], said[m.end() :]
        said = said.strip() or GOT_IT
    return route, " ".join(split_sentences([said])), acks


def _keep(tools: ReminderTools, turn: Turn, changes: list, paired: dict, acks, refs: dict) -> list[str]:
    """Makes what the kept turn asked for, as the assistant does; an added reminder's number goes to its ref."""
    wrong, ref_of = [], {j: turn.reminders[i].ref for i, j in paired.items() if turn.reminders[i].ref}
    reminders, who = tools.reminders, None if turn.speaker == "unknown" else turn.speaker
    if acks is not None:  # not the ones the same reply put off or cancelled, as the assistant does
        changed = {c.id for c in changes if c.id is not None}
        for i in acks or [r["id"] for r in reminders.waiting()[:1]]:
            if i not in changed:
                reminders.ack(i, who, VOICE)
    for j, change in enumerate(changes):
        try:
            if change.action == ADD:
                new = dataclasses.replace(change.reminder, from_name=who)
                rid = reminders.add(new, VOICE, voices=VOICES, now=change.asked_at or None)
                if j in ref_of:
                    refs[ref_of[j]] = rid
            else:
                tools.apply(change, from_name=who)
        except Exception as e:  # noqa: BLE001 - checked when the model asked; reported, not fatal
            wrong.append(f"couldn't make the change {describe_change(change)}: {e!r}")
    return wrong
