"""An Android TV box (a Fire TV Stick, a Chromecast with Google TV, a Shield) over ADB on the home network: TARS
presses its buttons, opens apps, reads what's on screen (the app in front, what's playing, the text the app shows)
and takes screenshots. With HDMI-CEC, waking the box wakes the TV, so "turn on the TV" can go through here.

Anything that takes several steps ("play Toy Story on Stremio") goes to TvAgent: a model that works the TV the way a
person would, looking after every step and checking the result, in the background. TARS answers "On it." at once and
says how it went when it's done, so the conversation never waits on a search screen.

ADB has to be on (on a Fire TV: Settings > My Fire TV > Developer Options > ADB debugging), and TARS's key allowed once
on the TV ("Allow USB debugging?", with "Always allow" ticked): python -m voice_assistant.androidtv pair."""

import base64
import contextlib
import json
import re
import shlex
import threading
import time
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

from .found import FOUND_FILE, Seen, find_by_mac, mac_of, reachable
from .home import DO, OFF, ON, OPEN, PLAY, PRESS, STATUS, Check, Context, Device, HomeError, Request, button, presses

# The remote's buttons, as Android's key codes.
KEYS = {
    "up": "KEYCODE_DPAD_UP",
    "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
    "select": "KEYCODE_DPAD_CENTER",
    "enter": "KEYCODE_ENTER",  # not on the remote: what submits a search typed with type_text
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "menu": "KEYCODE_MENU",
    "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE",
    "play": "KEYCODE_MEDIA_PLAY",
    "pause": "KEYCODE_MEDIA_PAUSE",
    "rewind": "KEYCODE_MEDIA_REWIND",
    "fast_forward": "KEYCODE_MEDIA_FAST_FORWARD",
    "next": "KEYCODE_MEDIA_NEXT",
    "previous": "KEYCODE_MEDIA_PREVIOUS",
}
# Apps by what people call them, as Fire TV and Google TV package them; anything else is found among the packages
# installed.
APPS = {
    "stremio": "com.stremio.one",
    "netflix": "com.netflix.ninja",
    "youtube": "com.amazon.firetv.youtube",
    "youtube on google tv": "com.google.android.youtube.tv",
    "prime video": "com.amazon.avod",
    "disney+": "com.disney.disneyplus",
    "disney plus": "com.disney.disneyplus",
    "spotify": "com.spotify.tv.android",
    "plex": "com.plexapp.android",
    "kodi": "org.xbmc.kodi",
    "apple tv": "com.apple.atve.amazon.appletv",
    "max": "com.wbd.stream",
}
PLAYBACK = {"0": "nothing", "1": "stopped", "2": "paused", "3": "playing", "6": "buffering", "7": "error"}
SETTLE_S = 4.0  # how long waking or sleeping gets to show on the box
# Opening an app: it gets OPEN_WAIT_S to come to the front, and must still be there STAY_S later, since some (Stremio)
# now and then crash a moment after starting, the first time after the box boots. Then it's opened again, at most
# OPEN_TRIES times.
OPEN_WAIT_S, STAY_S, OPEN_TRIES = 8.0, 3.0, 3
POLL_S = 0.5
AGENT_STEPS = 30
TASK_TIMEOUT_S = 150.0  # all of a task's steps together
STEP_TIMEOUT_S = 40.0  # one step: the model's answer, a screenshot to read
# How long a new task, or someone's own command, waits for the step the agent is on to end (a model's answer can't be
# cut short: it's thrown away when it comes).
STOP_WAIT_S = 5.0
# Working a remote is many small, easy choices: at the model's default effort each step thought for seconds.
AGENT_EFFORT = "low"
UI_DUMP = "/sdcard/tars_ui.xml"
KEY_FILE = "voice_data/androidtv/adbkey"  # TARS's ADB key: the box asks once whether to trust it
CONNECT_TIMEOUT_S = 3.0
# A connection unused this long may have gone (the box slept, or moved): it's checked before a press goes over it.
IDLE_S = 30.0


