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
AWS_KEY_ID, AWS_SECRET = "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"  # an IAM user's key, for [backup]
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
    # extra "echo": uv sync --all-extras).
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


# The Home Assistant domains TARS works ([[home.devices]] entity = "<domain>.<id>"); sensors are only read.
HOME_DOMAINS = ("lock", "switch", "light", "fan", "input_boolean", "cover", "climate", "media_player")
HOME_SENSORS = ("sensor", "binary_sensor")
# A device worked over ADB rather than through Home Assistant ([[home.devices]] kind = "androidtv").
ANDROID_TV = "androidtv"
EVERYTHING = "everything"  # what the home tool calls all the devices at once, for their status
_ENTITY = re.compile(r"[a-z_]+\.[a-z0-9_]+")
_SERVICE = re.compile(r"[a-z_]+\.[a-z0-9_]+")
_MODULE = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")


@dataclass
class HomeConfig:
    # Home Assistant on the home network (docs/home.md), whose token is HOME_ASSISTANT_TOKEN in .env. Empty = none.
    url: str = ""
    # Who may unlock by voice, by their names in the Voices list. Empty = nobody.
    unlock_voices: list[str] = field(default_factory=list)
    # How sure speaker ID must be that it's them, to unlock (0-1): stricter than [speaker] threshold.
    unlock_threshold: float = 0.6
    # The model that works an Android TV step by step, reading its screen. Empty = [llm] model.
    tv_model: str = ""
    # Code of your own for devices Home Assistant doesn't work: a name, and { module = "...", and its own settings }.
    plugins: dict[str, dict] = field(default_factory=dict)
    # [[home.devices]]: each thing TARS may work, by the name people call it (docs/home.md).
    devices: list[dict] = field(default_factory=list)


# The routers presence.py can ask ([[presence.routers]] kind); routers.py has one class for each.
TECHNICOLOR, XIAOMI = "technicolor", "xiaomi"
ROUTER_KINDS = (TECHNICOLOR, XIAOMI)
_MAC = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
_DASHED = re.compile(r"[0-9a-f]{1,2}(-[0-9a-f]{1,2}){3,}")
_SECRET = re.compile(r"[A-Z][A-Z0-9_]*")


@dataclass
class PresenceConfig:
    # Who's home, from whose phone is on the home Wi-Fi, as the routers see it (docs/presence.md). Runs in the
    # assistant; needs [learning] log_events, where it's kept for the other processes.
    enabled: bool = False
    # Each person, and their phone as the routers know it: hardware addresses, or names a router lists it under.
    people: dict[str, list[str]] = field(default_factory=dict)
    # [[presence.routers]]: each router to ask, { kind = "technicolor" | "xiaomi", address, user, secret }, secret
    # being the name of its password in .env.
    routers: list[dict] = field(default_factory=list)
    # How often the routers are asked: well within presence.STALE_AFTER_S, after which what was last written isn't
    # trusted.
    every_s: float = 30.0
    # Someone whose phone no router has seen for this long has gone out. A phone asleep on a table drops off Wi-Fi
    # now and then, for a few minutes, so this is longer than that.
    away_after_min: float = 10.0


@dataclass
class BackupConfig:
    # Back up what TARS keeps to an Amazon S3 bucket (docs/backup.md): voice-assistant --backup, every 15 minutes by
    # its systemd timer, with AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in .env.
    enabled: bool = False
    bucket: str = ""
    # The AWS region the bucket is in, like eu-central-1.
    region: str = ""
    # Everything goes under this folder in the bucket: audio/, files/ and snapshots/.
    prefix: str = "tars"
    # A recording stays on the machine this long, then only in the bucket (the web UI still plays it). What training
    # learns from, each voice's newest requests, and what TARS sent stay on the machine too. 0 = keep everything here.
    keep_local_days: float = 30.0
    # Move the audio to Glacier Deep Archive this many days after it's made: cheaper storage, but up to 48 hours to
    # bring back, and at a household's few GB a year the moves cost about what it saves. 0 = never.
    archive_after_days: int = 0
    # A copy of the database, the voiceprints, the trained wake models and config.local.toml is made every six hours,
    # and kept this long; the newest always stays.
    keep_snapshots_days: int = 30


