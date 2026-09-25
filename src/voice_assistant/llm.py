import itertools
import time
from collections.abc import Iterable, Iterator
from typing import Protocol

from openai import OpenAI

from .config import LLMConfig

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


class Brain(Protocol):
    def stream_reply(self, text: str) -> Iterator[str]:
        """Yield the reply in pieces as it's generated."""

    def forget_last(self) -> None:
        """Drop the most recent exchange from the conversation history."""


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


class OpenAIChat:
    def __init__(self, client: OpenAI, cfg: LLMConfig):
        self._client = client
        self._cfg = cfg
        self._history: list[dict] = []
        self._last_turn_at = self._previous_turn_at = 0.0
        # Only send optional settings that are configured; not every model accepts them.
        self._extra = {
            key: value
            for key, value in {"service_tier": cfg.service_tier, "reasoning_effort": cfg.reasoning_effort}.items()
            if value
        }

    def stream_reply(self, text: str) -> Iterator[str]:
        # After a long pause, start fresh: old context confuses more than it helps, and it's private.
        if self._history and time.monotonic() - self._last_turn_at > self._cfg.memory_minutes * 60:
            self._history.clear()
        self._previous_turn_at, self._last_turn_at = self._last_turn_at, time.monotonic()
        self._history.append({"role": "user", "content": text})
        stream = self._client.chat.completions.create(
            model=self._cfg.model,
            messages=[{"role": "system", "content": f"{self._cfg.system_prompt}\n\n{PROTOCOL}"}, *self._history],
            stream=True,
            **self._extra,
        )
        answer = ""
        for chunk in stream:
            if chunk.choices and (piece := chunk.choices[0].delta.content):
                answer += piece
                yield piece
        self._history.append({"role": "assistant", "content": answer.strip()})
        self._history = self._history[-MAX_TURNS * 2 :]

    def forget_last(self) -> None:
        # Overheard chatter shouldn't keep the memory alive either.
        del self._history[-2:]
        self._last_turn_at = self._previous_turn_at
