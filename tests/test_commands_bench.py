"""tools/commands_bench.py: house recording sessions' takes, heard as the Pi would, scored against what they mean."""

import random
import sys
from pathlib import Path

import pytest

from voice_assistant.commands import Command, Commands, DeviceWords
from voice_assistant.errors import ServiceError
from voice_assistant.events import EventLog
from voice_assistant.recording import HOUSE, VOICE, Prompt, Sessions, house_script

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
from commands_bench import any_wrong, expected, read_takes, score, summary

DEVICES = [("ac", "AC", ("status", "on", "off", "set"), True), ("front door", "Front door", ("lock", "unlock"), False)]
NAMES = {"ac": "ac", "front door": "front door"}


def test_what_a_take_means_is_the_command_the_pi_would_do_or_none():
    assert expected("AC|set:23", NAMES) == Command("ac", "set", 23)
    assert expected("front door|lock", NAMES) == Command("front door", "lock")
    assert expected("front door|unlock", NAMES) is None  # never on the Pi
    assert expected("front door|locked", NAMES) is None  # a question
    assert expected("", NAMES) is None


def test_a_take_names_a_device_by_its_label_and_means_its_key_in_config():
    names = {"living room ac": "ac_living", "ac_living": "ac_living"}
    assert expected("Living room AC|set:22", names) == Command("ac_living", "set", 22)


def test_every_command_the_house_script_asks_for_is_one_of_the_phrases():
    devices = [
        DeviceWords("ac_living", "Living room AC", ("status", "on", "off", "set"), True),
        DeviceWords("front_door", "Front door", ("status", "lock", "unlock"), False),
        DeviceWords("boiler", "Boiler", ("status", "on", "off"), False),
        DeviceWords("tv", "Fire TV", ("status", "on", "off"), False),
    ]
    commands = Commands(devices)
    names = {d.label.lower(): d.name for d in devices}
    for seed in range(60):  # every phrasing, in every opening, for each device
        for p in house_script(devices, random.Random(seed)):
            if want := expected(p.means or "", names):
                assert commands.match(p.say) == want, p.say


def take(prompt, say, means, problem=None):
    return {"session": 1, "prompt": prompt, "say": say, "means": means, "pcm": say.encode(), "seconds": 1.5,
            "problem": problem}  # fmt: skip


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
    assert "commands        2 takes: 1 right, 1 missed, 0 wrong (0 of the missed not in the phrases, 1 not" in said
    assert "not commands    1 takes: 0 right, 0 missed, 1 wrong" in said
    assert 'WRONG n1: "Don\'t lock the front door yet."' in said
    assert any_wrong(results)


def test_a_missed_command_the_phrases_dont_have_is_told_from_one_not_heard():
    commands = Commands(DEVICES)
    results = score([take("h1", "Could the AC go to 23 degrees?", "AC|set:23")], commands, NAMES, lambda pcm: "")
    assert results[0].outcome == "missed" and results[0].written == ""
    assert "(1 of the missed not in the phrases, 0 not heard)" in summary(results)


def test_a_wrong_take_says_what_its_check_found():
    results = score([take("n1", "Don't lock the front door yet.", "", problem="quiet")], Commands(DEVICES), NAMES,
                    lambda pcm: "lock the front door")  # fmt: skip
    assert results[0].problem == "quiet"
    assert "WRONG n1 (quiet):" in summary(results)


def test_the_cloud_is_timed_and_its_transcript_matched_too():
    commands = Commands(DEVICES)
    results = score([take("h1", "Lock the front door.", "front door|lock")], commands, NAMES, lambda pcm: "",
                    cloud=lambda pcm: "Lock the front door.")  # fmt: skip
    assert results[0].cloud_matched == "front door|lock" and results[0].cloud_ms is not None
    assert "the cloud's transcripts, matched: 1 right, 0 missed, 0 wrong" in summary(results)


