"""The command line: `voice-assistant` runs TARS; its options record voices, enroll speakers, test the mic, serve the
web UI and install trained wake models (entrypoints.py has what each runs). Heavy dependencies are imported lazily,
so each command loads only its own."""

import argparse
import sys
from pathlib import Path

from .audio import AudioDeviceError, NoDeviceError, list_devices
from .config import WEB_PORT, ConfigError, load_config
from .entrypoints import (
    backup_pass,
    enroll_speaker,
    fetch_audio,
    ha_config_from_backup,
    ha_config_history,
    ha_config_key,
    ha_config_restore,
    install_models,
    record_session,
    record_voice_session,
    restore_backup,
    run,
    telegram,
    typed,
    update_pass,
    web_invite,
    web_sign_out,
    web_ui,
)
from .wiring import keyed

# sysexits' EX_CONFIG: a setup that can't work, which systemd mustn't keep restarting (RestartPreventExitStatus).
EX_CONFIG = 78
# sysexits' EX_UNAVAILABLE: no speakerphone. Not restarted either; plugging it in starts TARS (the udev rule).
EX_UNAVAILABLE = 69


def main() -> None:
    parser = argparse.ArgumentParser(prog="voice-assistant")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--list-devices", action="store_true", help="show audio devices and exit")
    parser.add_argument(
        "--check", action="store_true", help="check everything TARS needs (keys, devices, services, models) and exit"
    )
    parser.add_argument(
        "--silent", action="store_true", help="with --check: make the voice without playing it, and don't listen"
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
        "--record-session",
        choices=["voice", "house"],
        help="send a link to a recording session on TARS's speakerphone: someone's voice, or the house's commands",
    )
    parser.add_argument("--for", dest="person", metavar="NAME", help="whose --record-session it is")
    parser.add_argument(
        "--cloud",
        action="store_true",
        help="with --record-session house: its takes may also be sent to Deepgram when measured, and its page says so",
    )
    parser.add_argument(
        "--web-invite", metavar="NAME", help="a one-time link for NAME to make a passkey for the web UI ([web] sign_in)"
    )
    parser.add_argument(
        "--web-sign-out", metavar="NAME", help="sign NAME out of the web UI everywhere, and forget their passkeys"
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
    backup.add_argument(
        "--ha-config", action="store_true", help="the latest changes to Home Assistant's setup ([home] config_dir)"
    )
    backup.add_argument(
        "--update", action="store_true", help="install what's new on [updates] branch, once, as its timer does"
    )
    backup.add_argument(
        "--ha-config-key",
        action="store_true",
        help="make the key Home Assistant's setup is sealed with in the backup (HA_CONFIG_KEY in .env)",
    )
    backup.add_argument(
        "--ha-config-from-backup",
        action="store_true",
        help="on a new machine, after --restore-backup: Home Assistant's setup and its history, from the snapshot",
    )
    backup.add_argument(
        "--ha-config-restore",
        metavar="CHANGE",
        help="put Home Assistant's setup back as it was at a change --ha-config lists (stops and starts it)",
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
    if bool(args.record_session) != bool(args.person):
        parser.error("--record-session and --for NAME go together")
    if args.cloud and args.record_session != "house":
        parser.error("--cloud goes with --record-session house")

    if args.list_devices:
        list_devices()
        return

    root = args.config.resolve().parent
    try:
        cfg = load_config(args.config)
        if args.check:
            from .check import check_all

            raise SystemExit(check_all(cfg, root, silent=args.silent))
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
        if args.record_session:
            record_session(cfg, root, args.record_session, args.person, args.port, args.cloud)
            return
        if args.web_invite:
            web_invite(cfg, root, args.web_invite, args.port)
            return
        if args.web_sign_out:
            web_sign_out(cfg, root, args.web_sign_out)
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
        if args.ha_config:
            ha_config_history(cfg, root)
            return
        if args.update:
            update_pass(cfg, root)
            return
        if args.ha_config_key:
            ha_config_key(root)
            return
        if args.ha_config_from_backup:
            ha_config_from_backup(cfg, root)
            return
        if args.ha_config_restore:
            ha_config_restore(cfg, root, args.ha_config_restore)
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


if __name__ == "__main__":
    main()
