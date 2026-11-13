"""How TARS's parts are made from config.toml (the clients, the event log, who's home, the house, the brain and the
rest, each optional, each saying why when it's left out). entrypoints.py puts them together for each of
`voice-assistant`'s options, and check.py asks them whether they can be made. Heavy dependencies are imported lazily,
so each is loaded only by what uses it."""

import dataclasses
import functools
import hashlib
import json
import platform
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

from .audio import SAMPLE_RATE
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
    Config,
    ConfigError,
    LLMConfig,
    QuickProvider,
    with_keys,
)
from .haconfig import BUNDLE as HA_BUNDLE
from .versions import pair_source
from .wake import PushToTalkTrigger, wake_word_trigger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from .backup import S3, Backup
    from .body import LocalBody
    from .events import EventLog
    from .health import Report
    from .llm import QuickService
    from .stt import Transcriber

OPENAI_TIMEOUT_S = 15.0
BROWSER_TIMEOUT_S = 120.0  # a browser turn at high effort over a long errand's pages
QUICK_TIMEOUT_S = 4.0  # the quick brain's replies take well under a second
PHRASES = "voice_data/phrases"  # the short lines TARS makes ahead, kept across restarts
MUTED_FILE = "voice_data/muted"  # there while TARS is muted, so a restart keeps it muted


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


def make_browser_client(env_path: Path):
    """OpenAI's client for TARS's browser: a turn of an errand reasons over a long page history, which takes longer
    than a reply aloud may, and a passing failure is tried again rather than ending the errand."""
    from openai import OpenAI

    return OpenAI(api_key=api_key(env_path, OPENAI_KEY), timeout=BROWSER_TIMEOUT_S, max_retries=3)


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


def start_metering(cfg: Config, events: EventLog | None) -> None:
    """What this process uses of the paid services, counted in the event log (usage.py), the oldest rows pruned, and
    [usage] monthly_cap_usd held to; with no event log, nothing is counted or capped."""
    from . import usage
    from .maintainers import tell

    if events is None:
        return
    meter = usage.Meter(events.store)
    try:
        meter.prune()
    except Exception as e:  # noqa: BLE001 - counting goes on
        print(f"(usage: couldn't prune the oldest rows: {e!r})")
    usage.install(meter)
    if cfg.usage.monthly_cap_usd:

        def tell_maintainers(text: str) -> None:
            if not tell(cfg, events.store, text):
                print(f"(usage: no maintainer's chat to tell: {text})")

        usage.install_cap(usage.Cap(events.store, cfg.usage.monthly_cap_usd, tell_maintainers))


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


@contextmanager
def local_body(
    cfg: Config, root: Path, sample_rate: int, push_to_talk: bool, journal
) -> Iterator[tuple[LocalBody, str]]:
    """The Pi's body (body.py) for the assistant, open while it's in use: the mic and the speaker, with the echo
    canceller between them, the wake word, or Enter with `push_to_talk`, and the recorder; and what to show while it
    waits for its name. `sample_rate`: the voice's, which the speaker plays at."""
    from .audio import Microphone, Speaker, find_device
    from .body import LocalBody
    from .recorder import make_recorder

    recorder = make_recorder(cfg, root)
    echo = make_echo_canceller(cfg)
    speaker = Speaker(find_device(cfg.audio.output_device, "output"), sample_rate, cfg.audio.playback_prebuffer_s, echo)
    with Microphone(find_device(cfg.audio.input_device, "input"), echo) as mic, speaker:
        if echo:
            echo.set_delay(mic.latency + speaker.latency)
        trigger, idle_message = make_trigger(cfg, push_to_talk, root, journal)
        yield LocalBody(mic, speaker, trigger, recorder), idle_message


def make_speaker_id(cfg: Config, root: Path):
    from .speaker import SpeakerID

    return SpeakerID(root / cfg.speaker.model, root / cfg.speaker.voiceprints, cfg.speaker.threshold)


def make_health(cfg: Config, root: Path, log: EventLog) -> Callable[[], Report]:
    """What the web UI's Status page shows, as it is when asked (health.py)."""
    from .check import service_checks, systemd_state
    from .health import Health, Services, expected

    services = Services(lambda: service_checks(cfg, root).listed())
    return Health(log, expected(cfg), services, systemd_state, backup_enabled=cfg.backup.enabled).report


def make_reminders(cfg: Config, household: Household, push_to_talk: bool):
    """The voice's reminder tools, over the household's reminders, which need the event log to keep them in; and the
    wake loop to say them: not with push-to-talk, which waits on Enter instead. Waiting until someone's back needs
    their voiceprint, or their phone (the household's presence)."""
    if push_to_talk or cfg.wake.mode == "push-to-talk" or household.reminders is None:
        return None
    voices = household.speaker_id.names if household.speaker_id else list
    return reminder_tools(household.reminders, voices, household.presence)


