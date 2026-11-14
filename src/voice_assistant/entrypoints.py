"""What each of `voice-assistant`'s options runs: the assistant, typed questions, the web UI, TARS on Telegram, the
backup, and the commands that run once. Each is put together from the parts wiring.py makes from config.toml;
__main__.py picks one. Heavy dependencies are imported lazily, so each command loads only its own."""

import functools
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

from .audio import SAMPLE_RATE, AudioDeviceError, Microphone, Speaker, find_device
from .config import OPENAI_KEY, QUICK_PROVIDERS, TELEGRAM_KEY, Config, ConfigError
from .files import atomic_write
from .recorder import make_recorder
from .store import DB_FILE, Store
from .versions import UNUSABLE, model_versions, pair_source
from .wiring import (
    Household,
    api_key,
    ask_to_say,
    browse_tool,
    chat_brain_config,
    chat_home,
    env_keys,
    errand_queue,
    ha_bundle_path,
    house_commands,
    listen_for_update_taps,
    local_body,
    local_first,
    maintainer_chats,
    make_backup,
    make_brain,
    make_browser_client,
    make_chat_when_out,
    make_continuity,
    make_errands,
    make_event_log,
    make_ha_config,
    make_health,
    make_home,
    make_openai_client,
    make_pipeline,
    make_recording_transcriber,
    make_reminders,
    make_s3,
    make_speaker_id,
    own_speaker,
    phrase_folder,
    reminder_tools,
    skill_clock,
    snapshot_files,
    spotify_login,
    start_auto_locks,
    start_buttons,
    start_heartbeat,
    start_metering,
    start_presence,
    tell_maintainers,
    voice_errands,
)

if TYPE_CHECKING:
    from .commands import DeviceWords
    from .events import EventLog


def record_voice_session(cfg: Config, person: str, mic_name: str, root: Path) -> None:
    from .enroll import record_voice

    print(
        f"Recording {person}'s voice on the {mic_name} mic. After each chime, say the prompt. "
        "Ctrl+C to stop; rerun to resume."
    )
    speaker = Speaker(find_device(cfg.audio.output_device, "output"), SAMPLE_RATE, cfg.audio.playback_prebuffer_s)
    with Microphone(find_device(cfg.audio.input_device, "input")) as mic, speaker:
        record_voice(person, mic_name, mic, speaker, make_recorder(cfg, root, turn_model=False), root / "voice_data")


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
    from .signin import Accounts
    from .webui import serve

    household = Household(cfg, root)
    log = household.events
    speaker_id = household.speaker_id
    try:
        pairs = pair_source(cfg, root)
    except (FileNotFoundError, ValueError) as e:
        raise ConfigError(str(e)) from None
    reminders = household.reminders
    names = speaker_id.names if speaker_id else list
    presence = household.presence
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
        finished=functools.partial(enroll_from_session, speaker_id, log.store),
        accounts=Accounts(log.store) if cfg.web.sign_in else None,
        tls=(root / cfg.web.tls_cert, root / cfg.web.tls_key) if cfg.web.tls_cert else None,
    )


def record_session(cfg: Config, root: Path, kind: str, person: str, port: int, cloud: bool = False) -> None:
    """--record-session: a recording session for `person` (recording.py), its link printed and sent to their
    Telegram chat if they have one. `cloud` (--cloud): a house session whose takes may also be sent to Deepgram when
    they're measured, as its page and the message say."""
    import random

    from .recording import ABOUT, HOUSE, SECONDS_A_PROMPT, Sessions, house_script, voice_script

    if not cfg.learning.log_events:
        raise ConfigError("Recording sessions record audio, which [learning] log_events = false says not to.")
    log = make_event_log(cfg, root)
    prompts = house_script(house_actions(cfg, root, log), random.Random()) if kind == HOUSE else voice_script()
    token = Sessions(log.store).create(kind, person, prompts, cloud)
    url = f"{web_link(cfg, port)}/?session={token}"
    minutes = max(1, round(len(prompts) * SECONDS_A_PROMPT / 60))
    print(f"{ABOUT[kind][0]} for {person}, {len(prompts)} phrases (about {minutes} minutes):\n{url}")
    if cloud:
        print("(its takes may also go to Deepgram, when tools/commands_bench.py --cloud measures them)")
    where = " Its recordings are kept on TARS, and also sent to Deepgram, in the cloud, when they're measured."
    where = where if cloud else ""
    text = (
        f"A recording session for you: {ABOUT[kind][0].lower()}, {len(prompts)} phrases, about {minutes} minutes."
        f"{where} Open it when you're near TARS; the link works for a day.\n{url}"
    )
    send_link(cfg, root, person, text, "their phone")


