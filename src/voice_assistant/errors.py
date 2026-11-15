"""A cloud service failing, as opposed to a bug. Deepgram's and Cartesia's voices and Deepgram's speech to text raise
ServiceError, from the error that says why; OpenAI's SDK raises OpenAIError. Either costs a turn, logged in a line,
never traced like a bug."""

import json

from openai import OpenAIError

SAID_MAX = 200  # characters: a service's own reason is cut to this


class ServiceError(Exception):
    """A cloud service failed: unreachable, too slow, refusing the request, or answering with something unreadable.
    `status`: the HTTP status it refused with, if it did."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def status_of(e: BaseException) -> int | None:
    """The HTTP status a service refused with: from the SDKs' errors, httpx's, and websockets' refused handshake."""
    for value in (getattr(e, "status", None), getattr(e, "status_code", None)):
        if isinstance(value, int):
            return value
    code = getattr(getattr(e, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


def said(e: BaseException) -> str:
    """An error as one short line: a refusal as its status and what the service said ("HTTP 402: Model credits limit
    reached"), not the whole response with its headers."""
    status = status_of(e)
    if status is None:
        text = " ".join(str(e).split()) or type(e).__name__
        return _cut(text)
    reason = _reason(getattr(e, "response", None)) or _reason_text(getattr(e, "message", ""))
    return f"HTTP {status}" + (f": {reason}" if reason else "")


def _reason(response) -> str:
    """What a refusal's body says: the service's title or message, else its status's own words."""
    if response is None:
        return ""
    body = getattr(response, "body", None)  # websockets
    if body is None:
        try:
            body = response.content  # httpx
        except Exception:  # noqa: BLE001 - a streamed body not read: there's still the reason phrase
            body = b""
    text = bytes(body or b"").decode("utf-8", "replace")
    return _reason_text(text) or str(getattr(response, "reason_phrase", "") or "")


def _reason_text(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return _cut(" ".join(text.split()))
    if isinstance(data, dict):
        inner = data.get("error") if isinstance(data.get("error"), dict) else {}
        for value in (data.get("title"), data.get("message"), inner.get("message"), data.get("err_msg"),
                      data.get("description"), data.get("detail"), data.get("error")):  # fmt: skip
            if isinstance(value, str) and value.strip():
                return _cut(" ".join(value.split()))
    return ""


def _cut(text: str) -> str:
    return text if len(text) <= SAID_MAX else text[: SAID_MAX - 1] + "…"


SERVICE_FAILURES = (ServiceError, OpenAIError)