def reminder_tools(reminders, voices, presence=None):
    """The brain's reminder tools, knowing people by `voices` (a callable) and, with `presence`, by their phone."""
    from .reminders import ReminderTools

    if presence is None:
        return ReminderTools(reminders, voices=voices)
    return ReminderTools(reminders, voices=voices, phones=lambda: presence.phones, is_home=presence.board.is_home)


def house_commands(cfg: Config, root: Path, home):
    """[home] commands_on_pi: the house's devices as phrases, their replies, and Vosk to hear them offline; None when
    it's off or there's nothing to command."""
    if not (cfg.home.commands_on_pi and home and home.devices):
        return None
    from .commands import WITTY_FROM_HUMOR, Commands, HouseCommands, Replies, device_words, vosk_hearing

    commands = Commands(device_words(home.devices))
    replies = Replies({name: d.display for name, d in home.devices.items()}, cfg.llm.humor >= WITTY_FROM_HUMOR)
    try:
        hear = vosk_hearing(commands.grammar(), root / "models")
    except Exception as e:  # noqa: BLE001 - commands still work from the cloud's transcript
        print(f"(house commands: no listening on the Pi without the cloud: {e!r})")
        hear = None
    print(f"(house commands: {sum(c is not None for c in commands.phrases.values())} phrases, handled on the Pi)")
    return HouseCommands(commands, replies, home.call, hear)


def local_first(cfg: Config, root: Path, commands):
    """[home] local_first: speech to text that hears a house command on the Pi before sending anything to the cloud;
    the cloud's endpoint for whole recordings takes the rest. Without Vosk, there's nothing to hear it with first."""
    from .commands import LocalFirstTranscriber

    if commands.hear is None:
        raise ConfigError("[home] local_first needs Vosk on this machine to hear house commands (see the log above).")
    env = root / ".env"
    keys = env_keys(env)
    cfg, _ = with_keys(cfg, keys)
    client = make_openai_client(env) if OPENAI_KEY in keys else None
    whole = make_recording_transcriber(cfg, env, client, spoken=True)
    commands.local_first = True
    print("(house commands: heard on the Pi first; the cloud hears only what isn't one)")
    return LocalFirstTranscriber(commands.hear, commands.commands.match, whole)


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


def start_presence(cfg: Config, root: Path, presence, reminders=None, came_in=None) -> None:
    """The assistant's: asks the routers from now on, listens to the doors' locks and sensors, and says waiting
    messages once their person is home, or as they come in by a door. `came_in(names, door)`: told then too (the
    skills' clock, so a "welcome home" runs at once)."""
    if presence is None:
        return
    from .reminders import came_in as messages_on_coming_in
    from .reminders import on_arrival

    for line in presence.left_out:
        print(f"(presence: leaving out {line}; until it's back nobody is marked out; see docs/presence.md)")
    told = [c for c in (reminders and messages_on_coming_in(reminders.reminders, cfg.presence.quiet), came_in) if c]

    def tell_came_in(names: list[str], door: str) -> None:
        for one in told:
            one(names, door)

    doors = make_doors(cfg, presence, tell_came_in)
    arrival = on_arrival(reminders.reminders, quiet=cfg.presence.quiet) if reminders else None
    changes = [c for c in (arrival, doors and doors.phone_came_home) if c]

    def changed(person: str, home: bool) -> None:
        for change in changes:
            change(person, home)

    watcher = presence.watch(changed if changes else None)
    if watcher:
        print(f"(presence: asking {len(presence.routers)} router(s) every {presence.every_s:.0f} s)")
    if doors:
        doors.watcher = watcher
        if watcher is None:
            print("(doors: no router to watch, so the doors are only recorded: nobody is marked home by them)")
        listen_to_doors(cfg, root, doors)
        follow_door_sensors(cfg, root, doors)


def make_doors(cfg: Config, presence, came_in=None):
    """[[presence.doors]]: what their locks say is kept, and learned from; None without any."""
    if not cfg.presence.doors:
        return None
    from . import maintainers
    from .doors import DoorLog, Doors

    store = presence.board.store
    return Doors(
        DoorLog(store, presence.people),
        None,
        lambda text: maintainers.tell(cfg, store, text),
        came_in=came_in,
    )


def follow_door_sensors(cfg: Config, root: Path, doors) -> None:
    """Each [[presence.doors]] sensor, followed in Home Assistant from now on (doors.Doors.sensed)."""
    sensors = {d["sensor"]: d["name"] for d in cfg.presence.doors if d.get("sensor")}
    if not sensors:
        return
    from .hass import follow
    from .home import TOKEN_KEY

    token = dotenv_values(root / ".env").get(TOKEN_KEY)
    if not (cfg.home.url and token):
        print(f"(doors: the door sensors are read from Home Assistant: set [home] url, and {TOKEN_KEY} in .env)")
        return

    def changed(entity: str, state: str) -> None:
        if state in ("on", "off"):
            doors.sensed(sensors[entity], state == "on")

    # moves_only: Home Assistant restarting, or the sensor heard again after a while, isn't the door moving.
    run = functools.partial(follow, cfg.home.url, token, list(sensors), changed, moves_only=True)
    threading.Thread(target=run, daemon=True, name="door-sensors").start()