def web_link(cfg: Config, port: int) -> str:
    """Where someone's phone reaches the web UI: [web] link, else this machine's address."""
    from .browser.login_page import reachable_address

    return cfg.web.link.rstrip("/") or f"{'https' if cfg.web.tls_cert else 'http'}://{reachable_address()}:{port}"


def send_link(cfg: Config, root: Path, person: str, text: str, open_on: str) -> None:
    """`text`, with a link in it, sent to `person`'s Telegram chat if they have one."""
    from .events import person_key
    from .telegram import Bot, TelegramError

    chat = next((c for name, c in cfg.telegram.people.items() if person_key(name) == person_key(person)), None)
    if not (cfg.telegram.enabled and chat):
        print(f"({person} has no Telegram chat in [telegram] people: open the link on {open_on}.)")
        return
    try:
        Bot(api_key(root / ".env", TELEGRAM_KEY)).call("sendMessage", chat_id=chat, text=text)
    except TelegramError as e:
        print(f"(couldn't send it on Telegram: {e})")
        return
    print(f"Sent to {person} on Telegram.")


def house_actions(cfg: Config, root: Path, events: EventLog | None) -> list[DeviceWords]:
    """The house's devices as it makes them: what a house recording session asks for."""
    from .commands import device_words

    home = make_home(cfg, root, events=events)
    if home is None or not home.devices:
        raise ConfigError("There are no devices in the house to make house commands from (docs/home.md).")
    return device_words(home.devices)


def web_invite(cfg: Config, root: Path, person: str, port: int) -> None:
    """--web-invite: a one-time link for `person` to make a passkey with, printed and sent to their Telegram chat if
    they have one."""
    from .signin import Accounts

    log = make_event_log(cfg, root)
    if not person.strip():
        raise ConfigError("--web-invite needs the person's name.")
    if cfg.web.tls_cert and not cfg.web.link:
        raise ConfigError(
            "Set [web] link to the web UI's name, e.g. https://<pi-name>.<tailnet>.ts.net:8080: passkeys belong to a "
            "name, and browsers won't make one for an address (docs/web-ui.md#signing-in)."
        )
    if not cfg.web.sign_in:
        print("(sign-in is off: the link works, but the web UI won't ask for it until [web] sign_in = true)")
    url = f"{web_link(cfg, port)}/sign-in/invite/{Accounts(log.store).invite(person)}"
    print(f"A link for {person} to make a passkey for the web UI, good for a day:\n{url}")
    text = f"A link to sign in to TARS's web page: it makes a passkey on your phone. It works once, for a day.\n{url}"
    send_link(cfg, root, person, text, "their phone or computer")


def web_sign_out(cfg: Config, root: Path, person: str) -> None:
    """--web-sign-out: `person` signed out of the web UI everywhere, their passkeys forgotten and unused invites voided
    (a lost phone)."""
    from .signin import Accounts

    log = make_event_log(cfg, root)
    passkeys, sessions, invites = Accounts(log.store).remove(person)
    if not (passkeys or sessions or invites):
        print(f"Nobody called {person} has a passkey, a session or an invite for the web UI.")
        return
    print(
        f"{person} is signed out everywhere: {passkeys} passkey(s) forgotten, {sessions} session(s) ended and "
        f"{invites} unused invite(s) voided. --web-invite makes a new one."
    )


def enroll_from_session(speaker_id, store: Store, session: dict) -> None:
    """A voice session's takes become its person's voiceprint (speaker ID), as --enroll does with --record-voice's."""
    from .audio import read_wav
    from .recording import MIN_ENROLL_TAKES, VOICE, Sessions

    if session["kind"] != VOICE or speaker_id is None:
        return
    takes = [t for t in Sessions(store).takes(session["id"]) if t["audio"] and not t["problem"]]
    if len(takes) < MIN_ENROLL_TAKES:
        print(f"(only {len(takes)} good takes from {session['person']}'s session: not enough to enroll)")
        return
    speaker_id.enroll(session["person"], [read_wav(store.folder / t["audio"]) for t in takes])
    print(f"Enrolled {session['person']} from {len(takes)} takes of their recording session.")


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


