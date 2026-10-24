"""The browser agent's model, whichever it is (OpenAI's on the Responses API, openai_brain.py; Anthropic's on the
Messages API, anthropic_brain.py): what the agent's loop (agent.py) asks of either, and what they share."""

from dataclasses import dataclass
from typing import Any, Protocol

SQUEEZE = 150_000  # input tokens a request: past this, the rest of the run keeps only the newest page in full


@dataclass
class Call:
    """One tool call a model made: its id, the tool and the arguments."""

    id: str
    name: str
    args: dict[str, Any]


class Brain(Protocol):
    """A model the agent works with: told its instructions, shown each page, asked for its next call, told what the
    call did. `used`: tokens so far (in, of them cached, out); `fallbacks`: turns another model answered."""

    used: tuple[int, int, int]
    fallbacks: int

    def start(self, instructions: str) -> None: ...

    def show(self, text: str, picture: bytes | None, new_page: bool = True) -> None: ...

    def ask(self, effort: str) -> Call | None: ...

    def answer(self, call: Call, said: str) -> None: ...


def earlier_page(text: str) -> str:
    """A page the model saw, once a newer one has come and it's shrunk: its title and address, from its first lines
    (page.View's text starts with them)."""
    found = [ln for ln in text.splitlines()[:3] if ln.startswith(("Title:", "Address:"))]
    return f"(an earlier page. {' '.join(found)})"


def brain_for(client, settings, tools: list[dict[str, Any]]) -> Brain:
    """The brain for the settings' model: a Claude model on Anthropic's API, else OpenAI's."""
    if settings.model.startswith("claude-"):
        from .anthropic_brain import AnthropicBrain

        return AnthropicBrain(client, settings, tools)
    from .openai_brain import OpenAIBrain

    return OpenAIBrain(client, settings, tools)
