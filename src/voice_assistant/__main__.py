"""The command line: `voice-assistant` runs TARS; its options record voices, enroll speakers, test the mic, serve the
web UI and install trained wake models. Heavy dependencies are imported lazily, so each command loads only its own."""

import argparse
import dataclasses
import functools
import hashlib
import json
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

from .audio import SAMPLE_RATE, AudioDeviceError, Microphone, NoDeviceError, Speaker, find_device, list_devices
from .config import (
    AWS_KEY_ID,
    AWS_SECRET,
    CARTESIA_KEY,
    DEEPGRAM_KEY,
    LOCAL_CONFIG,
    OPENAI_KEY,
    OPENAI_STT_MODEL,
    QUICK_PROVIDERS,
    TELEGRAM_KEY,
    WEB_PORT,
    Config,
    ConfigError,
    HomeConfig,
    LLMConfig,
    QuickProvider,
    load_config,
    with_keys,
)
from .recorder import make_recorder
from .store import DB_FILE
from .versions import UNUSABLE, model_versions, pair_source
from .wake import PushToTalkTrigger, wake_word_trigger

if TYPE_CHECKING:
    from collections.abc import Callable

    from .backup import S3, Backup
    from .events import EventLog
    from .health import Report
    from .llm import QuickService
    from .stt import Transcriber

OPENAI_TIMEOUT_S = 15.0
QUICK_TIMEOUT_S = 4.0  # the quick brain's replies take well under a second
PHRASES = "voice_data/phrases"  # the short lines TARS makes ahead, kept across restarts
# sysexits' EX_CONFIG: a setup that can't work, which systemd mustn't keep restarting (RestartPreventExitStatus).
EX_CONFIG = 78
# sysexits' EX_UNAVAILABLE: no speakerphone. Not restarted either; plugging it in starts TARS (the udev rule).
EX_UNAVAILABLE = 69


def api_key(env_path: Path, name: str) -> str:
    # Keys come from this project's .env only, never the shell environment, so the assistant can't silently pick up
    # a key meant for something else.
    key = dotenv_values(env_path).get(name)
    if not key:
        raise ConfigError(f"Put {name} in {env_path} (see .env.example).")
    return key


def env_keys(env_path: Path) -> set[str]:
    return {name for name, key in dotenv_values(env_path).items() if key} if env_path.exists() else set()


def keyed(cfg: Config, root: Path) -> Config:
    """config.toml's setup, cut down to the services .env has keys for, saying what runs differently."""
    cfg, notes = with_keys(cfg, env_keys(root / ".env"))
    for note in notes:
        print(f"({note})")
    return cfg


def make_openai_client(env_path: Path):
    from openai import OpenAI

    key = api_key(env_path, OPENAI_KEY)
    # The SDK default is 10 minutes per request plus 2 retries. A voice assistant should give up quickly, so a dropped
    # connection costs one "that didn't work" rather than a frozen assistant.
    return OpenAI(api_key=key, timeout=OPENAI_TIMEOUT_S, max_retries=1)


def make_quick_client(env_path: Path, provider: QuickProvider):
    from openai import OpenAI, Timeout

    # No retries and a short wait: when one is slow or says "too many requests", the next one answers instead.
    return OpenAI(
        base_url=provider.url,
        api_key=api_key(env_path, provider.key_name),
        timeout=Timeout(QUICK_TIMEOUT_S, connect=2.0),
        max_retries=0,
    )


def quick_services(llm: LLMConfig, env_path: Path) -> list[QuickService]:
    from .llm import QuickService

    return [
        QuickService(provider.name, make_quick_client(env_path, provider), model, provider.max_tokens)
        for provider in QUICK_PROVIDERS
        if (model := getattr(llm, provider.setting))
    ]


def make_event_log(cfg: Config, root: Path):
    if not cfg.learning.log_events:
        return None
    from .events import EventLog

    return EventLog(root / cfg.learning.folder)


def start_heartbeat(events: EventLog | None, process: str, root: Path) -> None:
    """The heartbeat the web UI's Status page reads for this process (health.py), kept with the event log."""
    if events is None:
        return
    from .health import Heartbeat

    Heartbeat(events.folder, process, root).start()


def load_echo_canceller():
    try:
        from .echo import EchoCanceller

        return EchoCanceller(SAMPLE_RATE)
    except ImportError:
        raise RuntimeError('needs the optional extra "echo": run uv sync --all-extras') from None


