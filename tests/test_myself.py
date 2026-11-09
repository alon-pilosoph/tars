"""The tars tool: TARS knowing itself, from its git history, its docs, its records and its log, with someone's chats
kept theirs."""

import dataclasses
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from voice_assistant.config import TelegramConfig, load_config
from voice_assistant.events import EventLog
from voice_assistant.home import Request
from voice_assistant.myself import ACTIONS, Myself

NOW = datetime(2026, 11, 8, 22, 0).astimezone()


@pytest.fixture
def root(tmp_path):
    """A checkout with two changes in its history, and docs."""
    root = tmp_path / "tars"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "skills.md").write_text(
        "# Skills\n\n## Teaching one\n\nTell TARS what to do and when, in your own words: a skill.\n\n"
        "## Running on something happening\n\nA skill can run when someone comes home.\n"
    )
    (root / "README.md").write_text("# TARS\n\nA voice assistant.\n")
    git = ["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@x"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    for when, title in (("2026-11-01T10:00", "Feeds by talking (#84)"), ("2026-11-08T19:00", "All of HA (#94)")):
        subprocess.run([*git, "add", "-A"], check=True)
        env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when, "PATH": "/usr/bin:/bin:/usr/local/bin"}
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", title], check=True, env=env)
    return root


@pytest.fixture
def cfg():
    shipped = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    return dataclasses.replace(shipped, telegram=TelegramConfig(enabled=True, people={"Alon": 1, "Stacey": 2}))


def remember(log: EventLog, via: str | None, turns: list[tuple[float, str, str | None, str]]) -> None:
    """A conversation: (hours before NOW, role, speaker, text) a turn."""
    with log.store.transaction() as db:
        cid = db.execute("INSERT INTO conversations (started, via) VALUES (?, ?)", (NOW.timestamp(), via)).lastrowid
        for hours, role, speaker, text in turns:
            db.execute(
                "INSERT INTO turns (conversation_id, ts, role, speaker, text) VALUES (?, ?, ?, ?, ?)",
                (cid, NOW.timestamp() - hours * 3600, role, speaker, text),
            )


def me(cfg, root, log=None, run=subprocess.run) -> Myself:
    return Myself(cfg, root, log, run=run, unit_state=lambda unit: None, now=lambda: NOW)


def ask(tars: Myself, action: str, request=None, **args) -> str:
    return tars.call(
        {"action": action, **dict.fromkeys(("query", "since", "until", "why")), **args}, request or Request()
    )


def test_about_says_the_version_and_whats_set_up(cfg, root):
    said = ask(me(cfg, root), "about")
    assert "version: " in said and "All of HA (#94)" in said
    assert "Telegram: for Alon, Stacey" in said and "models: " in said


def test_whats_new_comes_from_its_own_history(cfg, root):
    assert ask(me(cfg, root), "changes", since="2026-11-05").endswith("2026-11-08 All of HA (#94)")
    both = ask(me(cfg, root), "changes")  # the last two weeks
    assert "Feeds by talking (#84)" in both and "All of HA (#94)" in both
    assert ask(me(cfg, root), "changes", since="2026-11-09") == "no changes since 2026-11-09"


def test_how_it_works_comes_from_its_docs(cfg, root):
    said = ask(me(cfg, root), "docs", query="teach a skill")
    assert said.startswith("[skills.md — Teaching one]") and "in your own words" in said
    assert "say nothing about" in ask(me(cfg, root), "docs", query="spaceship")


def test_records_are_what_was_said_and_a_chat_is_only_its_own_persons(cfg, root, tmp_path):
    log = EventLog(tmp_path / "events")
    remember(log, None, [(3, "person", "Alon", "Put the news on."), (2.9, "tars", None, "Which channel, 11 or 12?")])
    remember(log, "telegram", [(2, "person", "Stacey", "Keep me updated"), (1.9, "tars", None, "I can't monitor that")])
    tars = me(cfg, root, log)
    stacey = ask(tars, "records", Request(speaker="Stacey", by_chat=True))
    assert "Stacey: Keep me updated" in stacey and "Put the news on." in stacey  # hers, and what was said aloud
    alon = ask(tars, "records", Request(speaker="Alon", by_chat=True))
    assert "Keep me updated" not in alon and "1 other people's chats in that span left out" in alon
    assert "Which channel" in ask(tars, "records", Request(), query="channel")
    searched = Request(speaker="Stacey", by_chat=True, read_the_web=True)
    assert ask(tars, "records", searched).startswith("refused: this reply searched the web")


def test_the_log_leaves_out_chat_messages_and_finds_what_went_wrong(cfg, root, monkeypatch):
    journal = (
        "Nov 08 20:55:15 homepi uv[1]: Stacey (Telegram):  'something private'\n"
        'Nov 08 20:55:20 homepi uv[1]: (tool feeds {"action": "follow"}: error: couldn\'t follow it)\n'
        "Nov 08 20:55:21 homepi uv[1]: (home: boiler on: done)\n"
    )

    def run(command, **kw):
        if command[0] == "journalctl":
            return subprocess.CompletedProcess(command, 0, journal, "")
        return subprocess.run(command, **kw)  # noqa: PLW1510 - the caller says check

    monkeypatch.setattr("voice_assistant.myself.shutil.which", lambda name: f"/usr/bin/{name}")
    said = ask(me(cfg, root, run=run), "log")
    assert "something private" not in said and "20:55:20 (tool feeds" in said and "(home: boiler on: done)" in said
    assert ask(me(cfg, root, run=run), "log", query="error").count("\n") == 0