def listen_to_doors(cfg: Config, root: Path, doors) -> None:
    """Listens to the locks on the broker, from now on: the log says when it's listening, and the maintainers are
    told if it can't for long (doors.Link)."""
    from .doors import Link, listen

    mqtt = cfg.presence.mqtt
    password = None
    if mqtt.get("secret"):
        password = dotenv_values(root / ".env").get(mqtt["secret"]) or None
        if password is None:
            print(f"(doors: leaving out the MQTT broker at {mqtt['address']}: no {mqtt['secret']} in .env)")
            return
    try:
        import paho.mqtt.client  # noqa: F401 - the door extra
    except ImportError:
        print("(doors: off: listening to the locks needs the door extra: uv sync --extra door)")
        return
    names = ", ".join(d["name"] for d in cfg.presence.doors)
    print(f"(doors: connecting to the MQTT broker at {mqtt['address']}, for the {names})")
    doors.link = Link(mqtt["address"], names, doors.tell)
    listen(mqtt, password, cfg.presence.doors, doors.heard, doors.link)
    doors.start()


def make_chat_when_out(cfg: Config, presence, reminders):
    """With [presence] and [telegram]: a reminder due aloud for someone who's out goes to their chat instead."""
    if not (presence and reminders and cfg.telegram.enabled and cfg.telegram.people):
        return None
    from .reminders import ChatWhenOut

    return ChatWhenOut(reminders.reminders, presence.board.is_out, cfg.telegram.people)


def make_home(
    cfg: Config, root: Path, announce=None, presence=None, speaker=None, mic=None, events=None, background=False
):
    """The devices in the house, as [home] lists them, with "who's home" given `presence`, TARS's own speaker given
    `speaker` (own_speaker's) and its microphone given `mic` (a chat's), as a HomeTools, or None. Like every service,
    optional: one that can't be made (no token, a missing extra, a plugin's own problem) is left out with a line saying
    why. `announce(line)`: how TARS says something on its own, later (how a task on the TV went)."""
    if not (cfg.home.devices or cfg.home.url or presence or speaker or mic):
        return None
    from .androidtv import make_tv_agent
    from .home import Context, build

    env = root / ".env"
    context = Context(root, lambda name: dotenv_values(env).get(name) or None, announce=announce)
    context.maintainers = list(cfg.household.maintainers)
    # `background`: this process (the voice assistant's) watches devices, and tells the maintainers through `events`.
    context.background = background
    if events is not None:
        from .maintainers import tell

        def tell_maintainers(text: str) -> None:
            if not tell(cfg, events.store, text):
                print(f"(no maintainer's chat to tell: {text})")

        context.tell = tell_maintainers
    context.tv_agent = functools.partial(make_tv_agent, cfg, env, announce) if announce else None
    extra = {}
    if presence:
        from .presence import DEVICE, WhoIsHome

        extra[DEVICE] = WhoIsHome(presence.board, presence.away_after_min)
    for own in (speaker, mic):
        if own:
            extra[own.name] = own
    for device in auto_lock_devices(cfg, root):
        extra[device.name] = device
    built = build(cfg.home, context, extra)
    if built.tools and (asked := {d["entity"]: a for d, _, _, a in auto_locks(cfg, root)}):
        built.tools.unlock_asked = lambda entity: asked[entity].mark() if entity in asked else None
    for name, why in built.unavailable.items():
        print(f"(home: the {name} isn't connected: {why}; see docs/home.md)")
    if built.tools and built.tools.devices:
        print(f"(home: the house has {', '.join(built.tools.devices)})")
    if built.tools and built.tools.ha:
        print("(home: and all of Home Assistant, with the home_assistant tool)")
    return built.tools


def auto_locks(cfg: Config, root: Path) -> list[tuple[dict, object, object, object]]:
    """Each lock in [home] with auto_lock and its door sensor: (its [[home.devices]], autolock.Settings,
    autolock.Off, autolock.Asked)."""
    from .autolock import Asked, Off, Settings

    found = []
    for d in cfg.home.devices:
        settings = Settings.of(d.get("auto_lock"))
        if settings and d.get("door") and str(d.get("entity", "")).startswith("lock."):
            folder = root / cfg.learning.folder
            found.append((d, settings, Off(folder, d["entity"]), Asked(folder, d["entity"])))
    return found


def auto_lock_devices(cfg: Config, root: Path) -> list:
    """The home tool's "<door> auto-lock" for each lock with one: on, or off for a while."""
    from .autolock import AutoLockDevice

    return [AutoLockDevice(d["name"].strip(), settings, off) for d, settings, off, _ in auto_locks(cfg, root)]