def make_echo_canceller(cfg: Config):
    """Takes TARS's own sound out of what the mic hears ([audio] echo_cancel), or None: without it, the mic closes
    while TARS speaks, as it always did."""
    if not cfg.audio.echo_cancel:
        return None
    try:
        return load_echo_canceller()
    except Exception as e:  # noqa: BLE001 - TARS works without it
        print(f"(echo cancellation is on but couldn't start, so the mic closes while TARS speaks: {e})")
        return None


def brain_config(cfg: Config, typed: bool) -> LLMConfig:
    """Sent items live in the event log, which typed questions (--text) don't use: without it, TARS mustn't say it
    sent anything."""
    return dataclasses.replace(cfg.llm, send=cfg.llm.send and cfg.learning.log_events and not typed)


def make_trigger(cfg: Config, push_to_talk: bool, root: Path, journal=None):
    if push_to_talk or cfg.wake.mode == "push-to-talk":
        return PushToTalkTrigger(), "Press Enter to talk."
    try:
        if cfg.wake.verify:
            from .verify import VerifiedTrigger

            trigger = VerifiedTrigger(pair_source(cfg, root), root / "models", journal=journal)
        else:
            trigger = wake_word_trigger(cfg.wake.model, cfg.wake.threshold)
    except (FileNotFoundError, ValueError) as e:  # a wake model or check that's missing or broken
        raise ConfigError(str(e)) from None
    return trigger, f"Say '{trigger.phrase}'..."


def record_voice_session(cfg: Config, person: str, mic_name: str, root: Path) -> None:
    from .enroll import record_voice

    print(
        f"Recording {person}'s voice on the {mic_name} mic. After each chime, say the prompt. "
        "Ctrl+C to stop; rerun to resume."
    )
    speaker = Speaker(find_device(cfg.audio.output_device, "output"), SAMPLE_RATE, cfg.audio.playback_prebuffer_s)
    with Microphone(find_device(cfg.audio.input_device, "input")) as mic, speaker:
        record_voice(person, mic_name, mic, speaker, make_recorder(cfg, root, turn_model=False), root / "voice_data")


def make_speaker_id(cfg: Config, root: Path):
    from .speaker import SpeakerID

    return SpeakerID(root / cfg.speaker.model, root / cfg.speaker.voiceprints, cfg.speaker.threshold)


def enroll_speaker(cfg: Config, person: str, mic_name: str, root: Path) -> None:
    from .audio import read_wav

    # Enroll from the mic the assistant will listen with: voiceprints don't transfer perfectly between mics.
    clips = sorted((root / "voice_data" / person / mic_name / "speech").glob("*.wav"))
    if len(clips) < 5:
        raise SystemExit(
            f"Need at least 5 sentences for {person} on the {mic_name} mic; "
            f"run --record-voice {person} --mic {mic_name} first."
        )
    speaker_id = make_speaker_id(cfg, root)
    speaker_id.enroll(person, [read_wav(c) for c in clips])
    print(
        f"Enrolled {person} from {len(clips)} sentences ({mic_name} mic). "
        f"Everyone enrolled: {', '.join(speaker_id.voiceprints)}"
    )
    if not cfg.speaker.enabled:
        print("Set enabled = true under [speaker] in config.toml to use it.")


def web_ui(cfg: Config, root: Path, host: str, port: int) -> None:
    from .clustering import regroup
    from .webui import serve

    log = make_event_log(cfg, root)
    if log is None:
        raise ConfigError("The web UI shows the event log: set log_events = true under [learning] in config.toml.")
    speaker_id = make_speaker_id(cfg, root) if cfg.speaker.enabled else None
    try:
        pairs = pair_source(cfg, root)
    except (FileNotFoundError, ValueError) as e:
        raise ConfigError(str(e)) from None
    from .reminders import Reminders

    reminders = Reminders.from_config(log.store, cfg.reminders) if cfg.reminders.enabled else None
    names = speaker_id.names if speaker_id else list
    presence = make_presence(cfg, root, log)
    # Someone known by their phone can be left a message for when they're back, here too.
    voices = (lambda: [*names(), *presence.phones]) if presence else names
    start_heartbeat(log, "web", root)
    bucket = None
    if cfg.backup.enabled:
        try:
            bucket = make_backup(cfg, root, log).read
        except ConfigError as e:
            print(f"(the web UI plays only what's on this machine: {e.code})")
    serve(
        log,
        host,
        port,
        recluster=functools.partial(regroup, log, speaker_id),
        pairs=pairs,
        allowed_hosts=frozenset(cfg.web.allowed_hosts),
        reminders=reminders,
        voice_names=voices,
        health=make_health(cfg, root, log),
        bucket=bucket,
    )


