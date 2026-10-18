"""The TV: the published IR codes (Samsung's TV, Polk's soundbar) made exactly, the Fire TV read and worked over ADB,
and the agent that works it step by step, checking as it goes, then says how it went."""

import json
import threading
import types

import pytest

from voice_assistant.ac_onboard import decode
from voice_assistant.assistant import Announcements
from voice_assistant.firetv import TV, FireTV, TvAgent, screen
from voice_assistant.home import HomeError, Request
from voice_assistant.ir import CODE_SETS, Remote, nec_pulses

from .conftest import make_assistant, speech

# The IR codes.


def bits(data: list[int]) -> str:
    """As the onboarding decoder reads a press: each byte lowest bit first."""
    return "".join(format(byte, "08b")[::-1] for byte in data)


def test_samsungs_codes_are_device_7_twice_then_the_button_and_its_inverse():
    d = decode(CODE_SETS["samsung-tv"].pulses("volume_up"))
    assert d and d.bits == bits([0x07, 0x07, 0x07, 0xF8]) and d.timing["header"] == [4500, 4500]


def test_polks_codes_are_its_address_then_the_button_and_its_inverse():
    d = decode(CODE_SETS["polk-re9641"].pulses("mute"))
    assert d and d.bits == bits([0xC8, 0x91, 0x20, 0xDF]) and d.timing["header"] == [9000, 4500]


def test_a_code_is_a_header_32_bits_and_a_final_mark():
    pulses = nec_pulses([0, 0xFF, 1, 2], (9000, 4500))
    assert len(pulses) == 2 + 64 + 1 and pulses[-1] == 560


def test_a_remote_presses_a_button_as_many_times_as_asked():
    sent = []
    remote = Remote("soundbar", CODE_SETS["polk-re9641"], sent.append, sleep=lambda s: None)
    said = remote.call("press", {"button": "volume up", "times": 4})
    assert said.startswith("sent: volume_up 4 times") and len(sent) == 4
    assert sent[0] == CODE_SETS["polk-re9641"].pulses("volume_up")


def test_a_remote_says_what_it_has_rather_than_guess():
    sent = []
    soundbar = Remote("soundbar", CODE_SETS["polk-re9641"], sent.append)
    assert soundbar.call("press", {"button": "netflix"}).startswith("error: the soundbar's buttons are")
    assert "no off of its own" in soundbar.call("off", {})
    tv = Remote("tv", CODE_SETS["samsung-tv"], sent.append)
    assert tv.call("off", {}).startswith("sent: off") and sent == [CODE_SETS["samsung-tv"].pulses("off")]


# The Fire TV.

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


class Stick(FireTV):
    """A Fire TV whose shell answers from a script, and remembers what it was asked."""

    def __init__(self, awake=True, front="com.amazon.tv.launcher", media="", ui=""):
        super().__init__("10.0.0.8", key_path=None)
        self.state = {"awake": awake, "front": front, "media": media, "ui": ui}
        self.commands: list[str] = []

    def _run(self, command, decode=True, timeout_s=10.0):
        self.commands.append(command)
        s = self.state
        if command.startswith("dumpsys power"):
            return f"  mWakefulness={'Awake' if s['awake'] else 'Asleep'}"
        if command.startswith("dumpsys window"):
            return f"  mCurrentFocus=Window{{7e2 u0 {s['front']}/{s['front']}.MainActivity}}" if s["front"] else ""
        if command.startswith("dumpsys media_session"):
            return s["media"]
        if command.startswith("uiautomator"):
            return s["ui"]
        if command == "pm list packages":
            return "package:com.amazon.tv.launcher\npackage:com.stremio.one\npackage:org.videolan.vlc\n"
        if command == "screencap -p":
            return b"\x89PNG fake"
        if "KEYCODE_WAKEUP" in command:
            s["awake"] = True
        elif "KEYCODE_SLEEP" in command:
            s["awake"] = False
        elif command.startswith("monkey -p "):
            s["front"] = command.split()[2]
        return ""


def test_what_the_stick_says_is_read():
    stick = Stick(front="com.stremio.one", media=MEDIA, ui=UI)
    assert stick.awake() and stick.app() == "com.stremio.one"
    assert stick.media()[0] == ("com.stremio.one", "playing", "Toy Story")
    assert stick.playing() == "playing: Toy Story (com.stremio.one)"
    assert stick.ui().splitlines() == [
        "Search @[10,10][200,60]",
        "[focused] Toy Story @[100,300][400,700]",
        "Toy Story 4 @[420,300][720,700]",
    ]
    assert "app: com.stremio.one\nplaying: Toy Story" in screen(stick)


def test_typing_goes_as_one_word_with_quotes_kept():
    stick = Stick()
    stick.type("it's toy story")
    assert stick.commands[-1] == "input text 'it'\\''s%stoy%sstory'"


def test_apps_are_found_by_name_or_among_whats_installed():
    stick = Stick()
    assert stick.find_app("Stremio") == "com.stremio.one"
    assert stick.find_app("VLC") == "org.videolan.vlc"
    assert stick.find_app("hbo") is None


def tv(stick: Stick, agent=None) -> TV:
    clock = [0.0]

    def sleep(s):
        clock[0] += s

    return TV(stick, agent, clock=lambda: clock[0], sleep=sleep)


