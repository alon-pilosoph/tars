"""An Android TV box over ADB: what it says is read, its changes are checked on the box itself, and the agent works
it step by step, checking as it goes, then says how it went, after a chime."""

import json
import shlex
import threading
import time
import types

import pytest

import voice_assistant.assistant as assistant_module
from voice_assistant.androidtv import AndroidTV, TvAgent, TvDevice, screen
from voice_assistant.assistant import Announcements
from voice_assistant.home import HomeError, Request

from .conftest import make_assistant, speech

# The box.

MEDIA = """MEDIA SESSION SERVICE (dumpsys media_session)
  Sessions Stack - have 2 sessions:
    com.amazon.tv.launcher/launcher (userId=0)
      package=com.amazon.tv.launcher
      active=false
      state=PlaybackState {state=1, position=0}
    Stremio/Stremio (userId=0)
      package=com.stremio.one
      active=true
      state=PlaybackState {state=3, position=81000, speed=1.0}
      metadata: size=4, description=Toy Story, 1995, null
"""
UI = """UI hierchary dumped to: /sdcard/tars_ui.xml
<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node text="" content-desc="" focused="false" bounds="[0,0][1920,1080]">
<node text="Search" focused="false" bounds="[10,10][200,60]" />
<node text="Toy Story" content-desc="" focused="true" bounds="[100,300][400,700]" />
<node text="" content-desc="Toy Story 4" focused="false" bounds="[420,300][720,700]" />
</node></hierarchy>"""


class Box(AndroidTV):
    """An Android TV box whose shell answers from a script, and remembers what it was asked."""

    def __init__(self, awake=True, front="com.amazon.tv.launcher", media="", ui=""):
        super().__init__("10.0.0.8", key_path=None)
        self.state = {"awake": awake, "front": front, "media": media, "ui": ui}
        self.commands: list[str] = []

    def _run(self, command, decode=True, timeout_s=10.0, repeatable=False):
        self.commands.append(command)
        s = self.state
        if command.startswith("dumpsys power"):
            return f"  mWakefulness={'Awake' if s['awake'] else 'Asleep'}"
        if command.startswith("dumpsys window windows"):  # as Fire OS 8 (Android 11) answers it
            return ""
        if command.startswith("dumpsys window"):
            return f"  mCurrentFocus=Window{{7e2 u0 {s['front']}/{s['front']}.MainActivity}}" if s["front"] else ""
        if command.startswith("dumpsys media_session"):
            return s["media"]
        if command.startswith("uiautomator"):
            return s["ui"]
        if command == "pm list packages":
            apps = ("com.amazon.tv.launcher", "com.stremio.one", "org.videolan.vlc")
            return "".join(f"package:{a}\n" for a in apps)
        if command == "screencap -p":
            return b"\x89PNG fake"
        if "KEYCODE_WAKEUP" in command:
            s["awake"] = True
        elif "KEYCODE_SLEEP" in command:
            s["awake"] = False
        elif command.startswith("monkey -p "):
            s["front"] = command.split()[2]
        elif command.startswith("am start "):
            s["front"] = command.split()[-1].split("/")[0]
        elif command == "input keyevent KEYCODE_DPAD_CENTER" and s["front"] == "com.stremio.one":
            s["media"] = f"  package={s['front']}\n  active=true\n  state=PlaybackState {{state=3}}\n"
        return ""


def test_what_the_stick_says_is_read():
    box = Box(front="com.stremio.one", media=MEDIA, ui=UI)
    assert box.awake() and box.app() == "com.stremio.one"
    assert box.media()[0] == ("com.stremio.one", "playing", "Toy Story")
    assert box.playing() == "playing: Toy Story (com.stremio.one)"
    assert box.ui().splitlines() == [
        "Search @[10,10][200,60]",
        "[focused] Toy Story @[100,300][400,700]",
        "Toy Story 4 @[420,300][720,700]",
    ]
    assert "app: com.stremio.one\nplaying: Toy Story" in screen(box)


def test_typing_goes_as_one_word_with_quotes_kept():
    box = Box()
    box.type("it's toy story")
    clear, typed = box.commands[-1].split("; ")
    assert clear.startswith("input keyevent KEYCODE_MOVE_END KEYCODE_DEL")  # what an earlier search left is cleared
    assert shlex.split(typed) == ["input", "text", "it's%stoy%sstory"]