def make_health(cfg: Config, root: Path, log: EventLog) -> Callable[[], Report]:
    """What the web UI's Status page shows, as it is when asked (health.py)."""
    from .check import service_checks, systemd_state
    from .health import Health, Services, expected

    services = Services(lambda: service_checks(cfg, root).listed())
    return Health(log, expected(cfg), services, systemd_state, backup_enabled=cfg.backup.enabled).report


def install_models(cfg: Config, root: Path, folder: Path, based_on: str | None) -> None:
    """Adds a pair training.household made and puts it in use; a running assistant switches within seconds."""
    versions = model_versions(cfg, root)
    if versions is None:
        raise ConfigError(
            "Trained wake models need a microWakeWord model (.tflite) with a learned double-check: set "
            "[wake] model, verify and check_model in config.toml."
        )
    try:
        name = versions.install(folder, based_on)
    except UNUSABLE as e:
        raise SystemExit(f"Not installed: {e}") from None
    print(f"Installed {folder} as {name}; it's in use now. The web UI's Models page can switch back.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="voice-assistant")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--list-devices", action="store_true", help="show audio devices and exit")
    parser.add_argument(
        "--check", action="store_true", help="check everything TARS needs (keys, devices, services, models) and exit"
    )
    parser.add_argument("--mic-test", action="store_true", help="live mic / VAD / wake-word meter (no API key)")
    parser.add_argument("--ptt", action="store_true", help="push-to-talk instead of wake word")
    parser.add_argument("--text", action="store_true", help="type questions instead of speaking them")
    parser.add_argument("--record-voice", metavar="NAME", help="guided recording of NAME's voice into voice_data/")
    parser.add_argument("--enroll", metavar="NAME", help="build NAME's voiceprint from their recorded sentences")
    parser.add_argument(
        "--mic", default="laptop", help="which mic a --record-voice/--enroll session is for (default: laptop)"
    )
    parser.add_argument(
        "--web", action="store_true", help="the web UI: conversations, what TARS sent, wakes to check, voices"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="web UI address (0.0.0.0 = reachable from the home network)"
    )
    parser.add_argument("--port", type=int, default=WEB_PORT)
    parser.add_argument(
        "--telegram", action="store_true", help="chat with TARS on Telegram ([telegram] in config.toml)"
    )
    backup = parser.add_mutually_exclusive_group()
    backup.add_argument(
        "--backup", action="store_true", help="back up to S3 once, as its timer does ([backup] in config.toml)"
    )
    backup.add_argument(
        "--fetch-audio",
        action="store_true",
        help="bring back the audio that's only in the S3 bucket (for training, or a new machine)",
    )
    parser.add_argument(
        "--restore-archived",
        action="store_true",
        help="with --fetch-audio: ask S3 to restore archived audio too, to fetch with a later run (up to 48 hours)",
    )
    parser.add_argument(
        "--delete-anyway",
        action="store_true",
        help="with --backup: delete from the bucket what TARS no longer keeps, even if it's most of it",
    )
    backup.add_argument(
        "--restore-backup", action="store_true", help="put the bucket's latest snapshot in place on a new machine"
    )
    parser.add_argument(
        "--install-models",
        type=Path,
        metavar="FOLDER",
        help="put a wake model and check made by training.household in use",
    )
    parser.add_argument(
        "--based-on", metavar="VERSION", help="with --install-models: only if this version is still the one in use"
    )
    args = parser.parse_args()
    if args.restore_archived and not args.fetch_audio:
        parser.error("--restore-archived goes with --fetch-audio")
    if args.delete_anyway and not args.backup:
        parser.error("--delete-anyway goes with --backup")

    if args.list_devices:
        list_devices()
        return

    root = args.config.resolve().parent
    try:
        cfg = load_config(args.config)
        if args.check:
            from .check import check_all

            raise SystemExit(check_all(cfg, root))
        if args.mic_test:
            from .mic_test import mic_test

            try:
                mic_test(cfg, root)
            except (FileNotFoundError, ValueError) as e:  # the wake model or check is missing or broken
                raise ConfigError(str(e)) from None
            return
        if args.record_voice:
            record_voice_session(cfg, args.record_voice, args.mic, root)
            return
        if args.enroll:
            enroll_speaker(cfg, args.enroll, args.mic, root)
            return
        if args.web:
            web_ui(cfg, root, args.host, args.port)
            return
        if args.telegram:
            telegram(keyed(cfg, root), root)
            return
        if args.backup:
            backup_pass(cfg, root, delete_anyway=args.delete_anyway)
            return
        if args.fetch_audio:
            fetch_audio(cfg, root, restore_archived=args.restore_archived)
            return
        if args.restore_backup:
            restore_backup(cfg, root)
            return
        if args.install_models:
            install_models(cfg, root, args.install_models, args.based_on)
            return
        if args.text:
            typed(keyed(cfg, root), root)
            return
        run(keyed(cfg, root), args.ptt, root)
    except KeyboardInterrupt:
        print("\nBye.")
    except ConfigError as e:
        print(e.code, file=sys.stderr)
        raise SystemExit(EX_CONFIG) from None
    except NoDeviceError as e:
        print(e, file=sys.stderr)
        raise SystemExit(EX_UNAVAILABLE) from None
    except AudioDeviceError as e:
        # Exit non-zero so a supervisor (systemd on the Pi) restarts the process once the device is back.
        raise SystemExit(str(e)) from None