def test_the_brain_has_it_and_knows_to_look_before_saying_it_cant(cfg, root):
    from voice_assistant.config import LLMConfig
    from voice_assistant.llm import OpenAIChat

    from .conftest import fake_openai_chat

    asked = {"action": "changes", "query": None, "since": None, "until": None, "why": None}
    first = lambda m: len(m) <= 2  # the question alone: the first round
    client = fake_openai_chat(
        lambda m: "" if first(m) else "New: all of Home Assistant.", lambda m: [("tars", asked)] * first(m)
    )
    brain = OpenAIChat(client, LLMConfig(), myself=me(cfg, root))
    assert "".join(brain.stream_reply("what's new?")) == "New: all of Home Assistant."
    assert "never say you can't tell something about yourself" in client.requests[0]["instructions"]
    output = next(i for i in client.requests[1]["input"] if i.get("type") == "function_call_output")
    assert "All of HA (#94)" in output["output"]
    assert set(ACTIONS) == set(client.requests[0]["tools"][-1]["parameters"]["properties"]["action"]["enum"])


def test_what_changed_between_two_versions_is_each_ones_title(cfg, root):
    tars = me(cfg, root)
    new = tars.version()
    old = tars._git("rev-parse", "--short", "HEAD~1")
    assert new and old and tars.between(old, new).endswith("All of HA (#94)")
    assert "Feeds by talking" not in tars.between(old, new)
    assert "All of HA (#94)" in tars.between("not-a-version", new)  # a history rewritten: the latest few


def test_what_it_really_cant_do_is_kept_as_a_wish_until_its_granted(cfg, root, tmp_path):
    tars = me(cfg, root, EventLog(tmp_path / "events"))
    assert ask(tars, "wishes") == "no wishes: nothing asked for lately that TARS couldn't do"
    stacey = Request(speaker="Stacey", by_chat=True)
    said = ask(
        tars,
        "wish",
        stacey,
        query="Call Grandma on the phone",
        why="TARS can't make phone calls; a calling service could",
    )
    assert said.startswith("noted for whoever looks after TARS (1)") and "never that it's a wish" in said
    assert "error: wish needs" in ask(tars, "wish", stacey, query="something")
    listed = ask(tars, "wishes")
    assert (
        "1. " in listed
        and "Stacey (chat): Call Grandma on the phone (stopped by: TARS can't make phone calls" in listed
    )
    assert ask(tars, "granted", query="1") == "wish 1 marked done"
    assert ask(tars, "granted", query="1") == "error: there's no open wish 1"
    assert ask(tars, "wishes").startswith("no wishes")


def test_every_model_is_told_to_combine_what_it_has_and_its_cant_list_follows_whats_set_up(cfg, root):
    from voice_assistant import llm
    from voice_assistant.config import HomeConfig, LLMConfig
    from voice_assistant.home import HomeTools
    from voice_assistant.llm import OpenAIChat

    from .conftest import fake_openai_chat

    client = fake_openai_chat(lambda m: "Fine.")
    spotify = type("Spotify", (), {"what": lambda self: "music", "rules": lambda self: "", "actions": ("play",)})()
    house = HomeTools(HomeConfig(), {"spotify": spotify})
    brain = OpenAIChat(client, LLMConfig(), home=house, myself=me(cfg, root))
    "".join(brain.stream_reply("hi"))
    told = client.requests[0]["instructions"]
    assert llm.CANT_MUSIC not in told and llm.CANT_UNLISTED in told and llm.CANT_CALL in told
    assert "note it there as a wish" in told


def test_a_wish_reaches_the_maintainers_chats_and_only_they_mark_it_done(cfg, root, tmp_path):
    from voice_assistant.config import HouseholdConfig
    from voice_assistant.reminders import TELEGRAM, line

    log = EventLog(tmp_path / "events")
    tars = me(dataclasses.replace(cfg, household=HouseholdConfig(maintainers=["Alon"])), root, log)
    stacey = Request(speaker="Stacey", by_chat=True)
    said = ask(tars, "wish", stacey, query="Read me the kindergarten's WhatsApp updates", why="I can't reach WhatsApp")
    assert said.startswith("passed on to Alon in their chat (1)")
    (message,) = log.store.rows("SELECT * FROM reminders")
    assert message["channel"] == TELEGRAM and message["for_name"] == "Alon" and message["set_via"] == TELEGRAM
    assert line(message) == (
        "Alon, a message: Stacey asked me: Read me the kindergarten's WhatsApp updates. I couldn't: I can't reach "
        "WhatsApp."
    )
    alon = Request(speaker="Alon", by_chat=True)
    assert ask(tars, "wish", alon, query="Call my mother", why="no phone line").startswith("noted for whoever")
    assert len(log.store.rows("SELECT * FROM reminders")) == 1  # his own wish isn't sent back to him
    assert ask(tars, "granted", stacey, query="1").startswith("refused: only TARS's maintainers")
    assert ask(tars, "granted", alon, query="1") == "wish 1 marked done"
