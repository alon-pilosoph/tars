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
    verify: bool = False  # double-check each wake with an offline speech recognizer (verify.py)
    check_window_s: float = 2.5  # how much audio before the wake the double-check hears
    check_model: str = ""  # a learned layer for the double-check (.json); "" = plain phrase match


@dataclass
class RecorderConfig:
    vad_aggressiveness: int = 2
    start_timeout_s: float = 5.0
    end_silence_s: float = 0.5
    max_utterance_s: float = 15.0
    follow_up_s: float = 4.0
    greet_after_s: float = 1.5  # say "Yes, <name>?" if nothing follows the wake word this long; 0 = off


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
    # "hey TARS" alone is under a second of speech, so naming someone from it needs its own, lower bar.
    wake_threshold: float = 0.25
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