class AndroidTV:
    """One ADB connection, made when first needed and again after it drops. Every call is short, and a box that
    doesn't answer is a HomeError."""

    def __init__(
        self,
        address: str,
        key_path: Path,
        auth_timeout_s: float = 3.0,
        seen: Seen | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """`seen`: where the box was last found, and its hardware address, so that a box the router gave another
        address is found again (found.py); None: only ever at `address`."""
        host, _, port = address.partition(":")
        self._key = f"androidtv:{address}"  # the address in config.toml names the box, wherever it is now
        self._seen = seen
        self._host, self._port = (seen.get(self._key).get("address") if seen else None) or host, int(port or 5555)
        self._key_path, self._auth_timeout_s = key_path, auth_timeout_s
        self._adb = None
        self._lock = threading.RLock()
        self._clock, self._used = clock, 0.0

    def _locate(self) -> None:
        """If the box doesn't answer where it was, looks for its hardware address on the home network."""
        mac = self._seen.get(self._key).get("mac") if self._seen else None
        if not mac or reachable(self._host, self._port, CONNECT_TIMEOUT_S):
            return
        found = find_by_mac(mac, self._port, near=self._host)
        if found and found != self._host:
            print(f"(tv: the box moved from {self._host} to {found}: the router gave it a new address)", flush=True)
            self._host = found

    def _remember(self) -> None:
        if self._seen is not None:
            known = self._seen.get(self._key)
            mac = mac_of(self._host) or known.get("mac")
            if known != {**known, "address": self._host, "mac": mac}:
                self._seen.put(self._key, address=self._host, mac=mac or "")

    def _device(self):
        if self._adb is not None:
            return self._adb
        from adb_shell.adb_device import AdbDeviceTcp
        from adb_shell.auth.keygen import keygen
        from adb_shell.auth.sign_pythonrsa import PythonRSASigner

        if not self._key_path.exists():
            self._key_path.parent.mkdir(parents=True, exist_ok=True)
            keygen(str(self._key_path))
        signer = PythonRSASigner(Path(f"{self._key_path}.pub").read_text(), self._key_path.read_text())
        self._locate()
        # Short: a box that's unplugged or off the Wi-Fi is said to be, rather than leaving a voice turn waiting.
        adb = AdbDeviceTcp(self._host, self._port, default_transport_timeout_s=CONNECT_TIMEOUT_S)
        try:
            adb.connect(rsa_keys=[signer], auth_timeout_s=self._auth_timeout_s)
        except Exception as e:
            raise HomeError(f"the TV box didn't answer at {self._host} ({type(e).__name__}: {e})") from e
        self._adb = adb
        self._remember()
        return adb

    def _run(self, command: str, decode: bool = True, timeout_s: float = 10.0, repeatable: bool = False):
        """`repeatable`: running it twice does no harm (a read, waking the box), so it's tried again on a new
        connection whatever went wrong. Anything else (a press, typing) is tried again only after a connection error,
        which usually means it never went out: after a timeout the box may have done it, and twice would press twice.
        So a connection left idle is checked first, where trying again is safe."""
        with self._lock:
            if not repeatable and self._adb is not None and self._clock() - self._used > IDLE_S:
                self._run("echo", repeatable=True)
            for attempt in (1, 2):
                try:
                    out = self._device().shell(command, read_timeout_s=timeout_s, timeout_s=timeout_s, decode=decode)
                    self._used = self._clock()
                    return out
                except HomeError:
                    raise
                except Exception as e:
                    self.close()
                    if attempt == 2:
                        raise HomeError(f"the TV box stopped answering ({type(e).__name__})") from e
                    if not (repeatable or isinstance(e, ConnectionError)):
                        why = f"the TV box didn't answer in time ({type(e).__name__}); it may have done it all the same"
                        raise HomeError(why) from e
        raise AssertionError("unreachable")

    def shell(self, command: str, timeout_s: float = 10.0, repeatable: bool = False) -> str:
        return self._run(command, timeout_s=timeout_s, repeatable=repeatable) or ""

    def close(self) -> None:
        with self._lock:
            if self._adb is not None:
                with contextlib.suppress(Exception):  # closing one that's gone is as good as closing it
                    self._adb.close()
            self._adb = None

    # What it's doing.

    def awake(self) -> bool:
        return "mWakefulness=Awake" in self.shell("dumpsys power | grep mWakefulness=", repeatable=True)

    def app(self) -> str:
        """The package in front ("com.stremio.one"), or ""."""
        # Not "dumpsys window windows": on Fire OS 8 (Android 11) that prints nothing.
        out = self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'", repeatable=True)
        found = re.search(r"\s([\w.]+)/[\w.$]+", out)
        return found.group(1) if found else ""

    def media(self) -> list[tuple[str, str, str]]:
        """What the media sessions say: (package, playing/paused/..., title), the active ones first."""
        out = self.shell("dumpsys media_session", repeatable=True)
        sessions = []
        for block in re.split(r"(?=^\s*package=)", out, flags=re.MULTILINE):  # each session's lines start there
            package = re.search(r"package=([\w.]+)", block)
            state = re.search(r"state=PlaybackState \{state=(\d+)", block)
            if not (package and state):
                continue
            title = re.search(r"description=([^,\n]+)", block)
            active = "active=true" in block
            entry = (package.group(1), PLAYBACK.get(state.group(1), "unknown"), title.group(1).strip() if title else "")
            sessions.insert(0 if active else len(sessions), entry)
        return sessions

    def playing(self) -> str:
        """One line on what's playing, if anything is."""
        for package, state, title in self.media():
            if state in ("playing", "paused", "buffering"):
                return f"{state}: {title or 'something'} ({package})"
        return "nothing playing"

    def ui(self) -> str:
        """The text on screen, as the app describes it to accessibility tools: one line per element, the focused one
        marked. Empty if the app doesn't say (games, some video players): then only a screenshot shows it."""
        dump = f"uiautomator dump {UI_DUMP} >/dev/null 2>&1; cat {UI_DUMP}; rm -f {UI_DUMP}"
        out = self.shell(dump, timeout_s=15, repeatable=True)
        start = out.find("<?xml")
        if start < 0:
            return ""
        try:
            root = ElementTree.fromstring(out[start:])
        except ElementTree.ParseError:
            return ""
        lines = []
        for node in root.iter("node"):
            words = (node.get("text") or "").strip() or (node.get("content-desc") or "").strip()
            focused = node.get("focused") == "true"
            if words or focused:
                lines.append(f"{'[focused] ' if focused else ''}{words or '(no text)'} @{node.get('bounds', '')}")
        return "\n".join(dict.fromkeys(lines))[:6000]

    def screenshot(self) -> bytes:
        png = self._run("screencap -p", decode=False, timeout_s=15, repeatable=True)
        if not png or not png.startswith(b"\x89PNG"):
            raise HomeError("the TV box didn't give a screenshot")
        return png

    def packages(self) -> list[str]:
        out = self.shell("pm list packages", repeatable=True)
        return sorted(line.removeprefix("package:").strip() for line in out.splitlines() if line.startswith("package:"))

    # Doing things.

    def wake(self) -> None:
        self.shell("input keyevent KEYCODE_WAKEUP", repeatable=True)

    def sleep(self) -> None:
        self.shell("input keyevent KEYCODE_SLEEP", repeatable=True)

    def press(self, key: str, times: int = 1) -> None:
        """One `input` for all the presses: each starts Android's input tool, about a second on a Fire TV."""
        self.shell("input keyevent " + " ".join([KEYS[key]] * times))

    def type(self, text: str) -> None:
        """Types into whatever has focus, in place of what's there. `input text` takes no spaces: they go as %s."""
        typed = shlex.quote(text.replace(" ", "%s"))
        self.shell(f"input keyevent KEYCODE_MOVE_END {' '.join(['KEYCODE_DEL'] * CLEAR_CHARS)}; input text {typed}")

    def open(self, package: str) -> None:
        """Brings it to the front: opening one already open does nothing more, so it's repeatable."""
        self.shell(f"monkey -p {shlex.quote(package)} -c android.intent.category.LAUNCHER 1", repeatable=True)

    def open_link(
        self, uri: str, package: str, component: str = "", flags: str = "", extras: dict | None = None
    ) -> None:
        """Opens `uri` in `package` (Android's VIEW intent), returning once the app is up. Each part is quoted for the
        box's shell, so a link can't run anything of its own there."""
        q = shlex.quote
        parts = ["am start -W -a android.intent.action.VIEW", f"-d {q(uri)}"]
        parts += [f"-f {q(flags)}"] * bool(flags) + [f"-e {q(k)} {q(str(v))}" for k, v in (extras or {}).items()]
        self.shell(" ".join([*parts, f"-n {q(component)}" if component else q(package)]), timeout_s=15, repeatable=True)

    def force_stop(self, package: str) -> None:
        self.shell(f"am force-stop {shlex.quote(package)}", repeatable=True)

    def find_app(self, name: str) -> str | None:
        """The package for an app's name, among those installed: the known one, the package itself, or the shortest
        package that mentions it (an app may be packaged differently on each kind of box)."""
        wanted = name.strip().lower()
        installed = self.packages()
        if APPS.get(wanted) in installed:
            return APPS[wanted]
        if wanted in installed:
            return wanted
        squeezed = re.sub(r"[^a-z0-9]", "", wanted)
        matches = [p for p in installed if squeezed and squeezed in p.replace(".", "")]
        return min(matches, key=len) if matches else None


def open_app(
    box: AndroidTV,
    package: str,
    sleep: Callable[[float], None] = time.sleep,
    stopped: threading.Event | None = None,
) -> tuple[bool, int, str]:
    """Opens `package` and checks it stayed open: (it did, the tries it took, the package in front at the end). An app
    that never came up, or crashed a moment after, is stopped and opened again, up to OPEN_TRIES times. `stopped`:
    the agent was stopped, so this returns at once, touching nothing more: someone else is working the TV now."""
    front = ""
    for attempt in range(1, OPEN_TRIES + 1):
        if stopped and stopped.is_set():
            return False, attempt, front
        if not box.awake():
            box.wake()
            sleep(2.0)
        box.open(package)
        came = wait_for(lambda: box.app() == package, OPEN_WAIT_S, sleep, stopped)
        stayed = came and not wait_for(lambda: box.app() != package, STAY_S, sleep, stopped)
        front = box.app()
        if stayed:
            return True, attempt, front
        if stopped and stopped.is_set():
            return False, attempt, front
        print(f"(tv: {package} {'closed again' if came else 'never came up'}; try {attempt} of {OPEN_TRIES})")
        box.force_stop(package)
        sleep(1.0)
    return False, OPEN_TRIES, front


def wait_for(
    happened: Callable[[], bool],
    seconds: float,
    sleep: Callable[[float], None],
    stopped: threading.Event | None = None,
) -> bool:
    """Whether `happened` comes true within `seconds`, looked at every POLL_S; False at once if `stopped` is set."""
    for _ in range(int(seconds / POLL_S) + 1):
        if stopped and stopped.is_set():
            return False
        if happened():
            return True
        sleep(POLL_S)
    return False


def pause(seconds: float, sleep: Callable[[float], None], stopped: threading.Event | None = None) -> None:
    """Waits `seconds`, a POLL_S at a time, ending early if `stopped` is set."""
    wait_for(lambda: False, seconds, sleep, stopped)


def screen(box: AndroidTV) -> str:
    """What's on screen, as text: the app in front, what's playing, and what the app shows."""
    if not box.awake():
        return "asleep: the screen is off"
    app, playing, ui = box.app(), box.playing(), box.ui()
    return f"app: {app or 'none'}\n{playing}\non screen:\n{ui or '(no text: take a screenshot)'}"


CLEAR_CHARS = 40  # what typing deletes first: longer than any search left in a box

AGENT_RULES = """You work an Android TV box for someone in the house, the way a person with its remote would, to do \
what they asked: {goal}

You have its remote's buttons, typing into whatever has focus, opening apps, and two ways to see the screen: look (the \
app in front, what's playing, and the text the app shows, the focused element marked) and screenshot. Pressing, typing \
and opening an app each answer with the screen as it is after: check it did what you meant before the next step; look \
again only after waiting, and when the screen shows no text, take a screenshot. If the app closes \
or you're back on the TV's home screen without meaning to be, open the app again. Move the focus with \
the arrows and choose with select; back goes back. TV apps often take a few seconds to load: wait, then look again.

To play something: open the app, find it, choose the right title (the best known one, unless they said which), and \
start it. If it's already on screen (in Continue Watching, say), choose it there. Otherwise search: type_text into the \
search box, then press enter to search (select on the on-screen keyboard types a letter), and look for the results. \
It's playing only when look says playing, or a screenshot shows the video itself.

Finish with done: success true only when what they asked for is on screen, and in say, one short spoken line for them \
("Toy Story is playing."); if it can't be done, success false and why, in one line ("Stremio found no streams for Toy \
Story."). If it's not clear which they meant (two films of that name), stop and ask in say."""

AGENT_TOOLS = [
    {"type": "function", "name": "look", "description": "What's on screen, as text.", "strict": True,
     "parameters": {"type": "object", "additionalProperties": False, "required": [], "properties": {}}},
    {"type": "function", "name": "screenshot", "description": "A picture of the screen.", "strict": True,
     "parameters": {"type": "object", "additionalProperties": False, "required": [], "properties": {}}},
    {"type": "function", "name": "press", "description": "Press a button on the remote, once or more.", "strict": True,
     "parameters": {"type": "object", "additionalProperties": False, "required": ["button", "times"],
                    "properties": {"button": {"type": "string", "enum": list(KEYS)}, "times": {"type": "integer"}}}},
    {"type": "function", "name": "type_text", "description": "Type into the focused search box; then enter searches.",
     "strict": True,
     "parameters": {"type": "object", "additionalProperties": False, "required": ["text"],
                    "properties": {"text": {"type": "string"}}}},
    {"type": "function", "name": "open_app", "description": "Open an app by its name.", "strict": True,
     "parameters": {"type": "object", "additionalProperties": False, "required": ["app"],
                    "properties": {"app": {"type": "string"}}}},
    {"type": "function", "name": "wait", "description": "Wait for the screen to load.", "strict": True,
     "parameters": {"type": "object", "additionalProperties": False, "required": ["seconds"],
                    "properties": {"seconds": {"type": "number"}}}},
    {"type": "function", "name": "done", "description": "Finish, saying how it went.", "strict": True,
     "parameters": {"type": "object", "additionalProperties": False, "required": ["success", "say"],
                    "properties": {"success": {"type": "boolean"}, "say": {"type": "string"}}}},
]  # fmt: skip


def _without_pictures(items: list) -> list:
    """Earlier screenshots as a line each: the whole list is sent again every step, and every picture kept in it
    would make each step dearer and slower than the one before."""
    return [
        {"role": "user", "content": "(an earlier screenshot)"} if isinstance(i.get("content"), list) else i
        for i in items
    ]


class TvAgent:
    """Works the TV toward a goal in the background, a model choosing each step from what it sees, and says how it
    went through `announce(line)`. One goal at a time: a new one, or a person pressing a button, stops the
    one before."""

    def __init__(
        self,
        box: AndroidTV,
        client,
        model: str,
        announce: Callable[[str], None],
        steps: int = AGENT_STEPS,
        timeout_s: float = TASK_TIMEOUT_S,
        sleep: Callable[[float], None] = time.sleep,
        service_tier: str = "",
        extra_rules: str = "",
    ):
        """`service_tier`: OpenAI's, as [llm] has it ("fast"); "" = the default. `extra_rules`: what the household's
        own apps need besides AGENT_RULES, from a plugin (examples/my_house: how Stremio lists streams)."""
        self.box, self._client, self.model, self._announce = box, client, model, announce
        self._extra_rules = extra_rules
        self._extra = {"reasoning": {"effort": AGENT_EFFORT}} | ({"service_tier": service_tier} if service_tier else {})
        self._steps, self._timeout_s, self._sleep = steps, timeout_s, sleep
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self.log: list[str] = []  # the last goal's steps, for --check and the logs

    def start(self, goal: str, wait: bool = False) -> None:
        self.stop()
        self._stopped = stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(goal, stopped), daemon=True, name="tv-agent")
        self._thread.start()
        if wait:
            self._thread.join()

    def stop(self) -> None:
        """Stops the goal being worked on, and returns once the step it's on has ended (a wait, or an app being
        opened, ends early), or after STOP_WAIT_S: whatever comes next can't be fought over by both."""
        self._stopped.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_WAIT_S)

    def _run(self, goal: str, stopped: threading.Event) -> None:
        try:
            success, say = self.work(goal, stopped)
        except Exception as e:  # noqa: BLE001 - said, never raised: nobody is waiting on this thread
            success, say = False, "I couldn't work the TV just now."
            print(f"(tv: {goal!r} failed: {e!r})")
        if stopped.is_set():
            print(f"(tv: {goal!r} stopped)")
            return
        print(f"(tv: {goal!r} {'done' if success else 'not done'}: {say})")
        self._announce(say)

    def work(self, goal: str, stopped: threading.Event | None = None) -> tuple[bool, str]:
        """(success, what to say): the goal worked toward step by step, until done, stopped, or out of steps or time."""
        stopped = stopped or threading.Event()
        self.log = [f"goal: {goal}"]
        if not self.box.awake():
            self.box.wake()
            self._sleep(3.0)
        items: list = [{"role": "user", "content": f"The screen now:\n{self._look()}"}]
        deadline = time.monotonic() + self._timeout_s
        for _ in range(self._steps):
            if stopped.is_set():
                return False, ""
            if time.monotonic() > deadline:
                break
            response = self._client.responses.create(
                model=self.model,
                instructions="\n\n".join(r for r in (AGENT_RULES.format(goal=goal), self._extra_rules) if r),
                input=items,
                tools=AGENT_TOOLS,
                tool_choice="required",
                store=False,
                **self._extra,
            )
            calls = [i for i in response.output if getattr(i, "type", "") == "function_call"]
            if not calls:
                said = getattr(response, "output_text", "") or ""
                return False, said.strip() or "I couldn't finish that on the TV."
            for call in calls:
                if stopped.is_set():
                    return False, ""
                args = json.loads(call.arguments or "{}")
                if call.name == "done":
                    self.log.append(f"done: {args}")
                    return bool(args.get("success")), str(args.get("say") or "").strip() or "Done."
                output, image = self._step(call.name, args, stopped)
                self.log.append(f"{call.name} {args}: {output[:200]}")
                items += [
                    {"type": "function_call", "call_id": call.call_id, "name": call.name, "arguments": call.arguments},
                    {"type": "function_call_output", "call_id": call.call_id, "output": output},
                ]
                if image:
                    picture = f"data:image/png;base64,{base64.b64encode(image).decode()}"
                    items = _without_pictures(items)
                    items.append(
                        {"role": "user", "content": [{"type": "input_image", "image_url": picture, "detail": "high"}]}
                    )
        return False, f"I ran out of time trying to {goal[0].lower()}{goal[1:]}."

    def _look(self) -> str:
        return screen(self.box)

    def _step(self, name: str, args: dict, stopped: threading.Event) -> tuple[str, bytes | None]:
        try:
            if name == "look":
                return self._look(), None
            if name == "screenshot":
                return "the screenshot is below", self.box.screenshot()
            if name == "press":
                if args.get("button") not in KEYS:
                    return f"error: buttons are {', '.join(KEYS)}", None
                self.box.press(args["button"], presses(args))
                self._sleep(0.6)
                return f"pressed; the screen now:\n{self._look()}", None
            if name == "type_text":
                self.box.type(str(args.get("text", "")))
                self._sleep(0.8)
                return f"typed; the screen now:\n{self._look()}", None
            if name == "open_app":
                package = self.box.find_app(str(args.get("app", "")))
                if not package:
                    return (
                        f"error: no app called {args.get('app')!r}; installed: {', '.join(self.box.packages())}",
                        None,
                    )
                opened, tries, front = open_app(self.box, package, self._sleep, stopped)
                if not opened:
                    return (
                        f"error: opened it {tries} times and it didn't stay open; {front or 'nothing'} is in front",
                        None,
                    )
                again = f" on try {tries} (it closed before)" if tries > 1 else ""
                return f"opened {package}{again}, and it stayed open; the screen now:\n{self._look()}", None
            if name == "wait":
                pause(max(0.5, min(10.0, float(args.get("seconds") or 1))), self._sleep, stopped)
                return "waited", None
        except HomeError as e:
            return f"error: {e}", None
        return f"error: no tool called {name}", None


