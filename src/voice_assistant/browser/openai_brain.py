"""The browser agent's model on OpenAI's Responses API (Luna, Sol): the only way Luna calls tools while it reasons."""

import base64
import json
from typing import Any

from .brain import SQUEEZE, Call, earlier_page


class OpenAIBrain:
    """The model, on OpenAI's Responses API (Luna's only way to call tools while reasoning), with store off.

    With full history (Settings.history), every page stays as it was seen and the model's reasoning is carried from
    turn to turn (its encrypted reasoning items, sent back), the way Claude Code keeps a conversation: what a long
    errand needs. Repeated input is cached, so it costs little; past SQUEEZE tokens a request, or with short history,
    each page shrinks to a line once a newer one comes, and nothing of its reasoning is carried."""

    def __init__(self, client, settings, tools: list[dict[str, Any]]):
        self.client, self.settings, self.tools = client, settings, tools
        self.fallbacks = 0  # OpenAI's API has none
        self.full = settings.history == "full"
        self.instructions = ""
        self.items: list[dict[str, Any]] = []
        self.pages: list[int] = []  # the items that are a page in full
        self.used = (0, 0, 0)  # tokens so far: in, of them cached, out

    def start(self, instructions: str) -> None:
        self.instructions = instructions

    def ask(self, effort: str) -> Call | None:
        reply = self.client.responses.create(
            model=self.settings.model,
            instructions=self.instructions,
            input=self.items,
            tools=self.tools,
            tool_choice="required",
            parallel_tool_calls=False,
            store=False,
            reasoning={"effort": effort},
            **({"include": ["reasoning.encrypted_content"]} if self.full else {}),
        )
        if usage := reply.usage:
            details = getattr(usage, "input_tokens_details", None)
            cached = getattr(details, "cached_tokens", 0) or 0
            self.used = (self.used[0] + usage.input_tokens, self.used[1] + cached, self.used[2] + usage.output_tokens)
            if self.full and usage.input_tokens > SQUEEZE:
                self._squeeze()
        calls = [i for i in reply.output if getattr(i, "type", "") == "function_call"]
        if not calls:
            return None
        call = calls[0]
        if self.full:  # its reasoning goes back with the call, in the order it came
            self.items += [
                i.model_dump(exclude_none=True) for i in reply.output if getattr(i, "type", "") == "reasoning"
            ]
        self.items.append(
            {"type": "function_call", "call_id": call.call_id, "name": call.name, "arguments": call.arguments}
        )
        try:
            args = json.loads(call.arguments or "{}")
        except ValueError:
            args = {}
        return Call(call.call_id, call.name, args if isinstance(args, dict) else {})

    def answer(self, call: Call, said: str) -> None:
        self.items.append({"type": "function_call_output", "call_id": call.id, "output": said})

    def show(self, text: str, picture: bytes | None, new_page: bool = True) -> None:
        """The page as the newest message; `new_page` False: only a picture of the page it has, kept as it is."""
        if new_page and not self.full:
            self._shrink(keep=0)
        if new_page:
            self.pages.append(len(self.items))
        self.items.append({"role": "user", "content": _openai_content(text, picture)})

    def _shrink(self, keep: int) -> None:
        """Every page but the newest `keep` as a line: its title and address."""
        for i in self.pages[: len(self.pages) - keep]:
            self.items[i]["content"] = [
                {"type": "input_text", "text": earlier_page(self.items[i]["content"][0]["text"])}
            ]
        self.pages = self.pages[len(self.pages) - keep :] if keep else []

    def _squeeze(self) -> None:
        """Too long to send whole every turn: the rest of the run in short history, from the newest page on."""
        self.full = False
        self._shrink(keep=1)


def _openai_content(text: str, picture: bytes | None) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
    if picture:
        data = base64.b64encode(picture).decode()
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{data}", "detail": "auto"})
    return content


def _openai_content(text: str, picture: bytes | None) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
    if picture:
        data = base64.b64encode(picture).decode()
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{data}", "detail": "auto"})
    return content