def make_reminders(cfg: Config, events, push_to_talk: bool, speaker_id=None, presence=None):
    """Reminders need the event log to keep them in, and the wake loop to say them: not with push-to-talk, which
    waits on Enter instead. Waiting until someone's back needs their voiceprint, or their phone (`presence`)."""
    if not (cfg.reminders.enabled and events) or push_to_talk or cfg.wake.mode == "push-to-talk":
        return None
    from .reminders import Reminders

    voices = speaker_id.names if speaker_id else list
    return reminder_tools(Reminders.from_config(events.store, cfg.reminders), voices, presence)


def reminder_tools(reminders, voices, presence=None):
    """The brain's reminder tools, knowing people by `voices` (a callable) and, with `presence`, by their phone."""
    from .reminders import ReminderTools

    if presence is None:
        return ReminderTools(reminders, voices=voices)
    return ReminderTools(reminders, voices=voices, phones=lambda: presence.phones, is_home=presence.board.is_home)


def make_presence(cfg: Config, root: Path, events):
    """[presence] (presence.Presence), when it's on and there's an event log to keep who's home in; None otherwise.
    It only reads the board until the assistant's watch() asks the routers."""
    if not cfg.presence.enabled:
        return None
    if events is None:
        print("(presence: off: who's home is kept in the event log, so it needs [learning] log_events)")
        return None
    from .presence import make_presence as presence_from

    env = root / ".env"
    return presence_from(cfg.presence, events.store, lambda name: dotenv_values(env).get(name) or None)


def start_presence(presence, reminders=None) -> None:
    """The assistant's: asks the routers from now on, and says waiting messages once their person is home."""
    if presence is None:
        return
    from .reminders import on_arrival

    for line in presence.left_out:
        print(f"(presence: leaving out {line}; until it's back nobody is marked out; see docs/presence.md)")
    if presence.watch(on_arrival(reminders.reminders) if reminders else None):
        print(f"(presence: asking {len(presence.routers)} router(s) every {presence.every_s:.0f} s)")


def make_chat_when_out(cfg: Config, presence, reminders):
    """With [presence] and [telegram]: a reminder due aloud for someone who's out goes to their chat instead."""
    if not (presence and reminders and cfg.telegram.enabled and cfg.telegram.people):
        return None
    from .reminders import ChatWhenOut

    return ChatWhenOut(reminders.reminders, presence.board.is_out, cfg.telegram.people)


def make_home(cfg: Config, root: Path, announce=None, presence=None):
    """The devices in the house, as [home] lists them, with "who's home" given `presence` (a HomeTools), or None.
    Like every service, optional: one that can't be made (no token, a missing extra, a plugin's own problem) is left
    out with a line saying why. `announce(line)`: how TARS says something on its own, later (how a task on the TV
    went)."""
    if not (cfg.home.devices or presence):
        return None
    from .androidtv import make_tv_agent
    from .home import Context, build

    env = root / ".env"
    context = Context(root, lambda name: dotenv_values(env).get(name) or None, announce=announce)
    context.tv_agent = functools.partial(make_tv_agent, cfg, env, announce) if announce else None
    extra = {}
    if presence:
        from .presence import DEVICE, WhoIsHome

        extra[DEVICE] = WhoIsHome(presence.board, presence.away_after_min)
    built = build(cfg.home, context, extra)
    for name, why in built.unavailable.items():
        print(f"(home: the {name} isn't connected: {why}; see docs/home.md)")
    if built.tools:
        print(f"(home: the house has {', '.join(built.tools.devices)})")
    return built.tools