TV_RULES = (
    "An Android TV box wakes and sleeps the TV with it (HDMI-CEC). To play a film or an episode, use play with the "
    'app and the title ("Ratatouille (2007)", "Wednesday S1E3"; the best known one unless they said which). For '
    'anything else on screen that takes more than one button ("find the news on YouTube"), use do with the goal in '
    "their words. Both work it out on screen in the background, checking each step, and TARS says when it's done: "
    "say only that you're on it, in a few words. For one button (pause, back) use press; to open an app, open."
)


class TvDevice(Device):
    """The home tool's Android TV: one button, an app, on and off, each checked on the box itself; and play and do,
    for the agent, which works the screen in the background."""

    actions = (STATUS, ON, OFF, PRESS, OPEN, PLAY, DO)

    def __init__(
        self,
        name: str,
        box: AndroidTV,
        agent: TvAgent | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        super().__init__(name)
        self.box, self.agent = box, agent
        self._clock, self._sleep = clock, sleep

    def what(self) -> str:
        how = "play a film or episode, do (anything that takes several steps on screen)" if self.agent else ""
        return f"an Android TV box, and the TV through it: status, on, off, press ({', '.join(KEYS)}), open an app" + (
            f", {how}" if how else "; no several-step tasks (they need OPENAI_API_KEY)"
        )

    def rules(self) -> str:
        return TV_RULES

    def slow(self, action: str, args: dict) -> bool:
        return action in (ON, OFF, OPEN)

    def status(self) -> str:
        if not self.box.awake():
            return f"{self.name}: asleep (the TV is off, or showing something else)"
        return f"{self.name}: on, in {self.box.app() or 'nothing'}; {self.box.playing()}"

    def check(self) -> list[Check]:
        try:
            state = "awake" if self.box.awake() else "asleep"
        except HomeError as e:
            return [Check("fail", str(e), "is ADB debugging on, and the address right? Then pair it again")]
        finally:
            self.box.close()
        if self.agent is None:
            return [Check("warn", f"answers ({state})", 'tasks like "play Toy Story" need OPENAI_API_KEY')]
        return [Check("ok", f"answers ({state}); it does tasks with {self.agent.model}")]

    def stop(self) -> None:
        if self.agent:
            self.agent.stop()

    def call(self, action: str, args: dict, request: Request) -> str:
        self.stop()  # a person asking for something else takes over from the agent
        if action in (ON, OFF):
            want = action == ON
            (self.box.wake if want else self.box.sleep)()
            if self._until(lambda: self.box.awake() == want):
                tv = "the TV should come on with it" if want else "the TV should go off with it"
                return f"done: the {self.name} is {'awake' if want else 'asleep'} (it says so itself); {tv}"
            return f"not confirmed: asked the {self.name} to {'wake' if want else 'sleep'}, but it says otherwise"
        if action == PRESS:
            key = button(args)
            if key not in KEYS:
                return f"error: the {self.name}'s buttons are {', '.join(KEYS)}"
            times = presses(args)
            self.box.press(key, times)
            return f"done: pressed {key}{f' {times} times' if times > 1 else ''}; {self.box.playing()}"
        if action == OPEN:
            name = str(args.get("app") or "").strip()
            package = self.box.find_app(name) if name else None
            if not package:
                return f"error: there's no app called {name!r} on the {self.name}"
            opened, tries, front = open_app(self.box, package, self._sleep)
            if opened:
                again = f", on try {tries}: it closed or never came up before" if tries > 1 else ""
                return f"done: {name} is open and stayed open (the {self.name} says so){again}"
            return f"not confirmed: opened {name} {tries} times, and {front or 'nothing'} is in front; it may be broken"
        if action == PLAY:
            return self.play(str(args.get("title") or "").strip(), str(args.get("app") or "").strip())
        return self._start(str(args.get("goal") or "").strip())

    def play(self, title: str, app: str) -> str:
        """A film or episode, by the agent; a plugin may go straight to it instead (examples/my_house)."""
        if not title:
            return 'error: play needs the title, as "Ratatouille (2007)" or "Wednesday S1E3"'
        return self._start(f"play {title}" + (f" on {app}" if app else ""))

    def _start(self, goal: str) -> str:
        if not goal:
            return "error: do needs the goal, in their words"
        if self.agent is None:
            return "error: working the TV step by step needs OPENAI_API_KEY; press buttons or open an app instead"
        self.agent.start(goal)
        return "started: working on it on screen; TARS will say when it's done, so say only that you're on it"

    def _until(self, done: Callable[[], bool]) -> bool:
        deadline = self._clock() + SETTLE_S
        while True:
            if done():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(POLL_S)


def make_tv_agent(cfg, env: Path, announce: Callable[[str], None], box: AndroidTV, **settings) -> TvAgent | None:
    """The agent that works `box` step by step, if .env has an OpenAI key; None otherwise. `settings`: TvAgent's own,
    as a plugin's box needs them (extra_rules)."""
    from dotenv import dotenv_values

    from .config import OPENAI_KEY

    key = dotenv_values(env).get(OPENAI_KEY) if env.exists() else None
    if not key:
        return None
    from openai import OpenAI

    # Each step reads a screenshot, so a step can take longer than a spoken reply may.
    client = OpenAI(api_key=key, timeout=STEP_TIMEOUT_S, max_retries=1)
    model, tier = cfg.home.tv_model or cfg.llm.model, cfg.llm.service_tier
    return TvAgent(box, client, model, announce, service_tier=tier, **settings)


def make(name: str, spec: dict, context: Context) -> TvDevice:
    """A [[home.devices]] entry with kind = "androidtv": its box at `address`, and the agent if there's one to make."""
    try:
        import adb_shell  # noqa: F401 - only whether it's there
    except ImportError:
        raise ValueError('it needs the optional extra "tv": uv sync --all-extras') from None
    box = AndroidTV(spec["address"], context.root / KEY_FILE, seen=Seen(context.root / FOUND_FILE))
    agent = context.tv_agent(box) if context.tv_agent else None
    return TvDevice(name, box, agent)


def main() -> None:
    """python -m voice_assistant.androidtv pair | look | screenshot FILE | do "GOAL" [--device NAME]: setting up and
    trying an Android TV box. do runs the core agent, without any plugin's rules."""
    import argparse

    from .config import OPENAI_KEY, load_config

    parser = argparse.ArgumentParser(prog="python -m voice_assistant.androidtv", description=main.__doc__)
    parser.add_argument("command", choices=["pair", "look", "screenshot", "do"])
    parser.add_argument("argument", nargs="?", help="screenshot: the file to save it in; do: the goal")
    parser.add_argument("--device", help="its name in config.toml, if there's more than one with an address")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()
    root = args.config.resolve().parent
    cfg = load_config(args.config)
    boxes = [d for d in cfg.home.devices if "address" in d and (not args.device or d["name"] == args.device)]
    if len(boxes) != 1:
        found = ", ".join(d["name"] for d in boxes) or "none"
        raise SystemExit(f"config.toml needs one TV box with an address (found: {found}); pick one with --device")
    print('Connecting; if the TV asks "Allow USB debugging?", tick Always allow and choose OK.', flush=True)
    box = AndroidTV(boxes[0]["address"], root / KEY_FILE, auth_timeout_s=60.0, seen=Seen(root / FOUND_FILE))
    try:
        if args.command == "pair":
            print(f"Connected: the box is {'awake' if box.awake() else 'asleep'}; in front: {box.app() or '-'}")
        elif args.command == "look":
            print(screen(box))
        elif args.command == "screenshot":
            path = Path(args.argument or "screen.png")
            path.write_bytes(box.screenshot())
            print(f"Saved {path}")
        else:
            agent = make_tv_agent(cfg, root / ".env", lambda line: print(f"TARS: {line}"), box)
            if agent is None:
                raise SystemExit(f"do needs {OPENAI_KEY} in .env")
            success, say = agent.work(args.argument or "")
            print("\n".join(agent.log))
            print(f"TARS: {say}  ({'done' if success else 'not done'})")
    except HomeError as e:
        raise SystemExit(str(e)) from None
    finally:
        box.close()


if __name__ == "__main__":
    main()
