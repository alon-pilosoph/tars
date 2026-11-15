"""A service's failure said in a line (errors.py), not as the whole response it came with."""

import httpx
import pytest
from openai import APIStatusError
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from voice_assistant.check import FAIL, line, outcome, service_error
from voice_assistant.errors import SAID_MAX, ServiceError, said, status_of

CARTESIA = (
    b'{"error_code":"quota_exceeded","message":"Please upgrade your subscription at https://play.cartesia.ai/subscript'
    b'ion to increase your credit limit or enable overages for your account.","title":"Model credits limit reached"}'
)


def refused(status: int, body: bytes = b"", reason: str = "") -> InvalidStatus:
    """A WebSocket handshake a service turned down, as websockets raises it."""
    return InvalidStatus(Response(status, reason, Headers([("Content-Type", "application/json")]), bytearray(body)))


def test_a_refused_websocket_is_its_status_and_the_services_own_words():
    e = refused(402, CARTESIA, "Payment Required")
    assert (said(e), status_of(e)) == ("HTTP 402: Model credits limit reached", 402)
    assert said(refused(503, b"", "Service Unavailable")) == "HTTP 503: Service Unavailable"
    assert said(refused(500, b"<html>an error page</html>")) == "HTTP 500: <html>an error page</html>"


def test_httpx_and_openai_refusals_too():
    request = httpx.Request("POST", "https://api.deepgram.com/v1/listen")
    response = httpx.Response(429, json={"err_msg": "Too many requests, slow down"}, request=request)
    e = httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)
    assert said(e) == "HTTP 429: Too many requests, slow down"
    openai = APIStatusError(
        "Error code: 500", response=httpx.Response(500, json={"error": {"message": "The server had an error"}},
                                                    request=request), body=None)  # fmt: skip
    assert said(openai) == "HTTP 500: The server had an error"


def test_anything_else_is_its_own_words_on_one_short_line():
    assert said(TimeoutError("timed out")) == "timed out"
    assert said(ConnectionResetError()) == "ConnectionResetError"
    long = said(ValueError("x " * 500))
    assert len(long) == SAID_MAX and long.endswith("…") and "\n" not in said(ValueError("a\nb"))


def test_the_check_says_what_to_do_about_each_refusal():
    voice = ServiceError(f"Cartesia voice: {said(refused(402, CARTESIA))}", 402)
    assert line(service_error("Voice", voice)) == (
        "✗ Voice: out of credits: Cartesia voice: HTTP 402: Model credits limit reached\n"
        "    → top up, or raise the plan's limit, on the service's site"
    )
    assert "too many requests" in service_error("Deepgram", ServiceError("Deepgram: HTTP 429", 429)).detail
    assert service_error("OpenAI", ServiceError("x", 401)).detail == "the API key was refused (401)"


def test_a_check_that_raises_a_service_failure_is_said_the_same_way():
    def broken():
        raise ServiceError(f"Cartesia voice: {said(refused(402, CARTESIA))}", 402)

    [result] = outcome("Voice", broken)
    assert result.status == FAIL and result.detail.startswith("out of credits") and "Headers" not in result.detail


@pytest.mark.parametrize("status", [None, 418])
def test_status_is_none_unless_it_was_a_refusal(status):
    e = ServiceError("x", status)
    assert status_of(e) == status
