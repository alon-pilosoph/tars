"""The browser agent's model on Anthropic's Messages API (Claude Opus 5.5, say), inside TARS's own loop (agent.py):
the same tools, the same view of the page and the same history as the OpenAI brain, so the model is the only thing
that differs between them.

- One tool call a turn: Claude Opus 5.5 refuses forced tool use, so the call is asked for in the system prompt, with
  `tool_choice` auto and parallel calls off; a reply with no call gets one reminder, then counts as no call.
- The page goes in after the tool's result, in the same message (a picture with it when there is one). With full
  history (Settings.history), every page stays as it was seen and the model's thinking blocks go back unchanged, so
  the history only ever grows (an edited one would invalidate them). Past SQUEEZE tokens a request, or with short
  history, pages shrink to a line once a newer one comes; then the thinking blocks are left out from then on, as
  the API asks of an edited history, and each turn goes back as its text and tool call.
- The tools, the system prompt and the conversation so far are cached (the conversation by the request's own
  marker, which moves to its end each turn): with full history, each turn pays full price only for what's new.
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
SQUEEZE = 150_000  # input tokens a request: past this, the rest of the run keeps only the newest page in full


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
        self.full = settings.history == "full"
        self.system: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.pages: list[tuple[int, int]] = []  # (message, block) of each page in full
        self.pending: list[dict[str, Any]] = []  # what goes in the next user message: tool results, then the page
        self.used = (0, 0, 0)  # tokens so far: in (cache reads and writes too), of them cached, out
        self.fallbacks = 0  # turns another model answered, after this one declined

    def start(self, instructions: str) -> None:
        self.system = [{"type": "text", "text": instructions + ONE_CALL, "cache_control": {"type": "ephemeral"}}]

    def show(self, text: str, picture: bytes | None, new_page: bool = True) -> None:
        """The page, with the next tool results; `new_page` False: only a picture of the page it has."""
        if new_page and not self.full:
            self._shrink(keep=0)
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
                cache_control={"type": "ephemeral"},  # the conversation so far: re-read each turn, at a cache read
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
            self._count(reply)
            uses = [b for b in reply.content if b.type == "tool_use"]
            if reply.stop_reason == "refusal" or (not uses and attempt == 1):
                return None
            # The turn goes back as it came, its thinking too with full history (see above).
            kinds = ("text", "tool_use", "thinking", "redacted_thinking") if self.full else ("text", "tool_use")
            kept = [b.model_dump(exclude_none=True) for b in reply.content if b.type in kinds]
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
        self.pages += [(len(self.messages) - 1, i) for i in pages]
        self.pending = []

    def _shrink(self, keep: int) -> None:
        """Every page but the newest `keep` as a line: its title and address."""
        for m, b in self.pages[: len(self.pages) - keep]:
            head = self.messages[m]["content"][b]["text"]
            title = next((ln for ln in head.splitlines() if ln.startswith(("Title:", "Address:"))), "")
            self.messages[m]["content"][b] = {"type": "text", "text": f"(an earlier page. {title})"}
        self.pages = self.pages[len(self.pages) - keep :] if keep else []

    def _squeeze(self) -> None:
        """Too long to send whole every turn: short history from here, and no thinking blocks, since shrinking the
        pages edits what they were written after."""
        self.full = False
        for message in self.messages:
            if message["role"] == "assistant":
                message["content"] = [b for b in message["content"]
                                      if b.get("type") not in ("thinking", "redacted_thinking")] or [
                    {"type": "text", "text": "(thinking)"}]  # fmt: skip
        self._shrink(keep=1)

    def _count(self, reply) -> None:
        u = reply.usage
        written, read = u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0
        self.used = (self.used[0] + u.input_tokens + written + read, self.used[1] + read,
                     self.used[2] + u.output_tokens)  # fmt: skip
        if self.full and u.input_tokens + written + read > SQUEEZE:
            self._squeeze()
        if any(getattr(i, "type", "") == "fallback_message" for i in (getattr(u, "iterations", None) or [])):
            self.fallbacks += 1