def make_pipeline(cfg: Config, root: Path, typed: bool = False, reminders=None, home=None):
    """The cloud stages, as config.toml picks them: (transcriber, brain, voice); no transcriber for typed questions.
    With `reminders` (a ReminderTools), the brain can set them and knows what's set; with `home` (a HomeTools), it
    can work the devices in the house."""
    from .effects import apply_effect
    from .stt import (
        DeepgramTranscriber,
        FallbackTranscriber,
        FluxTranscriber,
        OpenAITranscriber,
    )
    from .tts import CartesiaSpeech, DeepgramSpeech, OpenAISpeech

    env = root / ".env"
    keys = env_keys(env)
    cfg, _ = with_keys(cfg, keys)
    client = make_openai_client(env) if OPENAI_KEY in keys else None
    brain = make_brain(client, brain_config(cfg, typed=typed), env, reminders, home=home)
    if cfg.tts.provider == "deepgram":
        speech = DeepgramSpeech(api_key(env, DEEPGRAM_KEY), cfg.tts)
    elif cfg.tts.provider == "cartesia":
        speech = CartesiaSpeech(api_key(env, CARTESIA_KEY), cfg.tts)
    else:
        speech = OpenAISpeech(client, cfg.tts)
    voice = apply_effect(speech, cfg.tts.effect)
    if typed:
        return None, brain, voice
    if cfg.stt.provider == "openai":
        return OpenAITranscriber(client, cfg.stt), brain, voice
    key = api_key(env, DEEPGRAM_KEY)
    if cfg.stt.provider == "flux":
        streaming = FluxTranscriber(key, cfg.stt, has_backup=client is not None)
    else:
        streaming = DeepgramTranscriber(key, cfg.stt)
    if client is None:
        return streaming, brain, voice
    backup = OpenAITranscriber(client, dataclasses.replace(cfg.stt, model=OPENAI_STT_MODEL))
    return FallbackTranscriber(streaming, backup), brain, voice


def make_recording_transcriber(cfg: Config, env: Path, client) -> Transcriber:
    """Speech to text for a whole recording (a voice message), with the services make_pipeline picks for the voice at
    home: Deepgram's endpoint for whole recordings, with OpenAI's `client` as the backup, or whichever of the two
    there's a key for. `cfg` has been through with_keys."""
    from .stt import FILE_TIMEOUT_S, DeepgramFileTranscriber, FallbackTranscriber, OpenAITranscriber

    if client is not None:
        client = client.with_options(timeout=FILE_TIMEOUT_S)  # a few minutes of audio, not one request
    if cfg.stt.provider == "openai":
        return OpenAITranscriber(client, cfg.stt)
    deepgram = DeepgramFileTranscriber(api_key(env, DEEPGRAM_KEY), cfg.stt)
    if client is None:
        return deepgram
    return FallbackTranscriber(
        deepgram, OpenAITranscriber(client, dataclasses.replace(cfg.stt, model=OPENAI_STT_MODEL))
    )


def make_brain(client, llm: LLMConfig, env: Path, reminders=None, **channel):
    """Qwen first, handing over to OpenAI's `client`, or OpenAI alone when no quick service is set up. `channel`:
    a chat's channel_rules and remind_tools, or the house's home (see OpenAIChat)."""
    from .llm import OpenAIChat, QuickChat

    quick = quick_services(llm, env)
    if quick:
        return QuickChat(client, llm, quick, reminders, **channel)
    return OpenAIChat(client, llm, reminders, **channel)


def chat_brain_config(cfg: Config, has_openai: bool) -> LLMConfig:
    """The brain's settings for a Telegram chat: [telegram]'s memory, and OpenAI's model alone, at [telegram]'s tier
    and reasoning effort, unless [telegram] brain is "quick" or there's no OpenAI key."""
    llm = dataclasses.replace(brain_config(cfg, typed=False), memory_minutes=cfg.telegram.memory_minutes)
    if cfg.telegram.brain != "openai" or not has_openai:
        return llm
    quick_off = {provider.setting: "" for provider in QUICK_PROVIDERS}
    tg = cfg.telegram
    return dataclasses.replace(llm, service_tier=tg.service_tier, reasoning_effort=tg.reasoning_effort, **quick_off)


