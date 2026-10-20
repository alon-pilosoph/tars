"""A cloud service failing, as opposed to a bug. Deepgram's and Cartesia's voices and Deepgram's speech to text raise
ServiceError, from the error that says why; OpenAI's SDK raises OpenAIError. Either costs a turn, logged in a line,
never traced like a bug."""

from openai import OpenAIError


class ServiceError(Exception):
    """A cloud service failed: unreachable, too slow, refusing the request, or answering with something unreadable."""


SERVICE_FAILURES = (ServiceError, OpenAIError)
