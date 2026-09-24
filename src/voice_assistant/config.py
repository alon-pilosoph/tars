import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AudioConfig:
    input_device: str = ""
    output_device: str = ""
    playback_prebuffer_s: float = 0.3


@dataclass
class WakeConfig:
    mode: str = "wakeword"
    model: str = "hey_jarvis"
    threshold: float = 0.5


@dataclass
class RecorderConfig:
    vad_aggressiveness: int = 2
    start_timeout_s: float = 5.0
    end_silence_s: float = 0.5
    max_utterance_s: float = 15.0
    follow_up_s: float = 4.0


@dataclass
class STTConfig:
    model: str = "gpt-4o-mini-transcribe"
    language: str = "en"


@dataclass
class LLMConfig:
    model: str = "gpt-4.1-mini"
    service_tier: str = ""
    reasoning_effort: str = ""
    memory_minutes: float = 10.0
    system_prompt: str = "You are a helpful voice assistant. Answer in one to three short sentences."


@dataclass
class TTSConfig:
    model: str = "gpt-4o-mini-tts"
    voice: str = "alloy"
    instructions: str = ""
    effect: str = ""


@dataclass
class SpeakerConfig:
    enabled: bool = False
    threshold: float = 0.5
    model: str = "models/voxceleb_resnet34_LM.onnx"
    voiceprints: str = "voice_data/voiceprints.npz"


@dataclass
class Config:
    audio: AudioConfig
    wake: WakeConfig
    recorder: RecorderConfig
    stt: STTConfig
    llm: LLMConfig
    tts: TTSConfig
    speaker: SpeakerConfig


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    return Config(
        audio=AudioConfig(**raw.get("audio", {})),
        wake=WakeConfig(**raw.get("wake", {})),
        recorder=RecorderConfig(**raw.get("recorder", {})),
        stt=STTConfig(**raw.get("stt", {})),
        llm=LLMConfig(**raw.get("llm", {})),
        tts=TTSConfig(**raw.get("tts", {})),
        speaker=SpeakerConfig(**raw.get("speaker", {})),
    )
