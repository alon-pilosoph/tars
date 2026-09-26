import tomllib
from dataclasses import dataclass, field
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
    vad_threshold: float = 0.5  # how sure the speech detector must be that it's hearing speech (0-1)
    vad_model: str = "models/silero_vad.onnx"
    start_timeout_s: float = 5.0
    end_silence_s: float = 0.8
    max_utterance_s: float = 15.0
    follow_up_s: float = 4.0
    greet_after_s: float = 1.5  # say "Yes, <name>?" if nothing follows the wake word this long; 0 = off
    end_of_turn: str = "smart"  # silence: end_silence_s ends it | smart: a model may extend it to max_pause_s
    max_pause_s: float = 1.6
    turn_model: str = "models/smart-turn-v3.2-cpu.onnx"


@dataclass
class STTConfig:
    provider: str = "openai"  # openai | deepgram (streams while you talk)
    model: str = "gpt-4o-mini-transcribe"
    language: str = "en"


@dataclass
class LLMConfig:
    model: str = "gpt-4.1-mini"
    service_tier: str = ""
    reasoning_effort: str = ""
    memory_minutes: float = 10.0
    system_prompt: str = "You are a helpful voice assistant. Answer in one to three short sentences."
    humor: int = 75  # percent; "{humor}" in the system prompt is replaced with it
    # Let it look things up on the web (current events, real links to send). Only used when the model decides to.
    web_search: bool = True
    # Let it send links, notes, lists and text files to the web UI (needs [learning] log_events).
    send: bool = True


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
class LearningConfig:
    # Keep every wake, near-miss, conversation and sent item (voice_data/events/) for the web UI and retraining.
    # Off also turns off sending ([llm] send): sent items live there.
    log_events: bool = True
    folder: str = "voice_data/events"
    # Audio of events nothing labels (lonely near-misses, wakes nobody spoke after) is dropped after this many days.
    keep_audio_days: float = 60.0


@dataclass
class WebConfig:
    # More names the web UI may be reached by, besides IP addresses, localhost, *.local, *.ts.net and single-label
    # names (which always work). Requests to any other name are refused.
    allowed_hosts: list[str] = field(default_factory=list)


@dataclass
class Config:
    audio: AudioConfig
    wake: WakeConfig
    recorder: RecorderConfig
    stt: STTConfig
    llm: LLMConfig
    tts: TTSConfig
    speaker: SpeakerConfig
    learning: LearningConfig
    web: WebConfig


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
        learning=LearningConfig(**raw.get("learning", {})),
        web=WebConfig(**raw.get("web", {})),
    )