@dataclass
class BrowserConfig:
    # TARS's own web browser, for errands asked in a Telegram chat: ordering, booking, looking something up on a site
    # (docs/browser.md). Needs [telegram], the browser extra (uv sync --extra browser) and OPENAI_API_KEY.
    enabled: bool = False
    # The model that works the browser, and how hard it thinks: Luna at high effort did every hard task in the evals.
    model: str = "gpt-6-luna"
    effort: str = "high"
    # The most turns an errand takes before it stops and says where it got to.
    turns: int = 40
    # Press the button that places an order once the person approves it on Telegram. Off: an approved order is a dry
    # run, and nothing is ordered.
    really_order: bool = False
    # Where the one-time page for a login TARS lacks is served, for the person's phone: this machine's address on the
    # home network or Tailscale ("": its Tailscale address if it has one, else its address at home).
    login_host: str = ""
    login_port: int = 8091


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
    home: HomeConfig
    presence: PresenceConfig
    backup: BackupConfig
    browser: BrowserConfig


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
    if not 0 < cfg.wake.threshold < 1:
        problems.append("[wake] threshold must be more than 0 and less than 1")
    if cfg.wake.check_window_s <= 0:
        problems.append("[wake] check_window_s must be more than 0")
    for name in ("threshold", "wake_threshold"):
        if not 0 < getattr(cfg.speaker, name) <= 1:
            problems.append(f"[speaker] {name} must be more than 0 and at most 1")
    for name in ("start_timeout_s", "max_utterance_s"):
        if getattr(r, name) <= 0:
            problems.append(f"[recorder] {name} must be more than 0")
    for name in ("follow_up_s", "greet_after_s"):
        if getattr(r, name) < 0:
            problems.append(f"[recorder] {name} must be 0 (off) or more")
    if not 0 <= cfg.llm.humor <= 100:
        problems.append("[llm] humor must be a percentage, from 0 to 100")
    if cfg.llm.memory_minutes < 0:
        problems.append("[llm] memory_minutes must be 0 or more")
    if r.greet not in GREETS:
        problems.append(f"[recorder] greet must be {', '.join(GREETS)}, not {r.greet!r}")
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
    w = cfg.browser
    if w.enabled and not cfg.telegram.enabled:
        problems.append("[browser] needs [telegram]: errands are asked for, approved and answered in a chat")
    if w.effort not in ("minimal", "low", "medium", "high", "xhigh"):
        problems.append(f"[browser] effort must be minimal, low, medium, high or xhigh, not {w.effort!r}")
    if not 5 <= w.turns <= 100:
        problems.append("[browser] turns must be between 5 and 100")
    if not 1024 <= w.login_port <= 65535:
        problems.append("[browser] login_port must be between 1024 and 65535")
    b = cfg.backup
    if b.enabled and not b.bucket:
        problems.append("[backup] bucket must name the S3 bucket to back up to")
    if b.enabled and not b.region:
        problems.append("[backup] region must say where the bucket is, like eu-central-1")
    if b.bucket and not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", b.bucket):
        problems.append(f"[backup] bucket must be an S3 bucket name, not {b.bucket!r}")
    if b.region and not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", b.region):
        problems.append(f"[backup] region must be an AWS region like eu-central-1, not {b.region!r}")
    if not re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*", b.prefix):
        problems.append(f"[backup] prefix must be a folder like tars, with no slash at either end, not {b.prefix!r}")
    if b.keep_local_days < 0:
        problems.append("[backup] keep_local_days must be 0 (keep everything) or more")
    if b.archive_after_days < 0:
        problems.append("[backup] archive_after_days must be 0 (never) or more")
    if b.keep_snapshots_days < 1:
        problems.append("[backup] keep_snapshots_days must be at least 1")
    return problems + _home_problems(cfg.home) + _presence_problems(cfg.presence)


def _presence_problems(p: PresenceConfig) -> list[str]:
    """What's wrong with [presence]: whose phone is whose, and each router's kind, address and password's name."""
    problems: list[str] = []
    if p.enabled and not p.people:
        problems.append('[presence] people must say whose phone is whose, like { Alon = ["aa:bb:cc:dd:ee:ff"] }')
    if p.enabled and not p.routers:
        problems.append("[presence] needs a [[presence.routers]] to ask")
    keys: set[str] = set()
    for name, ids in p.people.items():
        if not name.strip():
            problems.append("[presence] people needs a name for each list")
        elif name.strip().lower() in keys:
            problems.append(f"[presence] people has {name!r} twice")
        keys.add(name.strip().lower())
        if not ids or any(not i.strip() for i in ids):
            problems.append(f"[presence] people {name!r} needs their phone's address or name, none empty")
        for i in ids:
            meant = ":" in i or _DASHED.fullmatch(i.strip().lower())  # an address, not a name like Alons-iPhone
            if meant and not _MAC.fullmatch(i.strip().lower().replace("-", ":")):
                problems.append(f"[presence] people {name!r}: {i!r} isn't a hardware address like aa:bb:cc:dd:ee:ff")
    addresses: set[str] = set()
    for i, r in enumerate(p.routers, 1):
        where = f"[[presence.routers]] #{i}"
        if r.get("kind") not in ROUTER_KINDS:
            problems.append(f"{where}: kind must be {' or '.join(ROUTER_KINDS)}, not {r.get('kind')!r}")
        if not (isinstance(r.get("address"), str) and re.fullmatch(r"[A-Za-z0-9.-]+", r["address"])):
            problems.append(f"{where} needs address, its IP address on the home network, like 192.168.1.1")
        elif r["address"] in addresses:
            problems.append(f"{where}: two routers have the address {r['address']}")
        else:
            addresses.add(r["address"])
        if not (isinstance(r.get("secret"), str) and _SECRET.fullmatch(r["secret"])):
            problems.append(f'{where} needs secret, the name of its password in .env, like "ROUTER_PASSWORD"')
        if "user" in r and not (isinstance(r["user"], str) and r["user"].strip()):
            problems.append(f"{where}: user must be the admin's name, like admin")
        problems += [f"{where} has no setting {k}" for k in r if k not in ("kind", "address", "user", "secret")]
    if not 5 <= p.every_s <= 120:
        problems.append("[presence] every_s must be between 5 and 120")
    if not 1 <= p.away_after_min <= 240:
        problems.append("[presence] away_after_min must be between 1 and 240")
    return problems


