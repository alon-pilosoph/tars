"""The brain's tools, whoever provides them: one Toolbox each model is offered its tools from, and every call to one
goes through, so the brain (llm.py) names none of them. A provider (the house, the browser's errands, the feeds, the
skills, TARS itself, and the brain's own send and reminder tools) hands the brain a Toolset: its tools, its rules for
the instructions, and, for abilities(), what they let a model do (`can`) and what a model without them hands over
(`needs`). A plugin's own tools are the house's, each a Tool like any other.

Every tool says what calling it may change (`does`), and there's no default, so none can be offered without saying:

- READS: nothing. It's called as it is.
- CHANGES: something, at once. The toolbox holds it back (held_back): refused in a reply that read the web and in a
  turn a door's lock set off (Request.by_door), and made only once the turn goes ahead (Request.go_ahead), so a draft
  thrown away changes nothing.
- COLLECTS: only what it puts on the turn (Turn.sent, Turn.changes), made once the reply is kept and forgotten with
  it. It's called holding the conversation, where those are kept; every other tool is called with it released, as it
  may wait (for the turn to be over, a device, the network).
- ITS_OWN: reads and changes, told apart action by action in its own code, which holds back each change as it must:
  the house's unlocking, a door's own turn and the web (home.py, ha_tool.py), and each provider's own, said where it
  hands over its toolset; every change at least as held_back does.

Web search isn't one of them: it's OpenAI's own tool, run on its side, and the brain marks the reply that used it
(Request.read_the_web) for every tool here to go by. A tool whose answers are words from anywhere (`outside`: a
feed's stories) marks its reply the same way once it's answered.

A toolset may also have markers: something the model writes in its reply for the provider (the feeds' <told 2 5>, the
stories it told), which the brain takes out of what's said or posted and hands to the provider once the reply is over.
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .home import Request

READS, CHANGES, COLLECTS, ITS_OWN = "reads", "changes", "collects", "its own"
DOES = (READS, CHANGES, COLLECTS, ITS_OWN)
READ_THE_WEB = "refused: this reply read the web, so it changes nothing; they can ask again on its own"
WITHDRAWN = "error: the request was withdrawn before anything was done; nothing changed"
BY_DOOR = (
    "refused: this ran because the {door}'s lock said someone came in, and that alone never changes anything; say "
    "what could be done, and they can ask for it"
)


@dataclass
class Turn:
    """The reply a tool call is part of: `request` (home.Request: who asked, how, whether the turn is over yet, and
    whether the reply read the web), and what the reply collects, made only once it's kept: `sent`, items for the TARS
    page (conversations.SentItem), and `changes` to the reminders (reminders.Change). `no_links`: what the model
    calling should do instead of sending a link, which needs web search; "" if it can."""

    request: Request
    sent: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    no_links: str = ""


@dataclass(frozen=True)
class Tool:
    """One tool: `spec`, as the model is offered it (a Responses API function tool), and `call(args, turn)`, with the
    arguments as the model wrote them, decoded (an object, unless the model got it wrong), answering in words for the
    model. `does`: what calling it may change (READS, CHANGES, COLLECTS or ITS_OWN). `failed`: what the model is told
    when the call raises, with {e!r}. `slow(arguments)`, with the arguments as written: whether it takes long enough
    that TARS says "One moment." first. `outside`: its answers are words from anywhere (a feed's stories), so the
    reply that's read one changes nothing after it, as one that searched the web."""

    spec: Mapping
    does: str
    call: Callable[[object, Turn], str]
    failed: str = "error: it failed ({e!r}); nothing is known to have changed"
    slow: Callable[[str], bool] | None = None
    outside: bool = False

    def __post_init__(self):
        if self.does not in DOES:
            raise ValueError(f"the {self.spec.get('name')} tool has to say what it does: one of {', '.join(DOES)}")

    @property
    def name(self) -> str:
        return self.spec["name"]


@dataclass(frozen=True)
class Marker:
    """Something the model writes in a reply for a provider, never said or posted: `pattern` (what group 1 of it
    carries is the provider's), `starts` (how it begins, as written and HTML-escaped: words that may yet become one are
    held back until it's known), and `heard(request, found)`, once the reply is over, with what each one in it
    carried, in order: [] when it had none."""

    pattern: re.Pattern
    starts: tuple[str, ...]
    heard: Callable[[Request | None, list[str]], None]


@dataclass(frozen=True, eq=False)
class Toolset:
    """What a provider gives the brain: its `tools`, in the order they're offered; its `rules`, each given in the
    instructions as it is; for abilities(), what they let a model do (`can`) and what a model without them hands
    over (`needs`); and its `markers`, taken out of every reply and handed back to it."""

    tools: Sequence[Tool] = ()
    rules: Sequence[str] = ()
    can: Sequence[str] = ()
    needs: Sequence[str] = ()
    markers: Sequence[Marker] = ()


def held_back(request: Request | None) -> str:
    """Why a change asked for in `request`'s turn isn't made, or "" when it may be: refused in a reply that read the
    web (Request.read_the_web), then in a turn a door's lock set off (Request.by_door), and otherwise waiting for the
    turn to go ahead (Request.go_ahead), withdrawn if it doesn't. None, or one with no go_ahead: a turn from nobody in
    particular, over already (typed). What the toolbox holds a CHANGES tool to, and an ITS_OWN tool each of its
    changes."""
    if request is None:
        return ""
    if getattr(request, "read_the_web", False):
        return READ_THE_WEB
    if door := getattr(request, "by_door", ""):
        return BY_DOOR.format(door=door)
    go_ahead = getattr(request, "go_ahead", None)
    return "" if not callable(go_ahead) or go_ahead() else WITHDRAWN


class Provider(Protocol):
    def toolset(self) -> Toolset:
        """Its tools, as they are when a brain is made."""


def one_tool(provider, does: str, failed: str, outside: bool = False, markers: Sequence[Marker] = ()) -> Toolset:
    """The toolset of a provider with one tool, as the browser's errands, the feeds, the skills and TARS itself have
    theirs: `tool`, `rules`, `can`, `needs`, and call(args, request)."""
    tool = Tool(provider.tool, does, lambda args, turn: provider.call(args, turn.request), failed, outside=outside)
    return Toolset([tool], [provider.rules], [provider.can], [provider.needs], markers)


class Toolbox:
    """Toolsets, in order: what a model is offered of them, and each call to one of their tools, by its name."""

    def __init__(self, toolsets: Iterable[Toolset]):
        self.toolsets = list(toolsets)
        self._tools: dict[str, Tool] = {}
        for tool in (t for toolset in self.toolsets for t in toolset.tools):
            if tool.name in self._tools:
                raise ValueError(f"two tools are called {tool.name}")
            self._tools[tool.name] = tool

    @property
    def specs(self) -> list[Mapping]:
        return [tool.spec for tool in self._tools.values()]

    @property
    def rules(self) -> list[str]:
        return [rule for toolset in self.toolsets for rule in toolset.rules]

    @property
    def can(self) -> list[str]:
        return [can for toolset in self.toolsets for can in toolset.can]

    @property
    def needs(self) -> list[str]:
        return [need for toolset in self.toolsets for need in toolset.needs]

    @property
    def markers(self) -> list[Marker]:
        return [marker for toolset in self.toolsets for marker in toolset.markers]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def slow(self, name: str, arguments: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.slow and tool.slow(arguments))

    def call(self, tool: Tool, args, turn: Turn) -> str:
        """What `tool` answers, held back as its `does` says; its failing, said as its `failed`."""
        try:
            if tool.does == CHANGES and (refused := held_back(turn.request)):
                return refused
            return tool.call(args, turn)
        except Exception as e:  # noqa: BLE001 - a tool that fails never costs the reply: the model says so
            return tool.failed.format(e=e)
