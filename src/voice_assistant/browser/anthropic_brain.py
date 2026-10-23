"""The browser agent's model on Anthropic's Messages API (Claude Opus 5.5, say), inside TARS's own loop (agent.py):
the same tools, the same view of the page and the same history as the OpenAI brain, so the model is the only thing
that differs between them.

- One tool call a turn: Claude Opus 5.5 refuses forced tool use, so the call is asked for in the system prompt, with
  `tool_choice` auto and parallel calls off; a reply with no call gets one reminder, then counts as no call.
- The page goes in after the tool's result, in the same message (a picture with it when there is one), and earlier
  pages shrink to a line, as in the OpenAI brain. That edits earlier turns, which would invalidate the model's
  thinking blocks if they were sent back, so they aren't: each turn's tool call goes back without its reasoning,
  the way TARS's loop keeps the OpenAI model's (store=False). Effort sets how much it thinks.
- The tools and the system prompt are cached: they're the same on every turn.
- If the model declines a request, Anthropic's API runs it again on another model (`fallbacks: "default"`); that's
  counted, since it would mean the turn wasn't the model's own.
"""

import base64
from typing import Any

from anthropic import Anthropic

from ..check import env_value

ONE_CALL = ("\n\nAnswer every turn with exactly one call to one of your tools, and nothing else: the act tool to work "
            "the page, the done tool to finish.")  # fmt: skip
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16_000


def client(env) -> Anthropic:
    key = env_value(env, "ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(f"no ANTHROPIC_API_KEY in {env}")
    return Anthropic(api_key=key, timeout=120, max_retries=2)


class AnthropicBrain:
    def __init__(self, client: Anthropic, settings, tools: list[dict[str, Any]]):
        from .agent import Call  # the agent module imports this one only when a Claude model is asked for

        self.Call = Call
        self.client, self.settings = client, settings
        self.tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools
        ]
        self.tools[-1]["cache_control"] = {"type": "ephemeral"}  # the tools, then the system prompt, are cached
        self.system: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.pages: list[tuple[int, int]] = []  # (message, block) of the page in full: only the newest is kept so
        self.pending: list[dict[str, Any]] = []  # what goes in the next user message: tool results, then the page
        self.used = (0, 0, 0)  # tokens so far: in (cache reads and writes too), of them cached, out
        self.fallbacks = 0  # turns another model answered, after this one declined

    def start(self, instructions: str) -> None:
        self.system = [{"type": "text", "text": instructions + ONE_CALL, "cache_control": {"type": "ephemeral"}}]

    def show(self, text: str, picture: bytes | None, new_page: bool = True) -> None:
        """The page, with the next tool results; `new_page` False: only a picture of the page it has."""
        if new_page:
            for m, b in self.pages:  # the page before shrinks to its title and address
                head = self.messages[m]["content"][b]["text"]
                title = next((ln for ln in head.splitlines() if ln.startswith(("Title:", "Address:"))), "")
                self.messages[m]["content"][b] = {"type": "text", "text": f"(an earlier page. {title})"}
            self.pages.clear()
        self.pending.append({"type": "text", "text": text, "page": new_page})
        if picture:
            data = base64.standard_b64encode(picture).decode()
            self.pending.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                             "data": data}})  # fmt: skip

    def answer(self, call, said: str) -> None:
        self.pending.append({"type": "tool_result", "tool_use_id": call.id, "content": said})

    def ask(self, effort: str):
        self._send_pending()
        for attempt in range(2):
            reply = self.client.beta.messages.create(
                model=self.settings.model,
                max_tokens=MAX_TOKENS,
                system=self.system,
                tools=self.tools,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                output_config={"effort": effort},
                messages=self.messages,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
            self._count(reply)
            uses = [b for b in reply.content if b.type == "tool_use"]
            if reply.stop_reason == "refusal" or (not uses and attempt == 1):
                return None
            # The turn goes back as its text and its tool call: its thinking isn't sent back (see above).
            kept = [b.model_dump(exclude_none=True) for b in reply.content if b.type in ("text", "tool_use")]
            self.messages.append({"role": "assistant", "content": kept or [{"type": "text", "text": "(no reply)"}]})
            if uses:
                use = uses[0]
                return self.Call(use.id, use.name, use.input if isinstance(use.input, dict) else {})
            self.messages.append({"role": "user", "content": [{"type": "text", "text": "Call one of your tools."}]})
        return None

    def _send_pending(self) -> None:
        """The tool results and the page, as the next user message (results first, as the API wants)."""
        if not self.pending:
            return
        content = sorted(self.pending, key=lambda b: b["type"] != "tool_result")
        pages = [i for i, b in enumerate(content) if b.pop("page", False)]
        self.messages.append({"role": "user", "content": content})
        if pages:
            self.pages = [(len(self.messages) - 1, pages[-1])]
        self.pending = []

    def _count(self, reply) -> None:
        u = reply.usage
        written, read = u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0
        self.used = (self.used[0] + u.input_tokens + written + read, self.used[1] + read,
                     self.used[2] + u.output_tokens)  # fmt: skip
        if any(getattr(i, "type", "") == "fallback_message" for i in (getattr(u, "iterations", None) or [])):
            self.fallbacks += 1
