import argparse
import dataclasses
from pathlib import Path

from dotenv import dotenv_values

from .audio import Microphone, MicrophoneError, Speaker, find_device, list_devices
from .config import Config, LLMConfig, load_config
from .recorder import make_recorder
from .wake import PushToTalkTrigger, wake_word_trigger

OPENAI_TIMEOUT_S = 15.0
# OpenAI's transcription model, when it backs up another speech-to-text service.
OPENAI_STT_MODEL = "gpt-4o-mini-transcribe"


def api_key(env_path: Path, name: str) -> str:
    # Keys come from this project's .env only, never from the shell environment,
    # so the assistant can't silently pick up a key meant for something else.
    key = dotenv_values(env_path).get(name)
    if not key:
        raise SystemExit(f"Put {name} in {env_path} (see .env.example).")
    return key


def make_openai_client(env_path: Path):
    from openai import OpenAI

    key = api_key(env_path, "OPENAI_API_KEY")
    # The SDK default is 10 minutes per request plus 2 retries. A voice assistant should give up
    # quickly instead, so a dropped connection costs one error tone rather than a frozen assistant.
    return OpenAI(api_key=key, timeout=OPENAI_TIMEOUT_S, max_retries=1)


def make_event_log(cfg: Config, root: Path):
    if not cfg.learning.log_events:
        return None
    from .events import EventLog

    return EventLog(root / cfg.learning.folder)


def brain_config(cfg: Config, typed: bool) -> LLMConfig:
    """Sent items live in the event log, which typed questions (--text) don't use: without it, TARS mustn't say it
    sent anything."""
    return dataclasses.replace(cfg.llm, send=cfg.llm.send and cfg.learning.log_events and not typed)


def make_trigger(cfg: Config, push_to_talk: bool, root: Path, journal=None):
    if push_to_talk or cfg.wake.mode == "push-to-talk":
        return PushToTalkTrigger(), "Press Enter to talk."
    trigger = wake_word_trigger(cfg.wake.model, cfg.wake.threshold)
    if cfg.wake.verify:
        from .verify import PhraseVerifier, VerifiedTrigger

        check = root / cfg.wake.check_model if cfg.wake.check_model else None
        trigger = VerifiedTrigger(
            trigger,
            PhraseVerifier(trigger.phrase, root / "models", check),
            cfg.wake.check_window_s,
            journal=journal,
            wake_model=cfg.wake.model,
            check_model=cfg.wake.check_model,
        )
    return trigger, f"Say '{trigger.phrase}'..."


def record_voice_session(cfg: Config, person: str, mic_name: str, root: Path) -> None:
    """No API key needed: this only records clips for the verifier, the stop word and speaker ID."""
    from .audio import SAMPLE_RATE
    from .enroll import record_voice

    print(
        f"Recording {person}'s voice on the {mic_name} mic. After each chime, say the prompt. Ctrl+C to stop; rerun to resume."
    )
    speaker = Speaker(find_device(cfg.audio.output_device, "output"), SAMPLE_RATE, cfg.audio.playback_prebuffer_s)
    with Microphone(find_device(cfg.audio.input_device, "input")) as mic, speaker:
        record_voice(person, mic_name, mic, speaker, make_recorder(cfg, root), root)


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
        f"Enrolled {person} from {len(clips)} sentences ({mic_name} mic). Everyone enrolled: {', '.join(speaker_id.voiceprints)}"
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

    def models_info():
        return {
            "active": {
                "wake_model": cfg.wake.model,
                "threshold": cfg.wake.threshold,
                "check_model": cfg.wake.check_model or "plain phrase match",
                "check_window_s": cfg.wake.check_window_s,
            },
            "history": [],
            "last_retrain": None,
        }

    serve(
        log,
        host,
        port,
        recluster=partial(regroup, log, speaker_id),
        models_info=models_info,
        allowed_hosts=frozenset(cfg.web.allowed_hosts),
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="voice-assistant")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--list-devices", action="store_true", help="show audio devices and exit")
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
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    cfg = load_config(args.config)
    root = args.config.resolve().parent
    try:
        if args.mic_test:
            from .mic_test import mic_test

            mic_test(cfg, root)
            return
        if args.record_voice:
            record_voice_session(cfg, args.record_voice, args.mic, root / "voice_data")
            return
        if args.enroll:
            enroll_speaker(cfg, args.enroll, args.mic, root)
            return
        if args.web:
            web_ui(cfg, root, args.host, args.port)
            return
        run(cfg, args)
    except KeyboardInterrupt:
        print("\nBye.")
    except MicrophoneError as e:
        # Exit non-zero so a supervisor (systemd on the Pi) restarts us once the device is back.
        raise SystemExit(str(e))


def make_pipeline(cfg: Config, root: Path, typed: bool = False):
    """The cloud stages, as config.toml picks them: (transcriber, brain, voice)."""
    from .effects import apply_effect
    from .llm import OpenAIChat
    from .stt import DeepgramTranscriber, FallbackTranscriber, OpenAITranscriber
    from .tts import OpenAISpeech

    env = root / ".env"
    client = make_openai_client(env)
    brain = OpenAIChat(client, brain_config(cfg, typed=typed))
    voice = apply_effect(OpenAISpeech(client, cfg.tts), cfg.tts.effect)
    if cfg.stt.provider == "openai":
        return OpenAITranscriber(client, cfg.stt), brain, voice
    if cfg.stt.provider != "deepgram":
        raise SystemExit(f"[stt] provider must be openai or deepgram, not {cfg.stt.provider!r}")
    # OpenAI takes over, from the same recording, if the stream fails.
    backup = OpenAITranscriber(client, dataclasses.replace(cfg.stt, model=OPENAI_STT_MODEL))
    return FallbackTranscriber(DeepgramTranscriber(api_key(env, "DEEPGRAM_API_KEY"), cfg.stt), backup), brain, voice


def run(cfg: Config, args: argparse.Namespace) -> None:
    from openai import OpenAIError

    from .assistant import Assistant
    from .journal import Journal

    root = args.config.resolve().parent
    transcriber, brain, voice = make_pipeline(cfg, root, typed=args.text)
    recorder = make_recorder(cfg, root)
    speaker = Speaker(find_device(cfg.audio.output_device, "output"), voice.sample_rate, cfg.audio.playback_prebuffer_s)

    with Microphone(find_device(cfg.audio.input_device, "input")) as mic, speaker:
        if args.text:
            assistant = Assistant(mic, speaker, PushToTalkTrigger(), recorder, transcriber, brain, voice)
            while True:
                try:
                    text = input("\nYou:  ").strip()
                except EOFError:  # Ctrl+D, or the end of piped input.
                    print()
                    return
                if not text:
                    continue
                try:
                    assistant.answer(text)
                except OpenAIError as e:
                    print(f"OpenAI error: {e!r}")
        journal = Journal(make_event_log(cfg, root))
        journal.keep_pruning(cfg.learning.keep_audio_days)
        trigger, idle_message = make_trigger(cfg, args.ptt, root, journal)
        speaker_id = make_speaker_id(cfg, root) if cfg.speaker.enabled else None
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
        )
        assistant.run_forever(
            idle_message, follow_up_s=cfg.recorder.follow_up_s, greet_after_s=cfg.recorder.greet_after_s
        )


if __name__ == "__main__":
    main()