def start_auto_locks(cfg: Config, root: Path, say, presence=None, events=None, home=None) -> None:
    """The assistant's: locks each lock with auto_lock once its door is shut (autolock.py). `say(text)`: aloud at
    home. `home`: the house's tools (HomeTools), whose lock it holds to lock, as they do to change anything."""
    locks = auto_locks(cfg, root)
    if not locks:
        return
    from .autolock import AutoLock
    from .hass import HomeAssistant, follow
    from .home import TOKEN_KEY
    from .maintainers import tell
    from .reminders import MESSAGE, TELEGRAM, VOICE, NewReminder, Reminders

    token = dotenv_values(root / ".env").get(TOKEN_KEY)
    if not (cfg.home.url and token):
        print(f"(auto-lock: off: it works the lock through Home Assistant: set [home] url, and {TOKEN_KEY} in .env)")
        return
    store = events.store if events else None

    def post(text: str) -> None:
        """To everyone's Telegram chat."""
        for name in cfg.telegram.people if cfg.telegram.enabled and store else []:
            try:
                Reminders(store).add(
                    NewReminder(MESSAGE, text, for_name=name, due=time.time(), needs_ack=False, max_tries=1),
                    set_via=VOICE,
                    channel=TELEGRAM,
                    phones=[name],
                )
            except ValueError as e:
                print(f"(auto-lock: couldn't post to {name}: {e})")
        if not (cfg.telegram.enabled and store):
            say(text)

    def tell_maintainers(text: str) -> None:
        if not (store and tell(cfg, store, text)):
            print(f"(auto-lock: no maintainer's chat to tell: {text})")

    def anyone_home() -> bool | None:
        if not presence:
            return None
        states = [s.home for s in presence.board.states().values()]
        return True if any(states) else False if states and all(h is False for h in states) else None

    changing = home.changing if home else None
    for d, settings, off, asked in locks:
        hass = HomeAssistant(cfg.home.url, token)
        entity = d["entity"]

        def read(e: str, hass=hass) -> str:
            return str(hass.state(e).get("state", ""))

        def lock(hass=hass, entity=entity) -> bool:
            return hass.call("lock.lock", {"entity_id": entity})

        auto = AutoLock(
            d["name"].strip(), entity, d["door"], settings, off, read, lock, say, post, tell_maintainers, anyone_home,
            asked=asked, changing=changing,
        )  # fmt: skip
        auto.start(
            lambda entities, changed, subscribed: follow(
                cfg.home.url, token, entities, changed, unknown_too=True, on_subscribed=subscribed
            )
        )


def make_skills(events):
    """What TARS is taught, skills and preferences (skills.py), in the event database, or None without one."""
    from .skills import make

    return make(events)


def skill_clock(skills, say: str, home=None, presence=None, myself=None):
    """The clock for the skills that run by themselves and say what they have to `say` ("aloud": the voice's TARS;
    "chat": the Telegram service's, skills.ALOUD and CHAT), or None without skills. They're read again on every
    tick, so one taught a minute ago is kept to. With `home` (a HomeTools) and `presence`, it also watches the
    devices and who's home for the skills that run on something happening, and a trigger saved is checked against
    the devices and people there are. With `myself` (myself.py), the skills that run on TARS being updated are told
    when it is, and what changed. The one-offs are shared between the two: each is run by the process that answers
    where it was asked while that one's heartbeat says it's running, and otherwise by this one (skills.OneOffs)."""
    from .events import person_key
    from .health import running
    from .home import Request
    from .skills import Clock, OneOffs

    if not skills:
        return None
    folder = skills.store.folder
    one_offs = OneOffs(skills, say, lambda process: running(folder, process))
    people = status = came_by = None
    if presence:
        names = presence.board.people.names  # by person_key: as they're written

        def people() -> dict[str, bool | None]:
            return {names[key]: state.home for key, state in presence.board.states().items()}

        def came_by(name: str) -> str | None:
            state = presence.board.states().get(person_key(name))
            return state.via if state and state.home else None

        skills.people = lambda: set(names)
    if home:

        def status(device: str) -> str:
            return home.call({"device": device, "action": "status"}, Request())

        skills.devices, skills.status = (lambda: set(home.devices)), status
        skills.past = lambda device, start, end: home.devices[device].past(start, end)
    came_in = {}
    if presence:
        from .doors import DoorLog

        log = DoorLog(presence.board.store, presence.people)

        def since(after_id: int) -> list[dict]:
            return [r | {"person": names.get(r["person"], r["person"])} for r in log.came_in_since(after_id)]

        came_in = {"came_in": since, "last_came_in": log.last_id}
    updates = {}
    if myself:
        updates = {"version": myself.version, "between": myself.between}
        updates |= {"seen": skills.seen_version, "see": skills.see_version}
    clock = Clock(
        skills.all,
        say,
        people=people,
        came_by=came_by,
        status=status,
        record=skills.ran,
        one_offs=one_offs,
        **updates,
        **came_in,
    )
    return clock.start()


