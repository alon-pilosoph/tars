"""Settings from config.toml, one dataclass per section. A setting that can't work stops the assistant at startup
with one line naming it, rather than failing on the first request."""

import dataclasses
import re
import tomllib
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple


class ConfigError(SystemExit):
    """A setup that can't work: a bad setting, or a missing key or model file."""


# The default speech to text, and the backup for the streaming services.
OPENAI_STT_MODEL = "gpt-4o-mini-transcribe"

DEEPGRAM_HOST = "api.deepgram.com"
DEEPGRAM_STT_MODEL = "nova-3"
DEEPGRAM_VOICE = "flux-wes-en"
OPENAI_KEY, DEEPGRAM_KEY, CARTESIA_KEY = "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY"
TELEGRAM_KEY = "TELEGRAM_BOT_TOKEN"


class QuickProvider(NamedTuple):
    name: str
    url: str
    key_name: str
    setting: str
    max_tokens: int  # a reply's cap, reasoning included


# Where the quick model runs, in the order they're asked: each runs Qwen as its [llm] `setting` names it. Groq's free
# key allows 1,000 written tokens a minute, and refuses a request whose cap is over what's left, so its replies are
# capped at 500: enough for a spoken answer, not for a long note. Cerebras has room for one, and takes the turn when
# Groq's reply runs out before anything was said or done.
QUICK_PROVIDERS = (
    QuickProvider("Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "groq_model", 500),
    QuickProvider("Cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "cerebras_model", 2000),
)


WEB_PORT = 8080  # the web UI's, unless --port says otherwise


GREETS = ("pause", "always", "never")  # [recorder] greet


@dataclass
class AudioConfig:
    input_device: str = ""
    output_device: str = ""
    playback_prebuffer_s: float = 0.3
    # Take TARS's own sound out of what the mic hears, so the mic can stay open while TARS makes one (the optional
    # extra "echo": uv sync --extra echo).
    echo_cancel: bool = False


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
    # When to say "Yes, <name>?" after the wake word: "pause" (if nothing follows it for greet_after_s), "always" (at
    # once), or "never".
    greet: str = "pause"
    end_of_turn: str = "smart"  # silence: end_silence_s ends it | smart: a model may extend it to max_pause_s
    max_pause_s: float = 1.6
    answer_early_s: float = 0.25  # start preparing the answer after this much silence; it plays only once you're done
    turn_model: str = "models/smart-turn-v3.2-cpu.onnx"


@dataclass
class STTConfig:
    provider: str = "openai"  # openai | deepgram (streams while you talk) | flux (Deepgram also ends your turn)
    model: str = OPENAI_STT_MODEL
    language: str = "en"
    deepgram_host: str = DEEPGRAM_HOST


@dataclass
class LLMConfig:
    model: str = "gpt-4.1-mini"
    # Answer first, on Groq, then on Cerebras while Groq can't; turns that need the web or the TARS page go to `model`.
    # Empty = not used; both empty = `model` answers all.
    groq_model: str = ""
    cerebras_model: str = ""
    service_tier: str = ""
    reasoning_effort: str = ""  # OpenAI's model's, and the quick model's unless quick_reasoning_effort is set
    quick_reasoning_effort: str = ""  # the quick model's (Groq's and Cerebras's); empty = reasoning_effort
    memory_minutes: float = 10.0
    system_prompt: str = "You are a helpful voice assistant. Answer in one to three short sentences."
    humor: int = 75  # percent; "{humor}" in the system prompt is replaced with it
    # Let it look things up on the web (current events, real links to send). Only used when the model decides to.
    web_search: bool = True
    # Where the household is: questions about the weather, opening hours and the like are about there. Told to the
    # models and the web search. Empty = not said (OpenAI's model then guesses, or asks for a city).
    home_city: str = ""
    home_country: str = ""  # a two-letter country code, like IL or GB
    # Let it send links, notes, lists and text files to the web UI (needs [learning] log_events).
    send: bool = True
    # The quick model (Groq or Cerebras) sets reminders and sends notes, lists and files itself, with its own tools,
    # instead of handing those turns to `model`. Off until tools/capability_bench.py shows it gets them right.
    quick_tools: bool = False
    # Questions that need real thinking go from the quick model to `think_model` (empty = `model`) at this reasoning
    # effort, for at most think_timeout_s. Empty think_effort = no such hand-off.
    think_model: str = ""
    think_effort: str = "high"
    think_timeout_s: float = 45.0


@dataclass
class TTSConfig:
    provider: str = "openai"  # openai | deepgram (Flux TTS or Aura-2, by the model's name) | cartesia (Sonic)
    model: str = "gpt-4o-mini-tts"
    voice: str = "alloy"
    instructions: str = ""
    effect: str = ""
    deepgram_host: str = DEEPGRAM_HOST
    cartesia_voice: str = ""


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
    # A reminder or message that waits for an acknowledgement is said again this often, at most this many times, then
    # it's missed.
    repeat_every_min: float = 5.0
    max_tries: int = 4
    # A timer rings until someone turns it off, for at most this long.
    timer_ring_min: float = 15.0
    # After saying one, how long TARS listens for "got it" without the wake word.
    ack_window_s: float = 6.0


@dataclass
class TelegramConfig:
    # Chat with TARS on Telegram (docs/telegram.md): voice-assistant --telegram, with TELEGRAM_BOT_TOKEN in .env.
    enabled: bool = False
    # Who may: each person's name, as TARS knows them, and their Telegram chat id. Anyone else is ignored.
    people: dict[str, int] = field(default_factory=dict)
    # Things TARS sends at home for someone here ("send me the recipe") also come to their chat.
    send_items: bool = True
    # A chat is one conversation until it's been quiet this long (or someone sends /new); then the next message
    # starts a new one. Longer than [llm] memory_minutes, since a written reply often comes back much later.
    memory_minutes: float = 60.0
    # Who answers a chat: "openai" ([llm] model, which has web search and sends links; Qwen if there's no OpenAI key)
    # or "quick" (Qwen first, handing over as by voice). A chat is read whole, so Qwen's faster start buys nothing.
    brain: str = "openai"
    # OpenAI's settings for chats: the standard tier (no one's waiting on a first word) and a little thinking.
    service_tier: str = ""
    reasoning_effort: str = "low"


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
    telegram: TelegramConfig


# This machine's own settings, next to config.toml and never committed (.gitignore): the audio devices' names, who's
# on Telegram. Each setting in it replaces config.toml's, so the shared file stays generic and `git pull` never
# meets a local edit.
LOCAL_CONFIG = "config.local.toml"


def load_config(path: Path, local: bool = True) -> Config:
    """config.toml's settings, with this machine's from config.local.toml beside it on top (`local`; the tests read
    the shipped file alone)."""
    raw = _read(path)
    where = str(path)
    local_path = path.with_name(LOCAL_CONFIG)
    if local and path.name != LOCAL_CONFIG and local_path.exists():
        for name, values in _read(local_path).items():
            both = isinstance(values, dict) and isinstance(raw.get(name), dict)
            raw[name] = {**raw[name], **values} if both else values
        where = f"{path} (with {local_path.name})"
    sections = {f.name: f.type for f in dataclasses.fields(Config)}
    problems = [f"there's no [{name}] section" for name in raw if name not in sections]
    cfg = Config(**{name: _section(kind, name, raw.get(name, {}), problems) for name, kind in sections.items()})
    problems = problems or _problems(cfg)  # the checks between settings assume each one is the right type
    if problems:
        raise ConfigError(f"{where}: " + "; ".join(problems))
    return cfg


def _read(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text()) if path.exists() else {}
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None


def with_keys(cfg: Config, keys: set[str]) -> tuple[Config, list[str]]:
    """config.toml's setup, cut down to the services there are API keys for (`keys`, the names set in .env): (the setup
    that runs, a line for each thing done differently). Every paid service is optional, as long as each stage has one:
    a missing quick provider is skipped, Deepgram's stages fall to OpenAI's and OpenAI's to Deepgram's, and without
    OpenAI Qwen answers everything itself."""
    stt, tts, llm, notes = cfg.stt, cfg.tts, cfg.llm, []
    openai, deepgram = OPENAI_KEY in keys, DEEPGRAM_KEY in keys
    if not (openai or deepgram):
        raise ConfigError(f"Hearing and speaking need {DEEPGRAM_KEY} or {OPENAI_KEY} in .env (see .env.example).")
    if stt.provider != "openai" and not deepgram:
        stt = dataclasses.replace(stt, provider="openai", model=OPENAI_STT_MODEL)
        notes.append(f"no {DEEPGRAM_KEY}: OpenAI transcribes, once you've stopped talking")
    elif stt.provider == "openai" and not openai:
        english = stt.language.lower().startswith("en")
        stt = dataclasses.replace(stt, provider="flux" if english else "deepgram", model=DEEPGRAM_STT_MODEL)
        notes.append(f"no {OPENAI_KEY}: Deepgram transcribes")
    if tts.provider == "cartesia" and CARTESIA_KEY not in keys:
        if deepgram:
            tts = dataclasses.replace(tts, provider="deepgram", model=DEEPGRAM_VOICE)
            notes.append(f"no {CARTESIA_KEY}: Deepgram's {DEEPGRAM_VOICE} voice speaks")
        else:
            tts = dataclasses.replace(tts, provider="openai", model=TTSConfig.model)
            notes.append(f"no {CARTESIA_KEY}: OpenAI's {tts.voice} voice speaks")
    elif tts.provider == "deepgram" and not deepgram:
        tts = dataclasses.replace(tts, provider="openai", model=TTSConfig.model)
        notes.append(f"no {DEEPGRAM_KEY}: OpenAI's {tts.voice} voice speaks")
    elif tts.provider == "openai" and not openai:
        tts = dataclasses.replace(tts, provider="deepgram", model=DEEPGRAM_VOICE)
        notes.append(f"no {OPENAI_KEY}: Deepgram's {DEEPGRAM_VOICE} voice speaks")
    for provider in QUICK_PROVIDERS:
        if getattr(llm, provider.setting) and provider.key_name not in keys:
            llm = dataclasses.replace(llm, **{provider.setting: ""})
            notes.append(f"no {provider.key_name}: Qwen doesn't run on {provider.name}")
    if not openai:
        if not any(getattr(llm, provider.setting) for provider in QUICK_PROVIDERS):
            names = ", ".join(provider.key_name for provider in QUICK_PROVIDERS)
            raise ConfigError(f"The brain needs {OPENAI_KEY}, or one of {names} with its [llm] model set.")
        llm = dataclasses.replace(llm, web_search=False, think_effort="", send=llm.send and llm.quick_tools)
        notes.append(f"no {OPENAI_KEY}: Qwen answers everything, with no web search, thinking model or backup")
    return dataclasses.replace(cfg, stt=stt, tts=tts, llm=llm), notes


_KINDS = {
    str: "text",
    bool: "true or false",
    int: "a whole number",
    float: "a number",
    list: "a list",
    dict: "a table, like { Alon = 123 }",
}


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
    if typing.get_origin(kind) is dict:
        keys, values = typing.get_args(kind)
        return isinstance(value, dict) and all(isinstance(k, keys) and _fits(v, values) for k, v in value.items())
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
    for name, section in (("stt", stt), ("tts", cfg.tts)):
        if not re.fullmatch(r"[a-z0-9.-]+", section.deepgram_host):
            problems.append(f"[{name}] deepgram_host must be a host name, like api.eu.deepgram.com")
    if cfg.recorder.greet not in GREETS:
        problems.append(f"[recorder] greet must be {', '.join(GREETS)}, not {cfg.recorder.greet!r}")
    if cfg.llm.think_timeout_s <= 0:
        problems.append("[llm] think_timeout_s must be more than 0")
    if cfg.llm.home_country and not re.fullmatch(r"[A-Z]{2}", cfg.llm.home_country):
        problems.append(f"[llm] home_country must be a two-letter country code, like IL, not {cfg.llm.home_country!r}")
    if cfg.tts.provider not in ("openai", "deepgram", "cartesia"):
        problems.append(f"[tts] provider must be openai, deepgram or cartesia, not {cfg.tts.provider!r}")
    elif cfg.tts.provider == "cartesia" and not cfg.tts.model.startswith("sonic"):
        problems.append(f"[tts] model must be a Cartesia model (sonic-...), not {cfg.tts.model!r}")
    elif cfg.tts.provider == "cartesia" and not cfg.tts.cartesia_voice:
        problems.append("[tts] cartesia_voice must be a Cartesia voice id")
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
    if r.max_pause_s < r.end_silence_s:
        problems.append("[recorder] max_pause_s can't be shorter than end_silence_s")
    if cfg.reminders.repeat_every_min < 0.5:
        problems.append("[reminders] repeat_every_min must be at least 0.5")
    if not 1 <= cfg.reminders.max_tries <= 30:
        problems.append("[reminders] max_tries must be between 1 and 30")
    if not 1 <= cfg.reminders.timer_ring_min <= 60:
        problems.append("[reminders] timer_ring_min must be between 1 and 60")
    if not 2 <= cfg.reminders.ack_window_s <= 30:
        problems.append("[reminders] ack_window_s must be between 2 and 30")
    if cfg.telegram.enabled and not cfg.telegram.people:
        problems.append("[telegram] people must say who may chat with TARS, like { Alon = 123456789 }")
    if any(not name.strip() for name in cfg.telegram.people):
        problems.append("[telegram] people needs a name for each chat id")
    if cfg.telegram.memory_minutes <= 0:
        problems.append("[telegram] memory_minutes must be more than 0")
    if cfg.telegram.brain not in ("openai", "quick"):
        problems.append(f"[telegram] brain must be openai or quick, not {cfg.telegram.brain!r}")
    if len(set(cfg.telegram.people.values())) < len(cfg.telegram.people):
        problems.append("[telegram] people has the same chat id twice")
    return problems
