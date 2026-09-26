from collections.abc import Iterator
from typing import Protocol

import httpx
from openai import OpenAI

from .config import TTSConfig

# A reply's audio normally starts within a second. If nothing arrives for this long, give up: TARS says so in its
# own (pre-made) voice instead of going quiet for the client's full timeout.
STALL_S = 4.0


class Voice(Protocol):
    sample_rate: int

    def stream(self, text: str) -> Iterator[bytes]:
        """Yield 16-bit mono PCM chunks as they're synthesized."""


class OpenAISpeech:
    # OpenAI's raw "pcm" format is 24 kHz, 16-bit, mono.
    sample_rate = 24_000

    def __init__(self, client: OpenAI, cfg: TTSConfig):
        self._client = client.with_options(timeout=httpx.Timeout(STALL_S, connect=3.0), max_retries=0)
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