def telegram(cfg: Config, root: Path) -> None:
    """--telegram: TARS in the household's Telegram chats, a process of its own, like the web UI."""
    from .conversations import ConversationLog
    from .reminders import Reminders
    from .speaker import voiceprint_names
    from .telegram import CHAT_REMIND_TOOLS, Bot, TelegramError, TelegramTars, channel_rules, refused_token_error

    if not cfg.telegram.enabled:
        raise ConfigError(
            "Set enabled = true under [telegram] in config.toml, and say who may chat in people (docs/telegram.md)."
        )
    env = root / ".env"
    bot = Bot(api_key(env, TELEGRAM_KEY))
    try:
        me = bot.me()
    except TelegramError as e:
        if e.refused:
            raise refused_token_error(env) from None
        raise SystemExit(f"Can't reach Telegram: {e}") from None  # systemd tries again
    events = make_event_log(cfg, root)
    start_heartbeat(events, "telegram", root)
    presence = make_presence(cfg, root, events)
    tools = None
    if cfg.reminders.enabled and events:
        voiceprints = root / cfg.speaker.voiceprints
        names = (lambda: voiceprint_names(voiceprints)) if cfg.speaker.enabled else list
        tools = reminder_tools(Reminders.from_config(events.store, cfg.reminders), names, presence)
    # A chat works nothing in the house, but can ask who's home.
    home = make_home(dataclasses.replace(cfg, home=HomeConfig()), root, presence=presence)
    client = make_openai_client(env) if OPENAI_KEY in env_keys(env) else None
    llm, rules = chat_brain_config(cfg, client is not None), channel_rules(cfg.telegram.people)
    who = "OpenAI's " + llm.model if not any(getattr(llm, p.setting) for p in QUICK_PROVIDERS) else "Qwen first"
    print(f"(chats are answered by {who})")
    errands = make_errands(cfg, client is not None)
    tars = TelegramTars(
        bot,
        cfg.telegram.people,
        lambda: make_brain(
            client, llm, env, tools, channel_rules=rules, remind_tools=CHAT_REMIND_TOOLS, home=home, browser=errands
        ),
        ConversationLog(events) if events else None,
        tools,
        memory_s=llm.memory_minutes * 60,
        send_items=cfg.telegram.send_items,
        transcriber=make_recording_transcriber(cfg, env, client),
        env=env,
        login_at=(cfg.browser.login_host, cfg.browser.login_port),
    )
    if errands:
        errands.chats = tars  # what an errand needs of the person comes to their chat
    tars.run_forever(me)


def make_errands(cfg: Config, has_openai: bool):
    """[browser]'s errands for the chats (browser/errands.py), or None with it off. ConfigError if it can't run."""
    if not cfg.browser.enabled:
        return None
    if not has_openai:
        raise ConfigError(f"[browser] needs {OPENAI_KEY} in .env: an OpenAI model works the browser.")
    try:
        from .browser.errands import Errands
    except ImportError:
        raise ConfigError("[browser] needs the browser extra: uv sync --extra browser") from None
    orders = "orders placed once approved" if cfg.browser.really_order else "approved orders a dry run"
    print(f"(errands in the browser, by {cfg.browser.model} at {cfg.browser.effort} effort; {orders})")
    return Errands(cfg.browser, None, list(cfg.telegram.people))


def make_s3(cfg: Config, root: Path) -> S3:
    """[backup]'s bucket, with the key in .env. ConfigError if the backup is off, or can't be."""
    from .backup import make_s3 as connect

    if not cfg.backup.enabled:
        raise ConfigError("Set enabled = true, bucket and region under [backup] (docs/backup.md).")
    env = root / ".env"
    try:
        return connect(cfg.backup, api_key(env, AWS_KEY_ID), api_key(env, AWS_SECRET))
    except ValueError as e:
        raise ConfigError(f"The backup {e}.") from None