def test_a_link_and_its_extras_reach_the_box_as_they_are_never_as_commands():
    box = Box()
    box.open_link("stremio:///x'; reboot; '", "com.stremio.one", extras={"title": "Toy Story; reboot"})
    words = shlex.split(box.commands[-1])
    assert words[words.index("-d") + 1] == "stremio:///x'; reboot; '"
    assert words[-4:] == ["-e", "title", "Toy Story; reboot", "com.stremio.one"]


class Flaky(AndroidTV):
    """A box whose connection fails with each of `errors` in turn (None: it answers): what was run is kept, and the
    clock moves only when told."""

    def __init__(self, *errors: Exception | None):
        super().__init__("10.0.0.8", key_path=None, clock=lambda: self.now)
        self.errors, self.ran, self.now = list(errors), [], 0.0

    def _device(self):
        self._adb = self._adb or types.SimpleNamespace(shell=self._shell, close=lambda: None)
        return self._adb

    def _shell(self, command, **kw):
        self.ran.append(command)
        if self.errors and (error := self.errors.pop(0)):
            raise error
        return "  mWakefulness=Awake"


def test_a_press_that_may_have_reached_the_box_is_never_sent_twice():
    box = Flaky(TimeoutError("read timed out"))
    with pytest.raises(HomeError, match="may have done it"):
        box.press("select", 2)
    assert box.ran == ["input keyevent KEYCODE_DPAD_CENTER KEYCODE_DPAD_CENTER"]


def test_a_read_waking_the_box_or_a_press_on_a_connection_already_gone_is_tried_again():
    assert Flaky(TimeoutError("read timed out")).awake()
    waking = Flaky(TimeoutError("read timed out"))
    waking.wake()
    assert len(waking.ran) == 2
    pressing = Flaky(ConnectionResetError("reset by peer"))
    pressing.press("select")
    assert len(pressing.ran) == 2


def test_a_connection_left_idle_is_checked_before_a_press_so_a_dead_one_costs_nothing():
    # The box slept, or moved, since the last command: its old connection only times out.
    box = Flaky()
    box.awake()
    box.now, box.errors = 60.0, [TimeoutError("read timed out")]
    box.press("select")
    assert box.ran[1:] == ["echo", "echo", "input keyevent KEYCODE_DPAD_CENTER"]


def test_apps_are_found_by_name_or_among_whats_installed():
    box = Box()
    assert box.find_app("Stremio") == "com.stremio.one"
    assert box.find_app("VLC") == "org.videolan.vlc"
    assert box.find_app("hbo") is None


def tv(box: Box, agent=None) -> TvDevice:
    clock = [0.0]

    def sleep(s):
        clock[0] += s

    return TvDevice("tv", box, agent, clock=lambda: clock[0], sleep=sleep)


def test_on_wakes_the_stick_and_waits_for_it_to_say_so():
    box = Box(awake=False)
    assert tv(box).call("on", {}, Request()).startswith("done: the tv is awake")
    assert "input keyevent KEYCODE_WAKEUP" in box.commands


def test_an_app_is_open_only_when_its_in_front_and_stays_there():
    box = Box()
    said = tv(box).call("open", {"app": "stremio"}, Request())
    assert said == "done: stremio is open and stayed open (the tv says so)"
    stuck = Box()
    stuck.open = lambda package: None  # it never comes to the front
    assert tv(stuck).call("open", {"app": "stremio"}, Request()).startswith("not confirmed: opened stremio 3 times")
    assert tv(Box()).call("open", {"app": "hbo"}, Request()).startswith("error: there's no app called 'hbo'")


class CrashesOnce(Box):
    """Stremio after the box boots: the first time, it comes up, then closes a moment later."""

    def __init__(self):
        super().__init__()
        self.opens, self.reads = 0, 0

    def app(self):
        front = super().app()
        if front == "com.stremio.one" and self.opens == 1:
            self.reads += 1
            if self.reads > 2:
                self.state["front"] = "com.amazon.tv.launcher"
        return super().app()

    def open(self, package):
        self.opens += 1
        super().open(package)


def test_an_app_that_crashes_right_after_opening_is_opened_again():
    box = CrashesOnce()
    said = tv(box).call("open", {"app": "stremio"}, Request())
    assert said.startswith("done: stremio is open and stayed open") and "on try 2" in said
    assert box.opens == 2 and "am force-stop com.stremio.one" in box.commands


def test_the_agent_opens_apps_the_same_way():
    box = CrashesOnce()
    client = calls(("open_app", {"app": "Stremio"}), ("done", {"success": True, "say": "Stremio is open."}))
    agent = TvAgent(box, client, "gpt", lambda line: None, sleep=lambda s: None)
    assert agent.work("open stremio") == (True, "Stremio is open.")
    assert box.opens == 2 and "on try 2" in agent.log[1]