def test_a_wrong_cloud_match_is_called_out_and_fails_the_run():
    results = score([take("n1", "Don't lock the front door yet.", "")], Commands(DEVICES), NAMES, lambda pcm: "",
                    cloud=lambda pcm: "Lock the front door.")  # fmt: skip
    assert results[0].outcome == "right" and results[0].cloud_outcome == "wrong"
    said = summary(results)
    assert "the cloud's transcripts, matched: 0 right, 0 missed, 1 wrong" in said and "WRONG n1" in said
    assert any_wrong(results)


def test_the_cloud_failing_on_a_take_is_kept_and_the_run_goes_on():
    def cloud(pcm):
        if pcm.startswith(b"Turn"):
            raise ServiceError("Deepgram speech to text: timed out")
        return pcm.decode()

    takes = [take("h1", "Turn on the AC.", "AC|on"), take("h2", "Lock the front door.", "front door|lock")]
    results = score(takes, Commands(DEVICES), NAMES, lambda pcm: "", cloud=cloud)
    assert results[0].cloud_error == "Deepgram speech to text: timed out" and results[0].cloud_outcome is None
    assert results[1].cloud_outcome == "right"
    said = summary(results)
    assert "matched: 1 right, 0 missed, 0 wrong" in said and "the cloud failed on 1 takes" in said
    assert not any_wrong(results)


def test_takes_are_read_from_house_sessions_with_what_each_prompt_meant(tmp_path):
    log = EventLog(tmp_path / "events")
    sessions = Sessions(log.store)
    prompts = [Prompt("h1", "Turn on the AC.", means="AC|on"), Prompt("c1", "Who's home?", means="")]
    session = sessions.open(sessions.create(HOUSE, "Alon", prompts))
    sessions.add_take(session, prompts[0], b"\1\0" * 16000, "silence")
    sessions.add_take(session, prompts[1], None, "nobody")  # nothing heard: no take to play
    (t,), skipped = read_takes(log.store, None)
    assert (t["prompt"], t["means"], t["say"], len(t["pcm"])) == ("h1", "AC|on", "Turn on the AC.", 32000)
    assert skipped == []


def test_one_session_is_read_by_its_token_and_voice_sessions_never(tmp_path):
    log = EventLog(tmp_path / "events")
    sessions = Sessions(log.store)
    prompt = Prompt("h1", "Turn on the AC.", means="AC|on")
    tokens = [sessions.create(HOUSE, person, [prompt]) for person in ("Alon", "Dana")]
    for token in tokens:
        sessions.add_take(sessions.open(token), prompt, b"\1\0" * 16000, "silence")
    voice = sessions.create(VOICE, "Alon", [Prompt("v1", "Thanks, that's all I needed.")])
    sessions.add_take(sessions.open(voice), Prompt("v1", "Thanks, that's all I needed."), b"\1\0" * 16000, "silence")
    assert len(read_takes(log.store, None)[0]) == 2
    (t,), _ = read_takes(log.store, tokens[1])
    assert t["session"] == sessions.open(tokens[1])["id"]
    with pytest.raises(LookupError):
        read_takes(log.store, voice)
    with pytest.raises(LookupError):
        read_takes(log.store, "no-such-token")


def test_a_take_whose_audio_is_gone_or_damaged_is_skipped_with_why(tmp_path):
    log = EventLog(tmp_path / "events")
    sessions = Sessions(log.store)
    prompts = [Prompt(f"h{n}", "Turn on the AC.", means="AC|on") for n in (1, 2, 3)]
    session = sessions.open(sessions.create(HOUSE, "Alon", prompts))
    for p in prompts:
        sessions.add_take(session, p, b"\1\0" * 16000, "silence")
    audio = {t["prompt"]: log.store.folder / t["audio"] for t in log.store.rows("SELECT * FROM takes")}
    audio["h1"].unlink()
    audio["h2"].write_bytes(b"not a wav")
    takes, skipped = read_takes(log.store, None)
    assert [t["prompt"] for t in takes] == ["h3"]
    assert len(skipped) == 2 and "h1" in skipped[0] and "h2" in skipped[1]
    assert "2 takes skipped" in summary([], len(skipped))
