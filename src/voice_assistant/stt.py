import io
import wave
from typing import Protocol

from openai import OpenAI

from .audio import SAMPLE_RATE
from .config import STTConfig


class Transcriber(Protocol):
    def transcribe(self, pcm: bytes) -> str: ...


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buf.getvalue()


class OpenAITranscriber:
    def __init__(self, client: OpenAI, cfg: STTConfig):
        self._client = client
        self._cfg = cfg

    def transcribe(self, pcm: bytes) -> str:
        result = self._client.audio.transcriptions.create(
            model=self._cfg.model,
            file=("speech.wav", pcm_to_wav(pcm), "audio/wav"),
            language=self._cfg.language or None,
        )
        return result.text.strip()
