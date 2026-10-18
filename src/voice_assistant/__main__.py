"""The command line: `voice-assistant` runs TARS; its options record voices, enroll speakers, test the mic, serve the
web UI and install trained wake models. Heavy dependencies are imported lazily, so each command loads only its own."""

import argparse
import dataclasses
import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

from .audio import SAMPLE_RATE, AudioDeviceError, Microphone, NoDeviceError, Speaker, find_device, list_devices
from .config import (
    OPENAI_KEY,
    OPENAI_STT_MODEL,
    QUICK_PROVIDERS,
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
    from .llm import QuickService

OPENAI_TIMEOUT_S = 15.0
AGENT_TIMEOUT_S = 40.0  # one step of the Fire TV agent, a screenshot to read
QUICK_TIMEOUT_S = 4.0  # the quick brain's replies take well under a second
PHRASES = "voice_data/phrases"  # the short lines TARS makes ahead, kept across restarts
AC_FOLDER = "voice_data/ac"  # the AC's learned profile (profile.json), and whether TARS thinks it's on (state.json)
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
    serve(
        log,
        host,
        port,
        recluster=partial(regroup, log, speaker_id),
        pairs=pairs,
        allowed_hosts=frozenset(cfg.web.allowed_hosts),
        reminders=reminders,
        voice_names=speaker_id.names if speaker_id else list,
    )


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
    """The devices in the house, as [home] lists them, or None. Like every service, optional: without the token, the
    blaster's key or the AC's profile, TARS says what it can't work and starts anyway. `announce(line, for_name)`:
    how TARS says something on its own, later (the Fire TV agent's result)."""
    if not cfg.home.devices:
        return None
    from .ac import TUYA_KEY
    from .home import AC, FIRETV, IR, TOKEN_KEY, HomeAssistant, HomeTools

    env = root / ".env"
    keys = env_keys(env)
    hass = None
    if cfg.home.url and TOKEN_KEY in keys:
        hass = HomeAssistant(cfg.home.url, api_key(env, TOKEN_KEY))
    elif cfg.home.url:
        print(f"(no {TOKEN_KEY}: the lock and the boiler aren't connected; see docs/home.md)")
    blaster = None
    if cfg.home.blaster and TUYA_KEY in keys:
        blaster = make_blaster(cfg.home.blaster, root)
    elif cfg.home.blaster:
        print(f"(no {TUYA_KEY}: the AC, the TV and the soundbar by IR aren't connected; see docs/home.md)")
    local = {}
    for spec in cfg.home.devices:
        name = spec["name"].strip().lower()
        try:
            if spec["kind"] == AC and blaster:
                local[name] = make_ac(spec, blaster, hass, root)
            elif spec["kind"] == IR and blaster:
                from .ir import CODE_SETS, Remote

                local[name] = Remote(name, CODE_SETS[spec["codes"]], blaster.send)
            elif spec["kind"] == FIRETV:
                local[name] = make_tv(cfg, spec, root, announce)
        except (ValueError, ConfigError) as e:
            print(f"(the {name} isn't connected: {e.code if isinstance(e, ConfigError) else e})")
    home = HomeTools(cfg.home, hass, local)
    if home.devices:
        print(f"(the house: {', '.join(home.devices)})")
    return home


def make_blaster(spec: dict, root: Path):
    """The IR blaster, from [home] blaster."""
    from .ac import TUYA_KEY, Blaster

    return Blaster(spec["id"], spec["address"], api_key(root / ".env", TUYA_KEY), spec.get("version", "3.3"))


def make_ac(spec: dict, blaster, hass, root: Path):
    from .ac import AC, Profile

    folder = root / AC_FOLDER
    profile = Profile.load(root / spec["profile"] if spec.get("profile") else folder / "profile.json")
    if not profile.checked:
        print("(the AC's profile wasn't tried on the AC yet: python -m voice_assistant.ac_onboard try)")
    return AC(profile, blaster.send, folder / "state.json", hass, spec.get("state", ""))


def make_tv(cfg: Config, spec: dict, root: Path, announce=None):
    """The Fire TV, and its agent if there's OpenAI to run it and somewhere to say how it went."""
    from .firetv import KEY_FILE, TV, FireTV, TvAgent

    try:
        import adb_shell  # noqa: F401 - only whether it's there
    except ImportError:
        raise ValueError('it needs the optional extra "tv": uv sync --all-extras') from None
    stick = FireTV(spec["address"], root / KEY_FILE)
    agent = None
    if announce and OPENAI_KEY in env_keys(root / ".env"):
        from openai import OpenAI

        # Each step reads a screenshot, so a step can take longer than a spoken reply may.
        client = OpenAI(api_key=api_key(root / ".env", OPENAI_KEY), timeout=AGENT_TIMEOUT_S, max_retries=1)
        agent = TvAgent(stick, client, cfg.home.tv_model or cfg.llm.model, announce)
    elif announce:
        print(f'(no {OPENAI_KEY}: the Fire TV takes single buttons and apps, not tasks like "play Toy Story")')
    return TV(stick, agent)


def make_pipeline(cfg: Config, root: Path, typed: bool = False, reminders=None, home=None):
    """The cloud stages, as config.toml picks them: (transcriber, brain, voice); no transcriber for typed questions.
    With `reminders` (a ReminderTools), the brain can set them and knows what's set; with `home` (a HomeTools), it
    can work the devices in the house."""
    from .effects import apply_effect
    from .llm import OpenAIChat, QuickChat
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
    llm = brain_config(cfg, typed=typed)
    quick = quick_services(llm, env)
    if quick:
        brain = QuickChat(client, llm, quick, reminders, home=home)
    else:
        brain = OpenAIChat(client, llm, reminders, home=home)
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


def phrase_folder(cfg: Config, root: Path) -> Path:
    """Where the short lines' audio is kept: one folder per [tts] setup, so a new voice makes them again."""
    setup = json.dumps(dataclasses.asdict(cfg.tts), sort_keys=True)
    return root / PHRASES / hashlib.sha256(setup.encode()).hexdigest()[:12]


def typed(cfg: Config, root: Path) -> None:
    """--text: questions typed, answers spoken. No microphone, and nothing is logged."""
    from .assistant import Assistant

    later: list = []  # the assistant, once made: what the TV agent says when it's done is said through it

    def announce(line: str, for_name: str | None = None) -> None:
        print(f"\nTARS:  {line}\n\nYou:  ", end="", flush=True)
        if later:
            later[0].say(line, keep=False)
            later[0].brain.remember_said(line)

    _, brain, voice = make_pipeline(cfg, root, typed=True, home=make_home(cfg, root, announce))
    with Speaker(
        find_device(cfg.audio.output_device, "output"), voice.sample_rate, cfg.audio.playback_prebuffer_s
    ) as speaker:
        assistant = Assistant(None, speaker, None, None, None, brain, voice)
        later.append(assistant)
        while True:
            try:
                text = input("\nYou:  ").strip()
            except EOFError:
                print()
                return
            if not text:
                continue
            try:
                assistant.answer_text(text)
            except AudioDeviceError:
                raise
            except Exception as e:  # noqa: BLE001 - a failed question shouldn't end the session
                print(f"Error: {e!r}")


def run(cfg: Config, push_to_talk: bool, root: Path) -> None:
    from .assistant import Assistant
    from .journal import Journal

    events = make_event_log(cfg, root)
    speaker_id = make_speaker_id(cfg, root) if cfg.speaker.enabled else None
    reminders = make_reminders(cfg, events, push_to_talk, speaker_id)
    from .assistant import Announcements

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