def telegram(cfg: Config, root: Path) -> None:
    """--telegram: TARS in the household's Telegram chats, a process of its own, like the web UI."""
    from .conversations import ConversationLog
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
    household = Household(cfg, root)
    events = household.events
    start_heartbeat(events, "telegram", root)
    start_metering(cfg, events)
    presence = household.presence
    tools = None
    if household.reminders is not None:
        voiceprints = root / cfg.speaker.voiceprints
        names = (lambda: voiceprint_names(voiceprints)) if cfg.speaker.enabled else list
        tools = reminder_tools(household.reminders, names, presence)
    told: list = []  # the TelegramTars, once made: how a task on the TV went is posted through it

    def announce(line: str) -> None:
        if told:
            told[0].announce(line)

    home = chat_home(cfg, root, presence, events, announce)
    client = make_openai_client(env) if OPENAI_KEY in env_keys(env) else None
    llm, rules = chat_brain_config(cfg, client is not None), channel_rules(cfg.telegram.people)
    who = "OpenAI's " + llm.model if not any(getattr(llm, p.setting) for p in QUICK_PROVIDERS) else "Qwen first"
    print(f"(chats are answered by {who})")
    queue = errand_queue(cfg, events, client is not None)  # errands asked for here and aloud, run here
    feeds = household.feeds  # one for every chat: each person's feeds are kept by person
    skills = household.skills
    wishes = household.wishes  # one, with one GitHub client, for the offers, the taps and following
    myself = household.myself
    tars = TelegramTars(
        bot,
        cfg.telegram.people,
        lambda: make_brain(
            client,
            llm,
            env,
            tools,
            channel_rules=rules,
            remind_tools=CHAT_REMIND_TOOLS,
            home=home,
            browser=browse_tool(cfg, queue, "telegram"),
            feeds=feeds,
            skills=skills,
            myself=myself,
        ),
        ConversationLog(events),
        tools,
        memory_s=cfg.telegram.memory_minutes * 60,
        keep_s=cfg.telegram.keep_days * 24 * 3600,
        continuity=make_continuity(cfg, env, client),
        send_items=cfg.telegram.send_items,
        transcriber=make_recording_transcriber(cfg, env, client),
        env=env,
        home=home,
        say=functools.partial(ask_to_say, events.store.folder),
        spotify_login=spotify_login(cfg, root, env),
        skill_clock=skill_clock(skills, "chat", home, presence, myself),
    )
    told.append(tars)
    if cfg.updates.enabled:
        listen_for_update_taps(cfg, root, tars)
    if wishes:
        from .wishes import tap_listener

        tars.listen_first(tap_listener(wishes, list(maintainer_chats(cfg).values()), tars.bot))
        wishes.start()
    if queue:
        make_errands(cfg, root, make_browser_client(env), queue, tars, tools, presence).serve()
    try:
        tars.run_forever(me)
    finally:
        if wishes:
            wishes.close()


def ha_config_pass(cfg: Config, root: Path) -> None:
    """Commits what changed in Home Assistant's setup since the last pass, and seals the history for the backup's
    snapshot when it changed, with HA_CONFIG_KEY; without the key nothing goes in the snapshot. A failure costs a
    line, never the backup."""
    from .haconfig import CHANGED_IN_HA, KEY_NAME, HAConfigError, seal

    if (ha := make_ha_config(cfg, root)) is None:
        return
    bundle = ha_bundle_path(cfg, root)
    try:
        started = ha.start()
        made = ha.commit(CHANGED_IN_HA)
        if started:
            print(f"(Home Assistant's setup: its history starts now, in {ha.folder}/.git)")
        elif made:
            print(f"(Home Assistant's setup: changed, kept as {made[:8]})")
        if not (key := dotenv_values(root / ".env").get(KEY_NAME) if (root / ".env").exists() else None):
            bundle.unlink(missing_ok=True)
            print(f"(Home Assistant's setup: not in the backup, since there's no {KEY_NAME} to seal it with: "
                  "voice-assistant --ha-config-key makes one)")  # fmt: skip
        elif started or made or not bundle.exists():
            atomic_write(bundle, seal(ha.bundle(), key))
    except HAConfigError as e:
        print(f"(Home Assistant's setup: couldn't keep its history: {e})")
    except ImportError:
        print("(Home Assistant's setup: sealing it for the backup needs the backup extra: uv sync --extra backup)")


