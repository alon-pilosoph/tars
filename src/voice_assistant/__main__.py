import argparse
from pathlib import Path

from dotenv import dotenv_values

from .audio import Microphone, MicrophoneError, Speaker, find_device, list_devices
from .config import Config, load_config
from .recorder import UtteranceRecorder
from .wake import PushToTalkTrigger, wake_word_trigger


OPENAI_TIMEOUT_S = 15.0


def make_openai_client(env_path: Path):
    from openai import OpenAI

    # Read the key from this project's .env only, never from the shell environment,
    # so the assistant can't silently pick up a key meant for something else.
    key = dotenv_values(env_path).get("OPENAI_API_KEY")
    if not key:
        raise SystemExit(f"Put OPENAI_API_KEY in {env_path} (see .env.example).")
    # The SDK default is 10 minutes per request plus 2 retries. A voice assistant should give up
    # quickly instead, so a dropped connection costs one error tone rather than a frozen assistant.
    return OpenAI(api_key=key, timeout=OPENAI_TIMEOUT_S, max_retries=1)


def make_trigger(cfg: Config, push_to_talk: bool, root: Path):
    if push_to_talk or cfg.wake.mode == "push-to-talk":
        return PushToTalkTrigger(), "Press Enter to talk."
    trigger = wake_word_trigger(cfg.wake.model, cfg.wake.threshold)
    if cfg.wake.verify:
        from .verify import PhraseVerifier, VerifiedTrigger

        trigger = VerifiedTrigger(trigger, PhraseVerifier(trigger.phrase, root / "models"))
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
        record_voice(person, mic_name, mic, speaker, UtteranceRecorder(cfg.recorder), root)


def make_speaker_id(cfg: Config, root: Path):
    from .speaker import SpeakerID

    return SpeakerID(root / cfg.speaker.model, root / cfg.speaker.voiceprints, cfg.speaker.threshold)


def enroll_speaker(cfg: Config, person: str, mic_name: str, root: Path) -> None:
    from .speaker import read_wav

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
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    cfg = load_config(args.config)
    try:
        if args.mic_test:
            from .mic_test import mic_test

            mic_test(cfg, args.config.resolve().parent)
            return
        root = args.config.resolve().parent
        if args.record_voice:
            record_voice_session(cfg, args.record_voice, args.mic, root / "voice_data")
            return
        if args.enroll:
            enroll_speaker(cfg, args.enroll, args.mic, root)
            return
        run(cfg, args)
    except KeyboardInterrupt:
        print("\nBye.")
    except MicrophoneError as e:
        # Exit non-zero so a supervisor (systemd on the Pi) restarts us once the device is back.
        raise SystemExit(str(e))


def run(cfg: Config, args: argparse.Namespace) -> None:
    from openai import OpenAIError

    from .assistant import Assistant
    from .effects import apply_effect
    from .llm import OpenAIChat
    from .stt import OpenAITranscriber
    from .tts import OpenAISpeech

    client = make_openai_client(args.config.resolve().parent / ".env")
    brain = OpenAIChat(client, cfg.llm)
    voice = apply_effect(OpenAISpeech(client, cfg.tts), cfg.tts.effect)
    transcriber = OpenAITranscriber(client, cfg.stt)
    recorder = UtteranceRecorder(cfg.recorder)
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
        trigger, idle_message = make_trigger(cfg, args.ptt, args.config.resolve().parent)
        speaker_id = make_speaker_id(cfg, args.config.resolve().parent) if cfg.speaker.enabled else None
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
        )
        assistant.run_forever(
            idle_message, follow_up_s=cfg.recorder.follow_up_s, greet_after_s=cfg.recorder.greet_after_s
        )


if __name__ == "__main__":
    main()