def own_speaker(cfg: Config):
    """With [home] own_speaker, TARS's own speaker as a device of the voice's (volume.py), or None, saying why, when
    its volume can't be read or set."""
    if not cfg.home.own_speaker:
        return None
    from .home import HomeError
    from .volume import DEVICE, OwnVolume, speaker_mixer

    try:
        return OwnVolume(speaker_mixer(cfg.audio.output_device))
    except HomeError as e:
        print(f"(home: {DEVICE} isn't connected: {e}; see docs/home.md)")
        return None


def chat_home(cfg: Config, root: Path, presence=None, events=None, announce=None):
    """The house as a Telegram chat has it: all the voice's, worked the same way, with who's home given `presence`
    and TARS's own speaker; unlocking only for [home] unlock_chats (home.py). `announce(line)`: how a task on the TV
    tells the chat that asked how it went. With `events`, the event log the assistant's socket is beside, TARS's
    microphone too, to mute and unmute it from afar: in a chat, "mute yourself" is about it, not the speaker."""
    mic = None
    if events:
        from .mic_mute import MicMute

        mic = MicMute(events.store.folder)
    return make_home(cfg, root, announce, presence, own_speaker(cfg), mic)


def ask_to_say(folder: Path, text: str) -> None:
    """Has the assistant say `text` aloud at home, word for word (its "say" op, over control.py)."""
    from .control import ask

    ask(folder, "say", text=text)


def make_pipeline(
    cfg: Config,
    root: Path,
    typed: bool = False,
    reminders=None,
    home=None,
    browser=None,
    feeds=None,
    skills=None,
    myself=None,
):
    """The cloud stages, as config.toml picks them: (transcriber, brain, voice); no transcriber for typed questions.
    With `reminders` (a ReminderTools), the brain can set them and knows what's set; with `home` (a HomeTools), it
    can work the devices in the house; with `browser` (browser/errands.py's BrowseTool), it can send TARS on errands;
    with `feeds` (feeds.py), it reads and manages the RSS feeds people follow; with `skills` (skills.py), it's taught
    skills and preferences, and knows them; with `myself` (myself.py), it knows itself."""
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
    brain = make_brain(
        client,
        brain_config(cfg, typed=typed),
        env,
        reminders,
        home=home,
        browser=browser,
        feeds=feeds,
        skills=skills,
        myself=myself,
    )
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


def make_recording_transcriber(cfg: Config, env: Path, client, spoken: bool = False) -> Transcriber:
    """Speech to text for a whole recording (a voice message), with the services make_pipeline picks for the voice at
    home: Deepgram's endpoint for whole recordings, with OpenAI's `client` as the backup, or whichever of the two
    there's a key for. `spoken`: a request said aloud ([home] local_first), with a spoken turn's waits rather than a
    voice message's. `cfg` has been through with_keys."""
    import httpx

    from .stt import (
        FILE_TIMEOUT_S,
        SPOKEN_FILE_TIMEOUT_S,
        DeepgramFileTranscriber,
        FallbackTranscriber,
        OpenAITranscriber,
    )

    if client is not None:
        # A voice message is a few minutes of audio, not one request; spoken, someone is waiting, so no retry.
        spoken_wait = {"timeout": OPENAI_TIMEOUT_S, "max_retries": 0}
        client = client.with_options(**(spoken_wait if spoken else {"timeout": FILE_TIMEOUT_S}))
    if cfg.stt.provider == "openai":
        return OpenAITranscriber(client, cfg.stt)
    http = httpx.Client(timeout=SPOKEN_FILE_TIMEOUT_S) if spoken else None
    deepgram = DeepgramFileTranscriber(api_key(env, DEEPGRAM_KEY), cfg.stt, http)
    if client is None:
        return deepgram
    return FallbackTranscriber(
        deepgram, OpenAITranscriber(client, dataclasses.replace(cfg.stt, model=OPENAI_STT_MODEL))
    )


def make_myself(cfg: Config, root: Path, events, wishes=None):
    """TARS knowing itself (myself.py): its version, changes, docs, records and log. `wishes`: the process's own
    Wishes, if it has one; otherwise one is made for offering them."""
    from .myself import Myself

    wishes = wishes or make_wishes(cfg, root, events)
    return Myself(cfg, root, events, offer=wishes.offer if wishes else None)