def update_pass(cfg: Config, root: Path) -> None:
    """--update: one pass, as its timer runs it."""
    from .updater import Updater

    if not cfg.updates.enabled:
        print("(updates: off; [updates] enabled = true turns them on)")
        return
    said = Updater(root, cfg, lambda text, buttons: tell_maintainers(cfg, root, text, buttons)).run_once()
    print(f"(updates: {said})")


def ha_config_key(root: Path) -> None:
    """--ha-config-key: a new HA_CONFIG_KEY in .env, to copy somewhere else too. Never printed."""
    from .haconfig import KEY_NAME, new_key

    env = root / ".env"
    if KEY_NAME in env_keys(env):
        raise SystemExit(
            f"There's a {KEY_NAME} in .env already. A new one couldn't open the snapshots the old one sealed: keep it."
        )
    key = new_key()
    with env.open("a") as f:
        f.write("\n# Seals Home Assistant's setup in the backup (docs/home.md). Keep a copy in a password manager.\n")
        f.write(f"{KEY_NAME}={key}\n")
    print(
        f"Added a new {KEY_NAME} to {env}. Copy it from there into a password manager yourself (it isn't shown here, "
        "so it stays out of logs and terminals): without it, a new machine can't open Home Assistant's setup from the "
        "backup. The next backup pass seals it."
    )


def ha_config_from_backup(cfg: Config, root: Path) -> None:
    """--ha-config-from-backup: the sealed history --restore-backup put back, opened into [home] config_dir."""
    from .haconfig import KEY_NAME, HAConfigError, unseal

    sealed = ha_bundle_path(cfg, root)
    if (ha := make_ha_config(cfg, root)) is None:
        raise SystemExit("Set config_dir under [home] first: that's where Home Assistant's setup goes (docs/home.md).")
    if not sealed.exists():
        raise SystemExit(f"There's no {sealed.name} here: voice-assistant --restore-backup puts it back first.")
    try:
        ha.clone(unseal(sealed.read_bytes(), api_key(root / ".env", KEY_NAME)))
    except (HAConfigError, ConfigError) as e:
        raise SystemExit(f"Couldn't open Home Assistant's setup: {e}") from None
    print(f"Home Assistant's setup is in {ha.folder}, with its history. Start Home Assistant: docker compose up -d")


def ha_config_history(cfg: Config, root: Path) -> None:
    """--ha-config: the latest changes, newest first."""
    from .haconfig import HAConfigError

    if (ha := make_ha_config(cfg, root)) is None:
        raise SystemExit("Set config_dir under [home] to keep Home Assistant's setup's history (docs/home.md).")
    if not ha.ready():
        raise SystemExit("There's no history yet: the next backup pass starts it (or voice-assistant --backup now).")
    try:
        changes = ha.history()
    except HAConfigError as e:
        raise SystemExit(f"Couldn't read the history: {e}") from None
    for c in changes:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.when))
        files = ", ".join(c.files[:4]) + (f" and {len(c.files) - 4} more" if len(c.files) > 4 else "")
        print(f"{c.commit[:8]}  {when}  {c.message}" + (f"\n          {files}" if files else ""))


def ha_config_restore(cfg: Config, root: Path, change: str) -> None:
    """--ha-config-restore: Home Assistant's setup as it was at `change`, kept as one more change."""
    from .haconfig import HAConfigError

    if (ha := make_ha_config(cfg, root)) is None or not ha.ready():
        raise SystemExit("There's no history of Home Assistant's setup to restore from (voice-assistant --ha-config).")
    try:
        made = ha.restore(change)
    except HAConfigError as e:
        raise SystemExit(f"Couldn't restore: {e}") from None
    print(f"Restored, kept as {made[:8]}; Home Assistant is starting again. To undo this: --ha-config-restore with "
          "the change before it, as --ha-config lists.")  # fmt: skip


