"""Settings from config.toml, one dataclass per section. A setting that can't work stops the assistant at startup
with one line naming it, rather than failing on the first request."""

import dataclasses
import tomllib
import typing
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(SystemExit):
    """A setup that can't work: a bad setting, or a missing key or model file."""


# The default speech to text, and the backup for the streaming services.
OPENAI_STT_MODEL = "gpt-4o-mini-transcribe"


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
    answer_early_s: float = 0.25  # start preparing the answer after this much silence; it plays only once you're done
    turn_model: str = "models/smart-turn-v3.2-cpu.onnx"


@dataclass
class STTConfig:
    provider: str = "openai"  # openai | deepgram (streams while you talk) | flux (Deepgram also ends your turn)
    model: str = OPENAI_STT_MODEL
    language: str = "en"


@dataclass
class LLMConfig:
    model: str = "gpt-4.1-mini"
    # Answers first, on Cerebras; turns that need the web or the TARS page go to `model`. Empty = `model` answers all.
    cerebras_model: str = ""
    service_tier: str = ""
    reasoning_effort: str = ""  # sent to both models: OpenAI's and Cerebras's
    memory_minutes: float = 10.0
    system_prompt: str = "You are a helpful voice assistant. Answer in one to three short sentences."
    humor: int = 75  # percent; "{humor}" in the system prompt is replaced with it
    # Let it look things up on the web (current events, real links to send). Only used when the model decides to.
    web_search: bool = True
    # Let it send links, notes, lists and text files to the web UI (needs [learning] log_events).
    send: bool = True


@dataclass
class TTSConfig:
    provider: str = "openai"  # openai | deepgram (Flux TTS or Aura-2, by the model's name)
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
    # Keep every wake, near-miss, conversation and sent item (voice_data/events/) for the web UI and training.
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
class RemindersConfig:
    # Timers, reminders and messages, set by voice or on the web UI (docs/reminders.md). Needs [learning] log_events.
    enabled: bool = True
    # One that waits for an acknowledgement is said again this often, at most this many times, then it's missed.
    repeat_every_min: float = 2.0
    max_tries: int = 10
    # After saying one, how long TARS listens for "got it" without the wake word.
    ack_window_s: float = 6.0


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
    reminders: RemindersConfig


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text()) if path.exists() else {}
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    sections = {f.name: f.type for f in dataclasses.fields(Config)}
    problems = [f"there's no [{name}] section" for name in raw if name not in sections]
    cfg = Config(**{name: _section(kind, name, raw.get(name, {}), problems) for name, kind in sections.items()})
    problems = problems or _problems(cfg)  # the checks between settings assume each one is the right type
    if problems:
        raise ConfigError(f"{path}: " + "; ".join(problems))
    return cfg


def required_keys(cfg: Config) -> list[str]:
    keys = ["OPENAI_API_KEY"]  # always: OpenAI's model answers what needs the web or the TARS page, and backs up STT
    if cfg.stt.provider in ("deepgram", "flux") or cfg.tts.provider == "deepgram":
        keys.append("DEEPGRAM_API_KEY")
    if cfg.llm.cerebras_model:
        keys.append("CEREBRAS_API_KEY")
    return keys


_KINDS = {str: "text", bool: "true or false", int: "a whole number", float: "a number", list: "a list"}


def _section(kind: type, name: str, values, problems: list[str]):
    """One section's settings; a misspelled or mistyped one is added to `problems` and left at its default."""
    if not isinstance(values, dict):
        problems.append(f"[{name}] must be a section")
        return kind()
    types = {f.name: f.type for f in dataclasses.fields(kind)}
    kept = {}
    for key, value in values.items():
        if key not in types:
            problems.append(f"[{name}] has no setting {key}")
        elif not _fits(value, types[key]):
            wanted = _KINDS[typing.get_origin(types[key]) or types[key]]
            problems.append(f"[{name}] {key} must be {wanted}, not {value!r}")
        else:
            kept[key] = value
    return kind(**kept)


def _fits(value, kind) -> bool:
    if isinstance(value, bool) and kind is not bool:  # bool is a subclass of int
        return False
    if kind is float:
        return isinstance(value, int | float)
    if typing.get_origin(kind) is list:
        (item,) = typing.get_args(kind)
        return isinstance(value, list) and all(isinstance(v, item) for v in value)
    return isinstance(value, kind)


def _problems(cfg: Config) -> list[str]:
    r, stt, problems = cfg.recorder, cfg.stt, []
    if cfg.wake.mode not in ("wakeword", "push-to-talk"):
        problems.append(f"[wake] mode must be wakeword or push-to-talk, not {cfg.wake.mode!r}")
    if stt.provider not in ("openai", "deepgram", "flux"):
        problems.append(f"[stt] provider must be openai, deepgram or flux, not {stt.provider!r}")
    elif stt.provider == "deepgram" and stt.model.startswith("gpt-"):
        problems.append(f"[stt] model must be a Deepgram model (e.g. nova-3) for provider deepgram, not {stt.model!r}")
    elif stt.provider == "flux" and stt.language and not stt.language.lower().startswith("en"):
        problems.append(f"[stt] provider flux only understands English, not language {stt.language!r}")
    if cfg.tts.provider not in ("openai", "deepgram"):
        problems.append(f"[tts] provider must be openai or deepgram, not {cfg.tts.provider!r}")
    elif cfg.tts.provider == "deepgram" and not cfg.tts.model.startswith(("flux-", "aura-")):
        problems.append(f"[tts] model must be a Deepgram voice (flux-... or aura-2-...), not {cfg.tts.model!r}")
    if cfg.tts.effect:
        from .effects import EFFECTS  # only here: it loads scipy

        if cfg.tts.effect not in EFFECTS:
            problems.append(f"[tts] effect must be {' or '.join(EFFECTS)}, or empty for none, not {cfg.tts.effect!r}")
    if r.end_of_turn not in ("silence", "smart"):
        problems.append(f"[recorder] end_of_turn must be silence or smart, not {r.end_of_turn!r}")
    if not 0.2 <= r.vad_threshold <= 0.95:
        problems.append("[recorder] vad_threshold must be between 0.2 and 0.95")
    if not 0 <= r.answer_early_s < r.end_silence_s:
        problems.append("[recorder] answer_early_s must be at least 0 and shorter than end_silence_s")
    if cfg.reminders.repeat_every_min < 0.5:
        problems.append("[reminders] repeat_every_min must be at least 0.5")
    if not 1 <= cfg.reminders.max_tries <= 30:
        problems.append("[reminders] max_tries must be between 1 and 30")
    if r.max_pause_s < r.end_silence_s:
        problems.append("[recorder] max_pause_s can't be shorter than end_silence_s")
    return problems
