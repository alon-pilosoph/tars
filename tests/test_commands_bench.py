"""tools/commands_bench.py: house recording sessions' takes, heard as the Pi would, scored against what they mean."""

import sys
from pathlib import Path

from voice_assistant.commands import Command, Commands
from voice_assistant.events import EventLog
from voice_assistant.recording import HOUSE, Prompt, Sessions

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
from commands_bench import expected, read_takes, score, summary

DEVICES = [("ac", "AC", ("status", "on", "off", "set"), True), ("front door", "Front door", ("lock", "unlock"), False)]
NAMES = {"ac": "ac", "front door": "front door"}


def test_what_a_take_means_is_the_command_the_pi_would_do_or_none():
    assert expected("AC|set:23", NAMES) == Command("ac", "set", 23)
    assert expected("front door|lock", NAMES) == Command("front door", "lock")
    assert expected("front door|unlock", NAMES) is None  # never on the Pi
    assert expected("front door|locked", NAMES) is None  # a question
    assert expected("", NAMES) is None


def take(prompt, say, means):
    return {"session": 1, "prompt": prompt, "say": say, "means": means, "pcm": say.encode(), "seconds": 1.5}


def test_takes_are_right_missed_or_wrong_and_a_wrong_one_is_called_out():
    commands = Commands(DEVICES)
    heard = {
        "Turn on the AC.": "turn on the a c",
        "AC to 23 degrees.": "",
        "Don't lock the front door yet.": "lock the front door",
    }
    results = score(
        [take("h1", "Turn on the AC.", "AC|on"), take("h2", "AC to 23 degrees.", "AC|set:23"),
         take("n1", "Don't lock the front door yet.", "")],
        commands, NAMES, lambda pcm: heard[pcm.decode()],
    )  # fmt: skip
    assert [r.outcome for r in results] == ["right", "missed", "wrong"]
    said = summary(results)
    assert "commands        2 takes: 1 right, 1 missed, 0 wrong" in said
    assert "not commands    1 takes: 0 right, 0 missed, 1 wrong" in said
    assert 'WRONG n1: "Don\'t lock the front door yet."' in said


def test_the_cloud_is_timed_and_its_transcript_matched_too():
    commands = Commands(DEVICES)
    results = score([take("h1", "Lock the front door.", "front door|lock")], commands, NAMES, lambda pcm: "",
                    cloud=lambda pcm: "Lock the front door.")  # fmt: skip
    assert results[0].cloud_matched == "front door|lock" and results[0].cloud_ms is not None
    assert "the cloud's transcripts, matched: 1 right, 0 wrong" in summary(results)


def test_takes_are_read_from_house_sessions_with_what_each_prompt_meant(tmp_path):
    log = EventLog(tmp_path / "events")
    sessions = Sessions(log.store)
    prompts = [Prompt("h1", "Turn on the AC.", means="AC|on"), Prompt("c1", "Who's home?", means="")]
    session = sessions.open(sessions.create(HOUSE, "Alon", prompts))
    sessions.add_take(session, prompts[0], b"\1\0" * 16000, "silence")
    sessions.add_take(session, prompts[1], None, "nobody")  # nothing heard: no take to play
    (t,) = read_takes(log.store, None)
    assert (t["prompt"], t["means"], t["say"], len(t["pcm"])) == ("h1", "AC|on", "Turn on the AC.", 32000)