def backup_pass(cfg: Config, root: Path, delete_anyway: bool = False) -> None:
    """--backup: one pass, as its timer runs it. Home Assistant's setup is committed first, so the snapshot has it."""
    from .backup import Busy

    ha_config_pass(cfg, root)
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


def typed(cfg: Config, root: Path) -> None:
    """--text: questions typed, answers spoken. No microphone, and the conversation isn't kept: nothing asked or
    answered is logged, and nothing is sent to the TARS page (brain_config). The event database is still opened for
    what the skills, the feeds, who's home and TARS itself keep there, so a skill taught, a feed followed or a wish
    noted here is kept as it would be aloud."""
    from .assistant import UNPROMPTED_WAIT_S, Assistant
    from .body import LocalBody

    made: list = []  # the assistant, once made: what the TV agent says when it's done is said through it
    # One at a time: the agent's line, from its own thread, never over a reply or in the middle of one.
    speaking = threading.Lock()

    def announce(line: str) -> None:
        print(f"\nTARS:  {line}\n\nYou:  ", end="", flush=True)
        if made:
            with speaking:
                if made[0].say(line, keep=False, wait_s=UNPROMPTED_WAIT_S):
                    made[0].brain.remember_said(line)

    household = Household(cfg, root)
    start_metering(cfg, household.events)
    home = make_home(cfg, root, announce, household.presence, own_speaker(cfg))
    _, brain, voice = make_pipeline(
        cfg,
        root,
        typed=True,
        home=home,
        feeds=household.feeds,
        skills=household.skills,
        myself=household.myself,
    )
    with Speaker(
        find_device(cfg.audio.output_device, "output"), voice.sample_rate, cfg.audio.playback_prebuffer_s
    ) as speaker:
        assistant = Assistant(LocalBody(None, speaker, None, None), None, brain, voice)
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

    household = Household(cfg, root)
    events = household.events
    start_heartbeat(events, "assistant", root)
    start_metering(cfg, events)
    speaker_id = household.speaker_id
    presence = household.presence
    reminders = make_reminders(cfg, household, push_to_talk)
    announcements = Announcements()
    clocks: list = []  # the skills' clock, once made: someone coming in by a door wakes it, for a "welcome home"
    start_presence(cfg, root, presence, reminders, lambda names, door: [c.nudge() for c in clocks])
    volume = own_speaker(cfg)
    home = make_home(cfg, root, announcements.add, presence, volume, events=events, background=True)
    start_auto_locks(cfg, root, announcements.add, presence, events, home)
    browser = voice_errands(cfg, events)
    skills = household.skills
    if skills and any(d.get("sensor") for d in cfg.presence.doors):
        from .skills import offer_welcome

        offer_welcome(skills)
    myself = household.myself
    transcriber, brain, voice = make_pipeline(
        cfg,
        root,
        reminders=reminders,
        home=home,
        browser=browser,
        feeds=household.feeds,
        skills=skills,
        myself=myself,
    )
    commands = house_commands(cfg, root, home)
    if commands and cfg.home.local_first:
        transcriber = local_first(cfg, root, commands)
    elif cfg.home.local_first:
        print("(house commands: none, with no devices in the house; local_first is off, the cloud hears every request)")
    journal = Journal(events)
    journal.keep_pruning(cfg.learning.keep_audio_days)
    with local_body(cfg, root, voice.sample_rate, push_to_talk, journal) as (body, idle_message):
        assistant = Assistant(
            body,
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
            skill_clock=(clock := skill_clock(skills, "aloud", home, presence, myself)),
            commands=commands,
        )
        clocks += [clock] if clock else []
        set_muted = body.set_muted = start_buttons(cfg, volume, assistant, root)
        if not push_to_talk:
            from .control import Server

            # Recording sessions, from the web UI; muting and unmuting, from a chat.
            Server(events.store.folder, assistant.control_ops(set_muted))
        assistant.run_forever(
            idle_message,
            follow_up_s=cfg.recorder.follow_up_s,
            greet_after_s=cfg.recorder.greet_after_s,
            greet=cfg.recorder.greet,
        )