def test_a_button_press_says_what_is_playing_after_it():
    box = Box(media=MEDIA)
    said = tv(box).call("press", {"button": "play_pause"}, Request())
    assert said == "done: pressed play_pause; playing: Toy Story (com.stremio.one)"
    assert tv(box).call("press", {"button": "eject"}, Request()).startswith("error: the tv's buttons are")


def test_do_hands_the_goal_to_the_agent_and_answers_at_once():
    agent = types.SimpleNamespace(goals=[], stop=lambda: None)
    agent.start = agent.goals.append
    said = tv(Box(), agent).call("do", {"goal": "play Toy Story on Stremio"}, Request("alon"))
    assert said.startswith("started") and agent.goals == ["play Toy Story on Stremio"]


def test_without_openai_do_says_what_else_it_can_do():
    assert "needs OPENAI_API_KEY" in tv(Box()).call("do", {"goal": "play Toy Story"}, Request())


def test_a_stick_that_doesnt_answer_is_an_error_not_a_crash():
    box = Box()

    def down(*args, **kwargs):
        raise HomeError("the TV box didn't answer at 10.0.0.8")

    box._run = down
    with pytest.raises(HomeError):
        tv(box).status()


# The agent.


def calls(*steps):
    """An OpenAI client whose every request answers with the next step's tool call: (name, arguments)."""
    requests, script = [], iter(steps)

    def create(**kw):
        requests.append(kw)
        name, args = next(script)
        call = types.SimpleNamespace(
            type="function_call", name=name, arguments=json.dumps(args), call_id=f"c{len(requests)}"
        )
        return types.SimpleNamespace(output=[call], output_text="")

    client = types.SimpleNamespace(responses=types.SimpleNamespace(create=create))
    client.requests = requests
    return client


def test_the_agent_works_step_by_step_and_finishes_with_what_to_say():
    box = Box(awake=False)
    client = calls(
        ("open_app", {"app": "Stremio"}),
        ("look", {}),
        ("type_text", {"text": "toy story"}),
        ("screenshot", {}),
        ("press", {"button": "select", "times": 1}),
        ("done", {"success": True, "say": "Toy Story is playing."}),
    )
    agent = TvAgent(box, client, "gpt", lambda line: None, sleep=lambda s: None)
    assert agent.work("play Toy Story on Stremio") == (True, "Toy Story is playing.")
    assert "input keyevent KEYCODE_WAKEUP" in box.commands  # it woke the TV first
    assert box.state["front"] == "com.stremio.one"
    assert any(c.endswith("input text toy%sstory") for c in box.commands)
    assert client.requests[-1]["reasoning"] == {"effort": "low"}
    pressed = next(i["output"] for i in client.requests[-1]["input"] if str(i.get("output", "")).startswith("pressed"))
    assert "the screen now:\napp: " in pressed  # no extra round trip to look
    last = client.requests[-1]["input"]
    assert any(isinstance(i.get("content"), list) and i["content"][0]["type"] == "input_image" for i in last)
    assert "play Toy Story on Stremio" in client.requests[0]["instructions"]


def test_the_agent_gives_up_when_its_out_of_steps():
    client = calls(*[("look", {})] * 5)
    agent = TvAgent(Box(), client, "gpt", lambda line: None, steps=3, sleep=lambda s: None)
    success, said = agent.work("Play the news")
    assert not success and said == "I ran out of time trying to play the news."


def test_the_agent_says_how_it_went():
    said = []
    client = calls(("done", {"success": False, "say": "Stremio found no streams for Toy Story."}))
    agent = TvAgent(Box(), client, "gpt", said.append, sleep=lambda s: None)
    agent.start("play Toy Story on Stremio", wait=True)
    assert said == ["Stremio found no streams for Toy Story."]


def test_stopping_the_agent_ends_the_step_its_on_before_anything_else_is_done():
    # Someone pressing a button while the agent waits for an app to open: the agent mustn't force-stop that app, or
    # anything else, after they've taken over.
    box, opening, said = Box(), threading.Event(), []
    box.open = lambda package: opening.set()  # it never comes to the front
    client = calls(("open_app", {"app": "Stremio"}), ("done", {"success": True, "say": "Stremio is open."}))
    agent = TvAgent(box, client, "gpt", said.append, sleep=lambda s: time.sleep(0.01))
    agent.start("open stremio")
    assert opening.wait(5)
    tv(box, agent).call("press", {"button": "home"}, Request())
    assert not agent._thread.is_alive() and said == []
    assert box.commands[-2:] == ["input keyevent KEYCODE_HOME", "dumpsys media_session"]  # the last, nothing after
    assert not any("force-stop" in c for c in box.commands)


