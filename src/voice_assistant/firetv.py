"""The Fire TV stick, over ADB on the home network: TARS presses its buttons, opens apps, reads what's on screen
(the app in front, what's playing, the text the app shows) and takes screenshots. Waking the stick wakes the TV
(HDMI-CEC), so "turn the TV on" goes through here.

Anything that takes several steps ("play Toy Story on Stremio") goes to TvAgent: a model that works the TV the way a
person would, looking after every step and checking the result, in the background. TARS answers "On it." at once and
says how it went when it's done, so the conversation never waits on a search screen.

ADB has to be on (Settings > My Fire TV > Developer Options > ADB debugging), and the first connection asked for on
the TV ("Allow USB debugging?", with "Always allow" ticked): python -m voice_assistant.firetv pair."""

import base64
import contextlib
import json
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

from .home import DO, OFF, ON, OPEN, PRESS, STATUS, HomeError, Request

# The remote's buttons, as Android's key codes.
KEYS = {
    "up": "KEYCODE_DPAD_UP",
    "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
    "select": "KEYCODE_DPAD_CENTER",
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
# Apps by what people call them; anything else is found among the packages installed.
APPS = {
    "stremio": "com.stremio.one",
    "netflix": "com.netflix.ninja",
    "youtube": "com.amazon.firetv.youtube",
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
SETTLE_S = 4.0  # how long waking or sleeping gets to show on the stick
# Opening an app: it gets OPEN_WAIT_S to come to the front, and must still be there STAY_S later, since some (Stremio)
# now and then crash a moment after starting, the first time after the stick boots. Then it's opened again, at most
# OPEN_TRIES times.
OPEN_WAIT_S, STAY_S, OPEN_TRIES = 8.0, 3.0, 3
POLL_S = 0.5
MAX_PRESSES = 20
AGENT_STEPS = 30
AGENT_TIMEOUT_S = 150.0
UI_DUMP = "/sdcard/tars_ui.xml"
KEY_FILE = "voice_data/firetv/adbkey"
CONNECT_TIMEOUT_S = 3.0  # TARS's ADB key: the stick asks once whether to trust it


class FireTV:
    """One ADB connection, made when first needed and again after it drops. Every call is short, and a stick that
    doesn't answer is a HomeError."""

    def __init__(self, address: str, key_path: Path, auth_timeout_s: float = 3.0):
        host, _, port = address.partition(":")
        self._host, self._port = host, int(port or 5555)
        self._key_path, self._auth_timeout_s = key_path, auth_timeout_s
        self._adb = None
        self._lock = threading.RLock()

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
        # Short: a stick that's unplugged or off the Wi-Fi is said to be, rather than leaving a voice turn waiting.
        adb = AdbDeviceTcp(self._host, self._port, default_transport_timeout_s=CONNECT_TIMEOUT_S)
        try:
            adb.connect(rsa_keys=[signer], auth_timeout_s=self._auth_timeout_s)
        except Exception as e:
            raise HomeError(f"the Fire TV didn't answer at {self._host} ({type(e).__name__}: {e})") from e
        self._adb = adb
        return adb

    def _run(self, command: str, decode: bool = True, timeout_s: float = 10.0):
        with self._lock:
            for attempt in (1, 2):
                try:
                    return self._device().shell(command, read_timeout_s=timeout_s, timeout_s=timeout_s, decode=decode)
                except HomeError:
                    raise
                except Exception as e:
                    self.close()
                    if attempt == 2:
                        raise HomeError(f"the Fire TV stopped answering ({type(e).__name__})") from e
        raise AssertionError("unreachable")

    def shell(self, command: str, timeout_s: float = 10.0) -> str:
        return self._run(command, timeout_s=timeout_s) or ""

    def close(self) -> None:
        with self._lock:
            if self._adb is not None:
                with contextlib.suppress(Exception):  # closing one that's gone is as good as closing it
                    self._adb.close()
            self._adb = None

    # What it's doing.

    def awake(self) -> bool:
        return "mWakefulness=Awake" in self.shell("dumpsys power | grep mWakefulness=")

    def app(self) -> str:
        """The package in front ("com.stremio.one"), or ""."""
        out = self.shell("dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp'")
        found = re.search(r"\s([\w.]+)/[\w.$]+", out)
        return found.group(1) if found else ""

    def media(self) -> list[tuple[str, str, str]]:
        """What the media sessions say: (package, playing/paused/..., title), the active ones first."""
        out = self.shell("dumpsys media_session")
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
        out = self.shell(f"uiautomator dump {UI_DUMP} >/dev/null 2>&1; cat {UI_DUMP}; rm -f {UI_DUMP}", timeout_s=15)
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
        png = self._run("screencap -p", decode=False, timeout_s=15)
        if not png or not png.startswith(b"\x89PNG"):
            raise HomeError("the Fire TV didn't give a screenshot")
        return png

    def packages(self) -> list[str]:
        out = self.shell("pm list packages")
        return sorted(line.removeprefix("package:").strip() for line in out.splitlines() if line.startswith("package:"))

    # Doing things.

    def wake(self) -> None:
        self.shell("input keyevent KEYCODE_WAKEUP")

    def sleep(self) -> None:
        self.shell("input keyevent KEYCODE_SLEEP")

    def press(self, key: str, times: int = 1) -> None:
        code = KEYS[key]
        self.shell("; ".join([f"input keyevent {code}"] * times))

    def type(self, text: str) -> None:
        """Types into whatever has focus. `input text` takes no spaces: they go as %s."""
        quoted = text.replace("'", "'\\''").replace(" ", "%s")
        self.shell(f"input text '{quoted}'")

    def open(self, package: str) -> None:
        self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

    def force_stop(self, package: str) -> None:
        self.shell(f"am force-stop {package}")

    def find_app(self, name: str) -> str | None:
        """The package for an app's name, among those installed: the known one, the package itself, or the shortest
        package that mentions it (an app built for Fire TV may be packaged differently)."""
        wanted = name.strip().lower()
        installed = self.packages()
        if APPS.get(wanted) in installed:
            return APPS[wanted]
        if wanted in installed:
            return wanted
        squeezed = re.sub(r"[^a-z0-9]", "", wanted)
        matches = [p for p in installed if squeezed and squeezed in p.replace(".", "")]
        return min(matches, key=len) if matches else None


class TV:
    """The home tool's Fire TV: one button, an app, on and off, checked on the stick; and `do`, for the agent."""

    def __init__(
        self,
        stick: FireTV,
        agent: TvAgent | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.stick, self.agent = stick, agent
        self._clock, self._sleep = clock, sleep

    def what(self) -> str:
        how = "do (anything that takes several steps on screen)" if self.agent else "no several-step tasks"
        return (
            f"the Fire TV stick, and the TV through it: status, on, off, press ({', '.join(KEYS)}), open an app, {how}"
        )

    def status(self) -> str:
        if not self.stick.awake():
            return "fire tv: asleep (the TV is off, or showing something else)"
        app = self.stick.app()
        return f"fire tv: on, in {app or 'nothing'}; {self.stick.playing()}"

    def call(self, action: str, args: dict, request: Request) -> str:
        if self.agent and action != STATUS:
            self.agent.stop()  # a person asking for something else takes over from the agent
        if action == STATUS:
            return self.status()
        if action in (ON, OFF):
            want = action == ON
            (self.stick.wake if want else self.stick.sleep)()
            if self._until(lambda: self.stick.awake() == want):
                tv = "the TV should come on with it" if want else "the TV should go off with it"
                return f"done: the fire tv is {'awake' if want else 'asleep'} (it says so itself); {tv}"
            return f"not confirmed: asked the fire tv to {'wake' if want else 'sleep'}, but it still says otherwise"
        if action == PRESS:
            key = str(args.get("button") or "").strip().lower()
            if key not in KEYS:
                return f"error: the fire tv's buttons are {', '.join(KEYS)}"
            times = _times(args)
            self.stick.press(key, times)
            return f"pressed {key}{f' {times} times' if times > 1 else ''}; {self.stick.playing()}"
        if action == OPEN:
            name = str(args.get("app") or "").strip()
            package = self.stick.find_app(name) if name else None
            if not package:
                return f"error: there's no app called {name!r} on the fire tv"
            opened, tries, front = open_app(self.stick, package, self._sleep)
            if opened:
                again = f", on try {tries}: it closed or never came up before" if tries > 1 else ""
                return f"done: {name} is open and stayed open (the fire tv says so){again}"
            return f"not confirmed: opened {name} {tries} times, and {front or 'nothing'} is in front; it may be broken"
        if action == DO:
            goal = str(args.get("goal") or "").strip()
            if not goal:
                return "error: do needs the goal, in their words"
            if self.agent is None:
                return "error: working the TV step by step needs OPENAI_API_KEY; press buttons or open an app instead"
            self.agent.start(goal)
            return "started: working on it on screen; TARS will say when it's done, so say only that you're on it"
        return f"error: the fire tv can't {action}"

    def _until(self, done: Callable[[], bool]) -> bool:
        deadline = self._clock() + SETTLE_S
        while True:
            if done():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(POLL_S)


def open_app(stick: FireTV, package: str, sleep: Callable[[float], None] = time.sleep) -> tuple[bool, int, str]:
    """Opens `package` and checks it stayed open: (it did, the tries it took, the package in front at the end). An app
    that never came up, or crashed a moment after, is stopped and opened again, up to OPEN_TRIES times."""
    front = ""
    for attempt in range(1, OPEN_TRIES + 1):
        if not stick.awake():
            stick.wake()
            sleep(2.0)
        stick.open(package)
        came = _watch(lambda: stick.app() == package, OPEN_WAIT_S, sleep)
        stayed = came and not _watch(lambda: stick.app() != package, STAY_S, sleep)
        front = stick.app()
        if stayed:
            return True, attempt, front
        print(f"(fire tv: {package} {'closed again' if came else 'never came up'}; try {attempt} of {OPEN_TRIES})")
        stick.force_stop(package)
        sleep(1.0)
    return False, OPEN_TRIES, front


def _watch(happened: Callable[[], bool], seconds: float, sleep: Callable[[float], None]) -> bool:
    """Whether `happened` comes true within `seconds`, looked at every POLL_S."""
    for _ in range(int(seconds / POLL_S) + 1):
        if happened():
            return True
        sleep(POLL_S)
    return False


def screen(stick: FireTV) -> str:
    """What's on screen, as text: the app in front, what's playing, and what the app shows."""
    if not stick.awake():
        return "asleep: the screen is off"
    app, playing, ui = stick.app(), stick.playing(), stick.ui()
    return f"app: {app or 'none'}\n{playing}\non screen:\n{ui or '(no text: take a screenshot)'}"


def _times(args: dict) -> int:
    try:
        return max(1, min(MAX_PRESSES, int(args.get("times") or 1)))
    except TypeError, ValueError:
        return 1


AGENT_RULES = """You work a Fire TV stick for someone in the house, the way a person with its remote would, to do what \
they asked: {goal}

You have its remote's buttons, typing into whatever has focus, opening apps, and two ways to see the screen: look (the \
app in front, what's playing, and the text the app shows, the focused element marked) and screenshot. Look after every \
step and check it did what you meant before the next; when look shows no text, take a screenshot. If the app closes \
or you're back on the Fire TV's home screen without meaning to be, open the app again. Move the focus with \
the arrows and choose with select; back goes back. Fire TV apps often take a few seconds to load: wait, then look again.

To play something: open the app, find it (its search, usually), choose the right title (the best known one, unless \
they said which), and start it. In Stremio, choosing a title lists streams from its add-ons: choose the first one that \
plays. It's playing only when look says playing, or a screenshot shows the video itself.

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
    {"type": "function", "name": "type_text", "description": "Type into the focused search box.", "strict": True,
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
        stick: FireTV,
        client,
        model: str,
        announce: Callable[[str], None],
        steps: int = AGENT_STEPS,
        timeout_s: float = AGENT_TIMEOUT_S,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.stick, self._client, self._model, self._announce = stick, client, model, announce
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
        self._stopped.set()

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
        if not self.stick.awake():
            self.stick.wake()
            self._sleep(3.0)
        items: list = [{"role": "user", "content": f"The screen now:\n{self._look()}"}]
        deadline = time.monotonic() + self._timeout_s
        for _ in range(self._steps):
            if stopped.is_set():
                return False, ""
            if time.monotonic() > deadline:
                break
            response = self._client.responses.create(
                model=self._model,
                instructions=AGENT_RULES.format(goal=goal),
                input=items,
                tools=AGENT_TOOLS,
                tool_choice="required",
                store=False,
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
                output, image = self._step(call.name, args)
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
        return screen(self.stick)

    def _step(self, name: str, args: dict) -> tuple[str, bytes | None]:
        try:
            if name == "look":
                return self._look(), None
            if name == "screenshot":
                return "the screenshot is below", self.stick.screenshot()
            if name == "press":
                if args.get("button") not in KEYS:
                    return f"error: buttons are {', '.join(KEYS)}", None
                self.stick.press(args["button"], _times(args))
                self._sleep(0.6)
                return "pressed; look to see what changed", None
            if name == "type_text":
                self.stick.type(str(args.get("text", "")))
                self._sleep(0.8)
                return "typed; look to check it went into the search box", None
            if name == "open_app":
                package = self.stick.find_app(str(args.get("app", "")))
                if not package:
                    return (
                        f"error: no app called {args.get('app')!r}; installed: {', '.join(self.stick.packages())}",
                        None,
                    )
                opened, tries, front = open_app(self.stick, package, self._sleep)
                if not opened:
                    return (
                        f"error: opened it {tries} times and it didn't stay open; {front or 'nothing'} is in front",
                        None,
                    )
                again = f" on try {tries} (it closed before)" if tries > 1 else ""
                return f"opened {package}{again}, and it stayed open; look to see where it is", None
            if name == "wait":
                self._sleep(max(0.5, min(10.0, float(args.get("seconds") or 1))))
                return "waited", None
        except HomeError as e:
            return f"error: {e}", None
        return f"error: no tool called {name}", None


def main() -> None:
    """python -m voice_assistant.firetv pair | look | screenshot FILE | do "GOAL": setting up and trying the stick."""
    import argparse

    from dotenv import dotenv_values

    from .config import OPENAI_KEY, load_config

    parser = argparse.ArgumentParser(prog="python -m voice_assistant.firetv", description=main.__doc__)
    parser.add_argument("command", choices=["pair", "look", "screenshot", "do"])
    parser.add_argument("argument", nargs="?", help="screenshot: the file to save it in; do: the goal")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()
    root = args.config.resolve().parent
    cfg = load_config(args.config)
    spec = next((d for d in cfg.home.devices if d.get("kind") == "firetv"), None)
    if spec is None:
        raise SystemExit('config.toml has no [[home.devices]] with kind = "firetv": see docs/home.md')
    print('Connecting; if the TV asks "Allow USB debugging?", tick Always allow and choose OK.', flush=True)
    stick = FireTV(spec["address"], root / KEY_FILE, auth_timeout_s=60.0)
    try:
        if args.command == "pair":
            print(f"Connected: the Fire TV is {'awake' if stick.awake() else 'asleep'}; in front: {stick.app() or '-'}")
        elif args.command == "look":
            print(screen(stick))
        elif args.command == "screenshot":
            path = Path(args.argument or "screen.png")
            path.write_bytes(stick.screenshot())
            print(f"Saved {path}")
        else:
            from openai import OpenAI

            key = dotenv_values(root / ".env").get(OPENAI_KEY)
            if not key:
                raise SystemExit(f"do needs {OPENAI_KEY} in .env")
            model = cfg.home.tv_model or cfg.llm.model
            agent = TvAgent(stick, OpenAI(api_key=key), model, lambda line, who: print(f"TARS: {line}"))
            success, say = agent.work(args.argument or "")
            print("\n".join(agent.log))
            print(f"TARS: {say}  ({'done' if success else 'not done'})")
    except HomeError as e:
        raise SystemExit(str(e)) from None
    finally:
        stick.close()


if __name__ == "__main__":
    main()