def snapshot_files(cfg: Config, root: Path) -> dict[str, Path]:
    """What a snapshot holds besides the database, by its name there, and where each is on this machine: the
    voiceprints, the wake models trained on the household (the self-learning pairs, and models/personal/) and this
    machine's own settings (config.local.toml; never .env, which holds the keys). And what the house has learned that
    can't be had again without someone there: the Android TV's ADB key (or every box asks again), the devices' found
    addresses, and each [home.plugins] module's BACKUP_FILES. A restore puts each back where this machine's config
    says, so the two machines' folders needn't match."""
    from .androidtv import KEY_FILE
    from .found import FOUND_FILE

    files = {
        "voiceprints.npz": root / cfg.speaker.voiceprints,
        "wake_models": root / cfg.learning.folder / "wake_models",
        "models/personal": root / "models/personal",
        LOCAL_CONFIG: root / LOCAL_CONFIG,
        Path(KEY_FILE).parent.as_posix(): root / Path(KEY_FILE).parent,
        FOUND_FILE: root / FOUND_FILE,
    }
    return files | plugin_files(cfg, root)


def plugin_files(cfg: Config, root: Path) -> dict[str, Path]:
    """Each [home.plugins] module's BACKUP_FILES: its own files and folders under the project's folder, by that path
    (examples.my_house keeps what it learned of the AC and the remotes). A plugin that won't load, or names a path
    outside the project's folder, is left out with a line saying so: the backup goes on without it."""
    from .home import load_plugin

    files: dict[str, Path] = {}
    for name, plugin in cfg.home.plugins.items():
        try:
            listed = getattr(load_plugin(plugin["module"], root), "BACKUP_FILES", ())
        except Exception as e:  # noqa: BLE001 - a plugin that won't load costs its files, not the backup
            print(f"(backup: the plugin {name} didn't load, so its files aren't in the snapshot: {e})")
            continue
        for path in map(Path, listed):
            if path.is_absolute() or ".." in path.parts:
                print(f"(backup: the plugin {name}'s {path} is outside the project's folder, so it's left out)")
                continue
            files[path.as_posix()] = root / path
    return files


def make_backup(cfg: Config, root: Path, log: EventLog | None = None) -> Backup:
    """The backup to [backup]'s bucket, of `log` (made from config.toml if not given): the event log's files, and a
    snapshot of the database and snapshot_files every six hours. ConfigError if it's off, or can't be."""
    from .backup import Backup

    s3 = make_s3(cfg, root)
    log = log or make_event_log(cfg, root)
    if log is None:
        raise ConfigError("The backup is of the event log: set log_events = true under [learning] in config.toml.")
    return Backup(log, cfg.backup, s3, snapshot_files(cfg, root))


def backup_pass(cfg: Config, root: Path, delete_anyway: bool = False) -> None:
    """--backup: one pass, as its timer runs it."""
    from .backup import Busy

    try:
        status = make_backup(cfg, root).run(delete_anyway=delete_anyway)
    except Busy as e:
        print(f"(backup: {e}; this pass is skipped)")
        return
    print(
        f"(backup: {status.uploaded} uploaded, {status.offloaded} removed from this machine, {status.brought_back} "
        f"brought back, {status.deleted} deleted from the bucket; {status.in_bucket} files in it, "
        f"{status.only_in_bucket} only there)"
    )
    for note in status.notes:
        print(f"({note})")
    if status.error:
        raise SystemExit(f"The backup didn't finish: {status.error}")


def fetch_audio(cfg: Config, root: Path, restore_archived: bool = False) -> None:
    """--fetch-audio: brings back onto this machine every recording that's only in the bucket."""
    from .backup import BackupError, Busy

    try:
        done = make_backup(cfg, root).fetch(restore=restore_archived)
    except (BackupError, Busy) as e:
        raise SystemExit(f"Couldn't fetch the audio: {e}") from None
    said = [f"Fetched {done.fetched} files."]
    if done.restoring:
        said.append(f"{done.restoring} archived ones are being restored: run this again in up to 48 hours.")
    if done.archived:
        said.append(f"{done.archived} are archived: add --restore-archived to bring them back.")
    if done.missing:
        said.append(f"{done.missing} weren't in the bucket any more.")
    print(" ".join(said))


def restore_backup(cfg: Config, root: Path) -> None:
    """--restore-backup: the bucket's latest snapshot, put in place on a new machine, with TARS stopped."""
    from .backup import SNAPSHOT_DB, BackupError, Busy, restore_snapshot
    from .check import systemd_state

    units = [
        "voice-assistant",
        "voice-assistant-web",
        "voice-assistant-telegram",
        "voice-assistant-backup.timer",
        "voice-assistant-backup.service",
    ]
    # Anything but stopped counts as running: "activating" is a backup pass under way.
    running = [u for u in units if systemd_state(u) not in (None, "inactive", "failed")]
    if running:
        raise SystemExit(f"Stop TARS first: systemctl --user stop {' '.join(running)}")
    places = {SNAPSHOT_DB: root / cfg.learning.folder / DB_FILE, **snapshot_files(cfg, root)}
    try:
        key = restore_snapshot(make_s3(cfg, root), cfg.backup, places)
    except (ValueError, BackupError, Busy) as e:
        raise SystemExit(f"Couldn't restore: {e}") from None
    print(
        f"Restored {key}. Start TARS again: the first backup pass (within 15 minutes, or --backup now) brings back "
        "what has to be on this machine; the other recordings play from the bucket, and --fetch-audio brings them here."
    )