def test_the_agent_carries_a_households_own_rules_and_none_of_its_own():
    client = calls(("done", {"success": True, "say": "Done."}))
    agent = TvAgent(Box(), client, "gpt", lambda line: None, sleep=lambda s: None, extra_rules="Stremio: the first.")
    agent.work("play Toy Story")
    assert client.requests[0]["instructions"].endswith("\n\nStremio: the first.")


def test_a_stopped_agent_says_nothing():
    said, started = [], threading.Event()

    def create(**kw):
        started.set()
        agent.stop()
        return types.SimpleNamespace(output=[], output_text="")

    client = types.SimpleNamespace(responses=types.SimpleNamespace(create=create))
    agent = TvAgent(Box(), client, "gpt", said.append, sleep=lambda s: None)
    agent.start("play Toy Story", wait=True)
    assert started.is_set() and said == []


# Saying it.


def test_what_tars_announces_is_said_after_a_chime_and_joins_the_conversation(speaker):
    assistant, brain = make_assistant(speaker, [None], [])
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    assistant.voice.stream = lambda text: iter([text.encode()])
    assistant.announcements = Announcements()
    assistant.announcements.add("Toy Story is playing.")
    assert assistant._due()
    assistant.say_announcements(follow_up_s=4.0)
    assert played[-1] == b"Toy Story is playing." and brain.remembered == ["Toy Story is playing."]
    assert not assistant._due()


def test_what_the_voice_couldnt_say_waits_and_is_said_once_it_can(speaker):
    now = [1000.0]
    assistant, brain = make_assistant(speaker, [], [])
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    down = [True]

    def voice(text):
        if down[0]:
            raise ConnectionError("no voice")
        return iter([text.encode()])

    assistant.voice.stream = voice
    assistant.announcements = Announcements(clock=lambda: now[0])
    assistant.announcements.add("Toy Story is playing.")
    assistant.say_announcements()
    assert b"Toy Story is playing." not in played and brain.remembered == []
    assert not assistant._due()  # not tried again at once: the wake loop would do nothing else
    down[0] = False
    now[0] += assistant_module.VOICE_RETRY_S
    assert assistant._due()
    assistant.say_announcements()
    assert played[-1] == b"Toy Story is playing." and brain.remembered == ["Toy Story is playing."]


def test_a_reply_to_an_announcement_is_heard_without_the_wake_word(speaker):
    assistant, brain = make_assistant(speaker, [speech(), None], ["the first one"], replies=["Playing Toy Story."])
    assistant.announcements = Announcements()
    assistant.announcements.add("Toy Story, or Toy Story 4?")
    assistant.say_announcements(follow_up_s=4.0)
    assert brain.asked == ["[Follow-up, no wake word] the first one"]


def test_play_hands_the_title_to_the_agent():
    agent = types.SimpleNamespace(goals=[], stop=lambda: None)
    agent.start = agent.goals.append
    said = tv(Box(), agent).call("play", {"title": "Toy Story (1995)", "app": "Stremio"}, Request("alon"))
    assert said.startswith("started") and agent.goals == ["play Toy Story (1995) on Stremio"]
    assert tv(Box(), agent).call("play", {"title": ""}, Request()).startswith("error: play needs the title")


def test_asking_for_anything_else_stops_the_agent():
    stopped = []
    agent = types.SimpleNamespace(start=lambda goal: None, stop=lambda: stopped.append(True))
    tv(Box(media=MEDIA), agent).call("press", {"button": "pause"}, Request())
    assert stopped == [True]


def test_check_says_whether_the_box_answers_and_whether_it_can_do_tasks():
    (no_agent,) = tv(Box()).check()
    assert no_agent.status == "warn" and "OPENAI_API_KEY" in no_agent.fix
    agent = types.SimpleNamespace(model="gpt-5.5", stop=lambda: None)
    (ready,) = tv(Box(awake=False), agent).check()
    assert ready.status == "ok" and ready.detail == "answers (asleep); it does tasks with gpt-5.5"
    down = Box()
    down._run = lambda *a, **kw: (_ for _ in ()).throw(HomeError("the TV box didn't answer"))
    (failed,) = tv(down).check()
    assert failed.status == "fail"


def test_the_box_is_slow_to_wake_and_open_but_not_to_press():
    device = tv(Box())
    assert device.slow("on", {}) and device.slow("open", {}) and not device.slow("press", {})
