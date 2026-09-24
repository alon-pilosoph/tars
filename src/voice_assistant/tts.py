from collections.abc import Iterator
from typing import Protocol

from openai import OpenAI

from .config import TTSConfig


class Voice(Protocol):
    sample_rate: int

    def stream(self, text: str) -> Iterator[bytes]:
        """Yield 16-bit mono PCM chunks as they're synthesized."""


class OpenAISpeech:
    # OpenAI's raw "pcm" format is 24 kHz, 16-bit, mono.
    sample_rate = 24_000

    def __init__(self, client: OpenAI, cfg: TTSConfig):
        self._client = client
        self._cfg = cfg
        # Only gpt-4o-mini-tts and newer accept delivery instructions; don't send them to older models.
        self._extra = {"instructions": cfg.instructions} if cfg.instructions else {}

    def stream(self, text: str) -> Iterator[bytes]:
        with self._client.audio.speech.with_streaming_response.create(
            model=self._cfg.model,
            voice=self._cfg.voice,
            input=text,
            response_format="pcm",
            **self._extra,
        ) as response:
            yield from response.iter_bytes(4096)
