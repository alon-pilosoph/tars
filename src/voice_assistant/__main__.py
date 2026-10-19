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
    OPENAI_KEY,
    OPENAI_STT_MODEL,
    QUICK_PROVIDERS,
    TELEGRAM_KEY,
    WEB_PORT,
    Config,
    ConfigError,
    LLMConfig,
    QuickProvider,
    load_config,
    with_keys,
)
from .recorder import make_recorder
from .versions import UNUSABLE, model_versions, pair_source
from .wake import PushToTalkTrigger, wake_word_trigger

if TYPE_CHECKING:
    from collections.abc import Callable

    from .events import EventLog
    from .health import Report
    from .llm import QuickService
    from .stt import Transcriber

OPENAI_TIMEOUT_S = 15.0
AGENT_TIMEOUT_S = 40.0  # one step of the Android TV agent, a screenshot to read
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

    key = api_key(env_path, "OPENAI_API_KEY")
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


def load_echo_canceller():
    try:
        from .echo import EchoCanceller

        return EchoCanceller(SAMPLE_RATE)
    except ImportError:
        raise RuntimeError('needs the optional extra "echo": run uv sync --extra echo') from None


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
    from .audio import SAMPLE_RATE
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
    from functools import partial

    from .clustering import regroup
    from .webui import serve

    log = make_event_log(cfg, root)
    if log is None:
        raise SystemExit("The web UI shows the event log: set log_events = true under [learning] in config.toml.")
    speaker_id = make_speaker_id(cfg, root) if cfg.speaker.enabled else None
    try:
        pairs = pair_source(cfg, root)
    except (FileNotFoundError, ValueError) as e:
        raise ConfigError(str(e)) from None
    from .reminders import Reminders

    reminders = Reminders.from_config(log.store, cfg.reminders) if cfg.reminders.enabled else None
    from .health import Heartbeat

    Heartbeat(log.folder, "web", root).start()
    serve(
        log,
        host,
        port,
        recluster=partial(regroup, log, speaker_id),
        pairs=pairs,
        allowed_hosts=frozenset(cfg.web.allowed_hosts),
        reminders=reminders,
        voice_names=speaker_id.names if speaker_id else list,
        health=make_health(cfg, root, log),
    )


def make_health(cfg: Config, root: Path, log: EventLog) -> Callable[[], Report]:
    """What the web UI's Status page shows, as it is when asked (health.py)."""
    from .check import service_checks, systemd_state
    from .health import Health, Services, expected

    return Health(log, expected(cfg), Services(lambda: service_checks(cfg, root)), systemd_state).report