def test_on_wakes_the_stick_and_waits_for_it_to_say_so():
    stick = Stick(awake=False)
    assert tv(stick).call("on", {}, Request()).startswith("done: the fire tv is awake")
    assert "input keyevent KEYCODE_WAKEUP" in stick.commands


def test_an_app_is_open_only_when_its_in_front():
    stick = Stick()
    assert tv(stick).call("open", {"app": "stremio"}, Request()) == "done: stremio is open (the fire tv says so)"
    stuck = Stick()
    stuck.open = lambda package: None  # it never comes to the front
    assert tv(stuck).call("open", {"app": "stremio"}, Request()).startswith("not confirmed")
    assert tv(Stick()).call("open", {"app": "hbo"}, Request()).startswith("error: there's no app called 'hbo'")


def test_a_button_press_says_what_is_playing_after_it():
    stick = Stick(media=MEDIA)
    said = tv(stick).call("press", {"button": "play_pause"}, Request())
    assert said == "pressed play_pause; playing: Toy Story (com.stremio.one)"
    assert tv(stick).call("press", {"button": "eject"}, Request()).startswith("error: the fire tv's buttons are")


def test_do_hands_the_goal_to_the_agent_and_answers_at_once():
    agent = types.SimpleNamespace(goals=[], stop=lambda: None)
    agent.start = lambda goal, who: agent.goals.append((goal, who))
    said = tv(Stick(), agent).call("do", {"goal": "play Toy Story on Stremio"}, Request("alon"))
    assert said.startswith("started") and agent.goals == [("play Toy Story on Stremio", "alon")]


def test_without_openai_do_says_what_else_it_can_do():
    assert "needs OPENAI_API_KEY" in tv(Stick()).call("do", {"goal": "play Toy Story"}, Request())


def test_a_stick_that_doesnt_answer_is_an_error_not_a_crash():
    stick = Stick()

    def down(*args, **kwargs):
        raise HomeError("the Fire TV didn't answer at 10.0.0.8")

    stick._run = down
    with pytest.raises(HomeError):
        tv(stick).status()


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
    stick = Stick(awake=False)
    client = calls(
        ("open_app", {"app": "Stremio"}),
        ("look", {}),
        ("type_text", {"text": "toy story"}),
        ("screenshot", {}),
        ("press", {"button": "select", "times": 1}),
        ("done", {"success": True, "say": "Toy Story is playing."}),
    )
    agent = TvAgent(stick, client, "gpt", lambda line, who: None, sleep=lambda s: None)
    assert agent.work("play Toy Story on Stremio") == (True, "Toy Story is playing.")
    assert "input keyevent KEYCODE_WAKEUP" in stick.commands  # it woke the TV first
    assert stick.state["front"] == "com.stremio.one" and "input text 'toy%sstory'" in stick.commands
    last = client.requests[-1]["input"]
    assert any(isinstance(i.get("content"), list) and i["content"][0]["type"] == "input_image" for i in last)
    assert "play Toy Story on Stremio" in client.requests[0]["instructions"]


def test_the_agent_gives_up_when_its_out_of_steps():
    client = calls(*[("look", {})] * 5)
    agent = TvAgent(Stick(), client, "gpt", lambda line, who: None, steps=3, sleep=lambda s: None)
    success, said = agent.work("Play the news")
    assert not success and said == "I ran out of time trying to play the news."


def test_the_agent_says_how_it_went_to_whoever_asked():
    said = []
    client = calls(("done", {"success": False, "say": "Stremio found no streams for Toy Story."}))
    agent = TvAgent(Stick(), client, "gpt", lambda line, who: said.append((line, who)), sleep=lambda s: None)
    agent.start("play Toy Story on Stremio", "alon", wait=True)
    assert said == [("Stremio found no streams for Toy Story.", "alon")]


def test_a_stopped_agent_says_nothing():
    said, started = [], threading.Event()

    def create(**kw):
        started.set()
        agent.stop()
        return types.SimpleNamespace(output=[], output_text="")

    client = types.SimpleNamespace(responses=types.SimpleNamespace(create=create))
    agent = TvAgent(Stick(), client, "gpt", lambda line, who: said.append(line), sleep=lambda s: None)
    agent.start("play Toy Story", wait=True)
    assert started.is_set() and said == []


# Saying it.


def test_what_tars_announces_is_said_after_a_chime_and_joins_the_conversation(speaker):
    assistant, brain = make_assistant(speaker, [None], [])
    played = []
    speaker.play_pcm_stream = lambda chunks, *a, **kw: played.extend(chunks)
    assistant.voice.stream = lambda text: iter([text.encode()])
    assistant.announcements = Announcements()
    assistant.announcements.add("Toy Story is playing.", "alon")
    assert assistant._due()
    assistant.say_announcements(follow_up_s=4.0)
    assert played[-1] == b"Toy Story is playing." and brain.remembered == ["Toy Story is playing."]
    assert not assistant._due()


def test_a_reply_to_an_announcement_is_heard_without_the_wake_word(speaker):
    assistant, brain = make_assistant(speaker, [speech(), None], ["the first one"], replies=["Playing Toy Story."])
    assistant.announcements = Announcements()
    assistant.announcements.add("Toy Story, or Toy Story 4?")
    assistant.say_announcements(follow_up_s=4.0)
    assert brain.asked == ["[Follow-up, no wake word] the first one"]