def phrase_folder(cfg: Config, root: Path) -> Path:
    """Where the short lines' audio is kept: one folder per [tts] setup, so a new voice makes them again."""
    setup = json.dumps(dataclasses.asdict(cfg.tts), sort_keys=True)
    return root / PHRASES / hashlib.sha256(setup.encode()).hexdigest()[:12]


def typed(cfg: Config, root: Path) -> None:
    """--text: questions typed, answers spoken. No microphone, and nothing is logged."""
    from .assistant import UNPROMPTED_WAIT_S, Assistant

    made: list = []  # the assistant, once made: what the TV agent says when it's done is said through it
    # One at a time: the agent's line, from its own thread, never over a reply or in the middle of one.
    speaking = threading.Lock()

    def announce(line: str) -> None:
        print(f"\nTARS:  {line}\n\nYou:  ", end="", flush=True)
        if made:
            with speaking:
                if made[0].say(line, keep=False, wait_s=UNPROMPTED_WAIT_S):
                    made[0].brain.remember_said(line)

    presence = make_presence(cfg, root, make_event_log(cfg, root)) if cfg.presence.enabled else None
    _, brain, voice = make_pipeline(cfg, root, typed=True, home=make_home(cfg, root, announce, presence))
    with Speaker(
        find_device(cfg.audio.output_device, "output"), voice.sample_rate, cfg.audio.playback_prebuffer_s
    ) as speaker:
        assistant = Assistant(None, speaker, None, None, None, brain, voice)
        made.append(assistant)
        while True:
            try:
                text = input("\nYou:  ").strip()
            except EOFError:
                print()
                return
            if not text:
                continue
            try:
                with speaking:
                    assistant.answer_text(text)
            except AudioDeviceError:
                raise
            except Exception as e:  # noqa: BLE001 - a failed question shouldn't end the session
                print(f"Error: {e!r}")


def run(cfg: Config, push_to_talk: bool, root: Path) -> None:
    from .assistant import Announcements, Assistant
    from .journal import Journal

    events = make_event_log(cfg, root)
    start_heartbeat(events, "assistant", root)
    speaker_id = make_speaker_id(cfg, root) if cfg.speaker.enabled else None
    presence = make_presence(cfg, root, events)
    reminders = make_reminders(cfg, events, push_to_talk, speaker_id, presence)
    start_presence(presence, reminders)
    announcements = Announcements()
    home = make_home(cfg, root, announcements.add, presence)
    transcriber, brain, voice = make_pipeline(cfg, root, reminders=reminders, home=home)
    recorder = make_recorder(cfg, root)
    echo = make_echo_canceller(cfg)
    speaker = Speaker(
        find_device(cfg.audio.output_device, "output"), voice.sample_rate, cfg.audio.playback_prebuffer_s, echo
    )
    with Microphone(find_device(cfg.audio.input_device, "input"), echo) as mic, speaker:
        if echo:
            echo.set_delay(mic.latency + speaker.latency)
        journal = Journal(events)
        journal.keep_pruning(cfg.learning.keep_audio_days)
        trigger, idle_message = make_trigger(cfg, push_to_talk, root, journal)
        assistant = Assistant(
            mic,
            speaker,
            trigger,
            recorder,
            transcriber,
            brain,
            voice,
            speaker_id,
            name_threshold=cfg.speaker.wake_threshold,
            journal=journal,
            humor=cfg.llm.humor,
            phrase_folder=phrase_folder(cfg, root),
            reminders=reminders,
            ack_window_s=cfg.reminders.ack_window_s,
            announcements=announcements,
            elsewhere=make_chat_when_out(cfg, presence, reminders),
        )
        assistant.run_forever(
            idle_message,
            follow_up_s=cfg.recorder.follow_up_s,
            greet_after_s=cfg.recorder.greet_after_s,
            greet=cfg.recorder.greet,
        )


if __name__ == "__main__":
    main()