def install_models(cfg: Config, root: Path, folder: Path, based_on: str | None) -> None:
    """Adds a pair training.household made and puts it in use; a running assistant switches within seconds."""
    versions = model_versions(cfg, root)
    if versions is None:
        raise SystemExit(
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


def make_reminders(cfg: Config, events, push_to_talk: bool, speaker_id=None):
    """Reminders need the event log to keep them in, and the wake loop to say them: not with push-to-talk, which
    waits on Enter instead. Waiting until someone's back needs speaker ID's voiceprints."""
    if not (cfg.reminders.enabled and events) or push_to_talk or cfg.wake.mode == "push-to-talk":
        return None
    from .reminders import Reminders, ReminderTools

    reminders = Reminders.from_config(events.store, cfg.reminders)
    return ReminderTools(reminders, voices=speaker_id.names if speaker_id else list)


def make_home(cfg: Config, root: Path, announce=None):
    """The devices in the house, as [home] lists them (a HomeTools), or None. Like every service, optional: one that
    can't be made (no token, a missing extra, a plugin's own problem) is left out with a line saying why.
    `announce(line)`: how TARS says something on its own, later (how a task on the TV went)."""
    if not cfg.home.devices:
        return None
    from .home import Context, build

    env = root / ".env"
    context = Context(root, lambda name: dotenv_values(env).get(name) or None, announce=announce)
    context.tv_agent = functools.partial(make_tv_agent, cfg, env, announce) if announce else None
    built = build(cfg.home, context)
    for name, why in built.unavailable.items():
        print(f"(the {name} isn't connected: {why}; see docs/home.md)")
    if built.tools:
        print(f"(the house: {', '.join(built.tools.devices)})")
    return built.tools


def make_tv_agent(cfg: Config, env: Path, announce, box):
    """The agent that works an Android TV box step by step, if there's OpenAI to run it; None otherwise."""
    from .androidtv import TvAgent

    if OPENAI_KEY not in env_keys(env):
        return None
    from openai import OpenAI

    # Each step reads a screenshot, so a step can take longer than a spoken reply may.
    client = OpenAI(api_key=api_key(env, OPENAI_KEY), timeout=AGENT_TIMEOUT_S, max_retries=1)
    return TvAgent(box, client, cfg.home.tv_model or cfg.llm.model, announce, service_tier=cfg.llm.service_tier)


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
        speech = DeepgramSpeech(api_key(env, "DEEPGRAM_API_KEY"), cfg.tts)
    elif cfg.tts.provider == "cartesia":
        speech = CartesiaSpeech(api_key(env, "CARTESIA_API_KEY"), cfg.tts)
    else:
        speech = OpenAISpeech(client, cfg.tts)
    voice = apply_effect(speech, cfg.tts.effect)
    if typed:
        return None, brain, voice
    if cfg.stt.provider == "openai":
        return OpenAITranscriber(client, cfg.stt), brain, voice
    key = api_key(env, "DEEPGRAM_API_KEY")
    streaming = FluxTranscriber(key, cfg.stt) if cfg.stt.provider == "flux" else DeepgramTranscriber(key, cfg.stt)
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
    deepgram = DeepgramFileTranscriber(api_key(env, "DEEPGRAM_API_KEY"), cfg.stt)
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
    from .reminders import Reminders, ReminderTools
    from .speaker import voiceprint_names
    from .telegram import CHAT_REMIND_TOOLS, Bot, TelegramError, TelegramTars, channel_rules

    if not cfg.telegram.enabled:
        raise ConfigError(
            "Set enabled = true under [telegram] in config.toml, and say who may chat in people (docs/telegram.md)."
        )
    env = root / ".env"
    bot = Bot(api_key(env, TELEGRAM_KEY))
    try:
        me = bot.me()
    except TelegramError as e:
        if e.status in (401, 404):
            raise ConfigError(f"Telegram refused {TELEGRAM_KEY}: check it in {env}.") from None
        raise SystemExit(f"Can't reach Telegram: {e}") from None  # systemd tries again
    events = make_event_log(cfg, root)
    if events:
        from .health import Heartbeat

        Heartbeat(events.folder, "telegram", root).start()
    tools = None
    if cfg.reminders.enabled and events:
        voiceprints = root / cfg.speaker.voiceprints
        names = (lambda: voiceprint_names(voiceprints)) if cfg.speaker.enabled else list
        tools = ReminderTools(Reminders.from_config(events.store, cfg.reminders), voices=names)
    client = make_openai_client(env) if OPENAI_KEY in env_keys(env) else None
    llm, rules = chat_brain_config(cfg, client is not None), channel_rules(cfg.telegram.people)
    who = "OpenAI's " + llm.model if not any(getattr(llm, p.setting) for p in QUICK_PROVIDERS) else "Qwen first"
    print(f"(chats are answered by {who})")
    TelegramTars(
        bot,
        cfg.telegram.people,
        lambda: make_brain(client, llm, env, tools, channel_rules=rules, remind_tools=CHAT_REMIND_TOOLS),
        ConversationLog(events) if events else None,
        tools,
        memory_s=llm.memory_minutes * 60,
        send_items=cfg.telegram.send_items,
        transcriber=make_recording_transcriber(cfg, env, client),
    ).run_forever(me)


def phrase_folder(cfg: Config, root: Path) -> Path:
    """Where the short lines' audio is kept: one folder per [tts] setup, so a new voice makes them again."""
    setup = json.dumps(dataclasses.asdict(cfg.tts), sort_keys=True)
    return root / PHRASES / hashlib.sha256(setup.encode()).hexdigest()[:12]


def typed(cfg: Config, root: Path) -> None:
    """--text: questions typed, answers spoken. No microphone, and nothing is logged."""
    from .assistant import Assistant

    made: list = []  # the assistant, once made: what the TV agent says when it's done is said through it
    # One at a time: the agent's line, from its own thread, never over a reply or in the middle of one.
    speaking = threading.Lock()

    def announce(line: str) -> None:
        print(f"\nTARS:  {line}\n\nYou:  ", end="", flush=True)
        if made:
            with speaking:
                made[0].say(line, keep=False)
                made[0].brain.remember_said(line)

    _, brain, voice = make_pipeline(cfg, root, typed=True, home=make_home(cfg, root, announce))
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
    if events:
        from .health import Heartbeat

        Heartbeat(events.folder, "assistant", root).start()
    speaker_id = make_speaker_id(cfg, root) if cfg.speaker.enabled else None
    reminders = make_reminders(cfg, events, push_to_talk, speaker_id)
    announcements = Announcements()
    home = make_home(cfg, root, announcements.add)
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
            phrases=phrase_folder(cfg, root),
            reminders=reminders,
            ack_window_s=cfg.reminders.ack_window_s,
            announcements=announcements,
        )
        assistant.run_forever(
            idle_message,
            follow_up_s=cfg.recorder.follow_up_s,
            greet_after_s=cfg.recorder.greet_after_s,
            greet=cfg.recorder.greet,
        )


if __name__ == "__main__":
    main()