def make_wishes(cfg: Config, root: Path, events):
    """Wishes that come true (wishes.py), with [wishes] on and a database; None otherwise."""
    if not cfg.wishes.enabled or events is None:
        return None
    from .reminders import MESSAGE, TELEGRAM, VOICE, NewReminder, Reminders
    from .wishes import GitHub, GitHubError, Told, Wishes, running_has

    env = root / ".env"

    def github() -> GitHub:
        try:
            return GitHub(cfg.wishes.repo, api_key(env, cfg.wishes.secret))
        except ConfigError as e:
            raise GitHubError(str(e)) from None

    def person(name: str, via: str, text: str) -> None:
        # In their chat if they asked in one; otherwise the next time they're heard, never into an empty room.
        chat = via == "chat" and chat_of(cfg, name) is not None
        new = NewReminder(MESSAGE, text, for_name=name, due=time.time() if chat else None, needs_ack=False, max_tries=1)
        channel = TELEGRAM if chat else VOICE
        Reminders(events.store).add(new, set_via=channel, voices=[name], phones=[name], channel=channel)

    told = Told(lambda text, buttons: tell_maintainers(cfg, root, text, buttons), person)
    return Wishes(events.store, cfg.wishes, github, told, running_has(root))


def make_feeds(cfg: Config, events):
    """The feeds tool (feeds.py), in the event database, or None without one."""
    from .feeds import make

    return make(cfg.feeds, events)


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
    # The chat decides when its conversation ends (TelegramTars: memory_minutes, then keep_days with a check), so the
    # brain keeps it as long as that may be.
    memory = max(cfg.telegram.memory_minutes, cfg.telegram.keep_days * 24 * 60)
    llm = dataclasses.replace(brain_config(cfg, typed=False), memory_minutes=memory)
    if cfg.telegram.brain != "openai" or not has_openai:
        return llm
    quick_off = {provider.setting: "" for provider in QUICK_PROVIDERS}
    tg = cfg.telegram
    return dataclasses.replace(llm, service_tier=tg.service_tier, reasoning_effort=tg.reasoning_effort, **quick_off)


CONTINUITY_MAX_TOKENS = 400  # one word, with room for the quick model's reasoning before it


def make_continuity(cfg: Config, env_path: Path, client):
    """The check of whether a chat message carries its conversation on ([telegram] keep_days): the quick model, in
    [llm]'s order, else OpenAI's; None with neither, or keep_days 0."""
    if not cfg.telegram.keep_days:
        return None
    from .threads import Continuity

    services = quick_services(cfg.llm, env_path)
    effort = cfg.llm.quick_reasoning_effort or cfg.llm.reasoning_effort
    if services:

        def ask(messages: list[dict]) -> str:
            failed: Exception | None = None
            for service in services:
                try:
                    reply = service.client.chat.completions.create(
                        model=service.model,
                        messages=messages,
                        max_completion_tokens=CONTINUITY_MAX_TOKENS,
                        **({"reasoning_effort": effort} if effort else {}),
                    )
                    return reply.choices[0].message.content or ""
                except Exception as e:  # noqa: BLE001 - the next one is asked
                    failed = e
            raise failed or RuntimeError("no quick service")

        return Continuity(ask)
    if client is None:
        return None
    reasoning = {"reasoning": {"effort": cfg.llm.reasoning_effort}} if cfg.llm.reasoning_effort else {}

    def ask_openai(messages: list[dict]) -> str:
        system, *rest = messages
        reply = client.responses.create(
            model=cfg.llm.model,
            instructions=system["content"],
            input=rest,
            max_output_tokens=CONTINUITY_MAX_TOKENS,
            **reasoning,
        )
        return reply.output_text

    return Continuity(ask_openai)


def listen_for_update_taps(cfg: Config, root: Path, tars) -> None:
    """A maintainer's tap on an update's Install button installs it at once (updater.py)."""
    import subprocess

    from .updater import Updater, tap_listener

    maintainers = list(maintainer_chats(cfg).values())

    def start() -> None:
        # Its own unit, so the update outlives this service, which it restarts.
        subprocess.run(
            ["systemctl", "--user", "start", "--no-block", "voice-assistant-update.service"], check=False, timeout=10
        )

    updater = Updater(root, cfg, lambda text, buttons: tell_maintainers(cfg, root, text, buttons))
    tars.listen_first(tap_listener(updater, maintainers, tars.bot, start))


def spotify_login(cfg: Config, root: Path, env: Path):
    """How /spotify signs in, on the browser's one-time page and port; None without SPOTIFY_CLIENT_ID."""
    from dotenv import dotenv_values

    from .spotify import CLIENT_KEY, TOKEN_FILE, page_login

    client = dotenv_values(env).get(CLIENT_KEY) if env.exists() else None
    if not client:
        return None
    from .browser.login_page import reachable_address

    host = cfg.browser.login_host or reachable_address()
    return functools.partial(page_login, client, root / TOKEN_FILE, host, cfg.browser.login_port)


def errand_queue(cfg: Config, events, has_openai: bool):
    """[browser]'s queue of errands (browser/errands.py), in the event database, or None with it off. ConfigError
    if the browser can't run here."""
    if not cfg.browser.enabled:
        return None
    if not has_openai:
        raise ConfigError(f"[browser] needs {OPENAI_KEY} in .env: an OpenAI model works the browser.")
    if events is None:
        raise ConfigError("[browser] needs [learning] log_events: errands are queued in the event database.")
    try:
        import cryptography  # noqa: F401 - only whether they're there
        import playwright  # noqa: F401

        from .browser.errands import Queue
    except ImportError:
        raise ConfigError('[browser] needs the optional extra "browser": run uv sync --all-extras') from None
    return Queue(events.store)