def _home_problems(home: HomeConfig) -> list[str]:
    """What's wrong with [home]. A plugin's devices are checked by the plugin when they're made; here only that
    the plugin is named."""
    problems: list[str] = []
    if home.url and not re.fullmatch(r"https?://[^\s/]+(:\d+)?/?", home.url):
        problems.append(f"[home] url must be an address like http://homeassistant.local:8123, not {home.url!r}")
    if not 0 < home.unlock_threshold <= 1:
        problems.append("[home] unlock_threshold must be more than 0 and at most 1")
    if any(not v.strip() for v in home.unlock_voices):
        problems.append("[home] unlock_voices has an empty name")
    for name, plugin in home.plugins.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            problems.append(f"[home.plugins] {name!r}: a name is lowercase letters, digits and _")
        if not (isinstance(plugin.get("module"), str) and _MODULE.fullmatch(plugin["module"])):
            problems.append(f'[home.plugins.{name}] needs module, the Python module to load, like "my_house"')
    names: set[str] = set()
    for i, d in enumerate(home.devices, 1):
        name = d.get("name")
        where = f"[[home.devices]] {name!r}" if isinstance(name, str) and name.strip() else f"[[home.devices]] #{i}"
        if not (isinstance(name, str) and name.strip()):
            problems.append(f"{where} needs a name")
        elif name.strip().lower() == EVERYTHING:
            problems.append(f"{where}: {EVERYTHING!r} is the name for every device at once")
        elif name.strip().lower() in names:
            problems.append(f"{where}: two devices have that name")
        else:
            names.add(name.strip().lower())
        kind = d.get("kind")
        if kind is None:
            problems += _entity_problems(home, d, where)
        elif kind == ANDROID_TV:
            if not (isinstance(d.get("address"), str) and d["address"].strip()):
                problems.append(f"{where} ({ANDROID_TV}) needs address, its IP address on the home network")
            extra = [k for k in d if k not in ("name", "kind", "address")]
            problems += [f"{where} ({ANDROID_TV}) has no setting {k}" for k in extra]
        elif not (isinstance(kind, str) and kind.partition(".")[0] in home.plugins and kind.partition(".")[2]):
            problems.append(
                f"{where}: kind must be {ANDROID_TV} or a plugin's, like my_house.ac, with the plugin under "
                f"[home.plugins]; for a Home Assistant entity leave kind out (not {kind!r})"
            )
    return problems


def _entity_problems(home: HomeConfig, d: dict, where: str) -> list[str]:
    """A device worked through Home Assistant: its entity, and the settings its domain may have."""
    entity, problems = d.get("entity"), []
    if not (isinstance(entity, str) and _ENTITY.fullmatch(entity)):
        return [f"{where} needs entity, Home Assistant's id for it, like lock.front_door (or a kind)"]
    domain = entity.partition(".")[0]
    if domain not in HOME_DOMAINS + HOME_SENSORS:
        kinds = ", ".join(HOME_DOMAINS + HOME_SENSORS)
        problems.append(f"{where}: TARS works {kinds}; not {domain}")
    if not home.url:
        problems.append(f"{where} goes through Home Assistant: set [home] url")
    allowed = {"name", "entity", "details"}
    allowed |= {"door", "battery"} if domain == "lock" else set()
    allowed |= {"timer"} if domain in ("switch", "light", "fan", "input_boolean") else set()
    problems += [f"{where} ({domain}) has no setting {k}" for k in d if k not in allowed]
    for key, domains in (("door", ("binary_sensor",)), ("battery", ("sensor",))):
        value = d.get(key)
        if key in d and not (isinstance(value, str) and _ENTITY.fullmatch(value) and value.split(".")[0] in domains):
            problems.append(f"{where}: {key} must be a {domains[0]}.* entity, not {value!r}")
    details = d.get("details", [])
    if not (isinstance(details, list) and all(isinstance(e, str) and _ENTITY.fullmatch(e) for e in details)):
        problems.append(f'{where}: details must be a list of entity ids, like ["sensor.boiler_time_left"]')
    if "timer" in d:
        t = d["timer"]
        fine = (
            isinstance(t, dict)
            and set(t) == {"service", "field", "max"}
            and isinstance(t["service"], str)
            and _SERVICE.fullmatch(t["service"])
            and isinstance(t["field"], str)
            and t["field"].strip()
            and isinstance(t["max"], int)
            and not isinstance(t["max"], bool)
            and 1 <= t["max"] <= 24 * 60
        )
        if not fine:
            problems.append(
                f'{where}: timer must be {{ service = "<domain>.<service>", field = "<its minutes field>", '
                "max = <minutes, up to 1440> }"
            )
    return problems