def browse_tool(cfg: Config, queue, via: str):
    """The brain's browse tool, for errands asked for this way ("telegram" or "voice"), or None without a queue."""
    if queue is None:
        return None
    from .browser.errands import BrowseTool

    return BrowseTool(queue, list(cfg.telegram.people), via)


def make_errands(cfg: Config, root: Path, client, queue, tars, reminders=None, presence=None):
    """[browser]'s errands, run by the Telegram service with OpenAI's `client` from `queue`, reaching the person in
    their chat (`tars`); with `reminders` (a ReminderTools), the answer to one asked aloud is said aloud."""
    from .browser.errands import ERRANDS, Errands
    from .homenet import configured
    from .telegram_errands import TelegramErrands

    orders = "orders placed once approved" if cfg.browser.really_order else "approved orders a dry run"
    print(f"(errands in the browser, by {cfg.browser.model} at {cfg.browser.effort} effort; {orders})")
    chats = TelegramErrands(tars, cfg.browser.login_host, cfg.browser.login_port)
    say = errand_said_aloud(reminders.reminders, presence) if reminders else None
    home = configured(cfg.web.allowed_hosts, cfg.home.url)  # the house's own names: never opened either
    return Errands(cfg.browser, chats, list(cfg.telegram.people), queue=queue, say_aloud=say, folder=root / ERRANDS,
                   openai=client, home=home)  # fmt: skip


def errand_said_aloud(reminders, presence=None):
    """How an errand asked for aloud has its answer said aloud at home: as a message for its person, said once, as a
    reminder is. Not for someone known to be out: it's in their chat."""
    from .reminders import MESSAGE, VOICE, NewReminder

    def say(person: str, text: str) -> None:
        if presence is not None and presence.board.is_out(person):
            return
        reminders.add(NewReminder(MESSAGE, text, for_name=person, due=time.time(), needs_ack=False, max_tries=1), VOICE)

    return say


def voice_errands(cfg: Config, events):
    """The browse tool for the voice (browser/errands.py), with [browser] on: an errand asked for aloud goes into
    the queue in the event database, for the Telegram service to run. None without the browser, Telegram or that
    database."""
    if not (cfg.browser.enabled and cfg.telegram.enabled and cfg.telegram.people):
        return None
    if events is None:
        print("(browser: errands asked aloud need [learning] log_events, where they're queued)")
        return None
    from .browser.errands import Queue

    return browse_tool(cfg, Queue(events.store), "voice")


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
        **({HA_BUNDLE: ha_bundle_path(cfg, root)} if cfg.home.config_dir else {}),
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


def make_ha_config(cfg: Config, root: Path):
    """Home Assistant's setup's history ([home] config_dir, haconfig.py); None if it isn't kept."""
    from .haconfig import HAConfig

    if not cfg.home.config_dir:
        return None
    return HAConfig(root / cfg.home.config_dir, cfg.home.container)


def ha_bundle_path(cfg: Config, root: Path) -> Path:
    """Where the latest bundle of Home Assistant's setup's history waits for the backup's next snapshot."""
    return root / cfg.learning.folder / HA_BUNDLE


def chat_of(cfg: Config, person: str) -> int | None:
    """`person`'s Telegram chat in [telegram] people, whatever the case of their name."""
    me = person.strip().lower()
    return next((chat for name, chat in cfg.telegram.people.items() if name.strip().lower() == me), None)


def maintainer_chats(cfg: Config) -> dict[str, int]:
    """Each [household] maintainer with a Telegram chat: their name, as [telegram] people has it, and the chat."""
    chats = {name.strip().lower(): (name, chat) for name, chat in cfg.telegram.people.items()}
    return dict(chats[m.strip().lower()] for m in cfg.household.maintainers if m.strip().lower() in chats)


def tell_maintainers(cfg: Config, root: Path, text: str, buttons: list[tuple[str, str]] = ()) -> list[str]:
    """Posts `text` (with buttons, if any) to each maintainer's Telegram chat, straight through Telegram's API: the
    Telegram service may be what's restarting. A chat that can't be reached costs a line. The maintainers reached."""
    from .telegram import Bot, TelegramError

    targets = maintainer_chats(cfg)
    if not cfg.telegram.enabled or not targets:
        print(f"(no maintainer's chat to tell: {text})")
        return []
    try:
        bot = Bot(api_key(root / ".env", TELEGRAM_KEY))
    except ConfigError as e:
        print(f"(can't tell the maintainers: {e})")
        return []
    reached = []
    for name, chat in targets.items():
        try:
            bot.send_buttons(chat, text, list(buttons)) if buttons else bot.send(chat, text)
        except TelegramError as e:
            print(f"(couldn't tell a maintainer on Telegram: {e})")
            continue
        reached.append(name)
    return reached


def phrase_folder(cfg: Config, root: Path) -> Path:
    """Where the short lines' audio is kept: one folder per [tts] setup, so a new voice makes them again."""
    setup = json.dumps(dataclasses.asdict(cfg.tts), sort_keys=True)
    return root / PHRASES / hashlib.sha256(setup.encode()).hexdigest()[:12]


def start_buttons(cfg: Config, volume, assistant, root: Path):
    """The buttons, from threads of their own (buttons.py): with TARS's own speaker (`volume`, own_speaker's), its
    volume keys and the remote's volume buttons turn it; the remote's talk and stop wake and stop `assistant`; and
    the speakerphone's own mute (speakerphone.py), which the remote's mute button works too, keeps it from waking.
    Muted when it last stopped (MUTED_FILE), it starts muted. Linux only: elsewhere there's no /dev/input or hidraw
    to read them from. Returns how to mute it as the mute button does (a chat's, over control.py), or None."""
    if platform.system() != "Linux":
        return None
    from .buttons import DOWN, MUTE, STOP, TALK, UP, Buttons, Listener
    from .speakerphone import find_mute, kept

    flag = root / MUTED_FILE
    was_muted = flag.exists()
    muted = kept(flag, assistant.mute)
    act = {TALK: assistant.talk, STOP: assistant.stop}
    if volume is not None:
        act |= {UP: lambda: volume.turn(True), DOWN: lambda: volume.turn(False)}
    mute = None
    if cfg.home.speakerphone_mute:
        mute, why = find_mute(cfg.audio.output_device, muted)
        if mute:
            mute.start()
            print("(buttons: the speakerphone's mute is TARS's: while it's muted, TARS doesn't listen)")
        else:
            print(f"(buttons: the speakerphone's mute isn't TARS's: {why}; docs/home.md#buttons)")

    def toggle() -> str:  # without the speakerphone's mute, TARS's own: it stops listening, the mic stays on
        muted(not assistant.muted)
        return "muted (TARS only)" if assistant.muted else "unmuted"

    act[MUTE] = mute.toggle if mute else toggle
    keys = cfg.home.volume_keys and volume is not None
    remote = {b: c for b, c in cfg.home.remote.items() if b in act}
    if was_muted:  # muted again, unless nothing could unmute it now: then the flag goes
        if mute:
            mute.set(True)
        elif MUTE in remote:
            muted(True)
        else:
            flag.unlink(missing_ok=True)
        if assistant.muted:
            print("(muted, as it was when TARS last stopped)")
    set_muted = mute.set if mute else muted
    if not (keys or remote):
        return set_muted
    Listener(Buttons(remote, keys), act).start()
    which = (["the volume keys"] if keys else []) + ([f"the remote's {', '.join(remote)}"] if remote else [])
    print(f"(buttons: {' and '.join(which)})")
    return set_muted


class Household:
    """The parts of TARS that the assistant, typed questions, the web UI and the Telegram service each make the same
    way from config.toml: the event log, who's home, the voiceprints, the reminders, the skills, the feeds, the wishes
    and TARS knowing itself. Each is made the first time it's asked for, and kept: a process pays only for what it
    asks for, in the order it asks. Nothing here starts a thread, listens or opens a device; starting is the
    process's own (start_heartbeat, start_presence). What a process makes its own way, the house, the doors, the
    errands, the brain, isn't here."""

    def __init__(self, cfg: Config, root: Path):
        self.cfg, self.root = cfg, root

    @functools.cached_property
    def events(self) -> EventLog | None:
        return make_event_log(self.cfg, self.root)

    @functools.cached_property
    def presence(self):
        """Who's home (make_presence), only read until the assistant's start_presence asks the routers."""
        return make_presence(self.cfg, self.root, self.events)

    @functools.cached_property
    def speaker_id(self):
        """The voiceprints (speaker.py), with [speaker] on; None otherwise."""
        return make_speaker_id(self.cfg, self.root) if self.cfg.speaker.enabled else None

    @functools.cached_property
    def reminders(self):
        """The reminders' table (reminders.Reminders), with [reminders] on and an event log; None otherwise. Each
        process gives the brain its own tools over it (reminder_tools), knowing people its own way."""
        if not (self.cfg.reminders.enabled and self.events):
            return None
        from .reminders import Reminders

        return Reminders.from_config(self.events.store, self.cfg.reminders)

    @functools.cached_property
    def skills(self):
        return make_skills(self.events)

    @functools.cached_property
    def feeds(self):
        return make_feeds(self.cfg, self.events)

    @functools.cached_property
    def wishes(self):
        return make_wishes(self.cfg, self.root, self.events)

    @functools.cached_property
    def myself(self):
        """TARS knowing itself, offering wishes through the household's own (wishes)."""
        return make_myself(self.cfg, self.root, self.events, self.wishes)
