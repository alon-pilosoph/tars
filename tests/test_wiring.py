"""The Household (wiring.py): the parts the processes share, each made the first time it's asked for, and only then;
and what each process asks it for, and doesn't."""

import dataclasses
import threading
from pathlib import Path

import pytest

from voice_assistant import entrypoints, wiring
from voice_assistant.config import load_config
from voice_assistant.reminders import Reminders
from voice_assistant.wiring import Household

REPO_CONFIG = Path(__file__).parents[1] / "config.toml"
PARTS = {"events", "presence", "speaker_id", "reminders", "skills", "feeds", "wishes", "myself"}


def config(log_events: bool = True, presence: bool = True, **parts):
    cfg = load_config(REPO_CONFIG, local=False)
    return dataclasses.replace(
        cfg,
        learning=dataclasses.replace(cfg.learning, log_events=log_events),
        presence=dataclasses.replace(cfg.presence, enabled=presence, people={"Alon": ["ca:fe:00:00:00:01"]}),
        speaker=dataclasses.replace(cfg.speaker, enabled=False),  # its model is a download
        **parts,
    )


def made(household: Household) -> set[str]:
    return set(vars(household)) & PARTS


@pytest.fixture
def factories(monkeypatch):
    """Each factory the household makes its parts with, recording that it was called, in order."""
    calls = []
    for name in ("make_event_log", "make_presence", "make_speaker_id", "make_skills", "make_feeds", "make_wishes"):
        real = getattr(wiring, name)
        monkeypatch.setattr(wiring, name, lambda *a, real=real, name=name: calls.append(name) or real(*a))
    real_myself = wiring.make_myself
    monkeypatch.setattr(wiring, "make_myself", lambda *a: calls.append("make_myself") or real_myself(*a))
    return calls


def test_a_household_makes_nothing_until_a_part_is_asked_for(tmp_path, factories):
    household = Household(config(), tmp_path)
    assert factories == [] and made(household) == set()
    assert not (tmp_path / "voice_data").exists()  # not even the event log's folder


def test_a_part_is_made_once_after_what_it_needs_and_nothing_else(tmp_path, factories):
    household = Household(config(), tmp_path)
    myself = household.myself
    asked = factories.copy()  # with [wishes] off, make_myself asks make_wishes again, as it always has
    assert asked[:3] == ["make_event_log", "make_wishes", "make_myself"] and set(asked[3:]) <= {"make_wishes"}
    assert household.myself is myself and factories == asked
    assert household.presence is not None and factories == [*asked, "make_presence"]
    assert factories.count("make_event_log") == 1
    assert made(household) == {"events", "wishes", "myself", "presence"}


def test_the_reminders_are_the_tables_on_the_event_log_and_none_without_it_or_with_them_off(tmp_path):
    household = Household(config(), tmp_path)
    assert isinstance(household.reminders, Reminders) and household.reminders.store is household.events.store
    assert Household(config(log_events=False), tmp_path).reminders is None
    cfg = config()
    off = Household(dataclasses.replace(cfg, reminders=dataclasses.replace(cfg.reminders, enabled=False)), tmp_path)
    assert off.reminders is None and made(off) == {"reminders"}  # the event log isn't needed to know that


def test_without_the_event_log_every_part_kept_in_it_is_left_out_saying_so(tmp_path, capsys):
    household = Household(config(log_events=False), tmp_path)
    assert all(getattr(household, part) is None for part in PARTS - {"myself"})
    assert household.myself is not None  # TARS knows itself without a database, from its files
    out = capsys.readouterr().out
    assert "(presence: off" in out and "(skills: off" in out and "(feeds: off" in out


def test_every_part_made_starts_no_thread_and_opens_no_device(tmp_path, monkeypatch):
    def no_device(*a, **kw):
        raise AssertionError("a household part opened an audio device")

    monkeypatch.setattr("voice_assistant.audio.Microphone", no_device)
    monkeypatch.setattr("voice_assistant.audio.Speaker", no_device)
    before = set(threading.enumerate())
    household = Household(config(), tmp_path)
    for part in sorted(PARTS):
        getattr(household, part)
    assert made(household) == PARTS
    assert set(threading.enumerate()) == before
    assert household.presence.board.store is household.events.store  # who's home is read, never asked for


@pytest.fixture
def households(monkeypatch):
    """The Household each entrypoint makes, kept to see what it was asked for."""
    kept = []

    class Kept(Household):
        def __init__(self, cfg, root):
            super().__init__(cfg, root)
            kept.append(self)

    monkeypatch.setattr(entrypoints, "Household", Kept)
    monkeypatch.setattr(entrypoints, "start_heartbeat", lambda *a: None)
    return kept


class Stop(Exception):
    """Raised where a test has seen enough of an entrypoint: before it opens a device or talks to a service."""


def test_the_web_ui_asks_for_the_event_log_the_voices_the_reminders_and_whos_home_only(
    tmp_path, monkeypatch, households
):
    from voice_assistant import webui

    def serve(log, host, port, **kw):
        raise Stop(kw)

    def no_device(*a, **kw):
        raise AssertionError("the web UI opened an audio device")

    monkeypatch.setattr(webui, "serve", serve)
    monkeypatch.setattr(entrypoints, "pair_source", lambda cfg, root: None)
    monkeypatch.setattr(entrypoints, "Microphone", no_device)
    monkeypatch.setattr(entrypoints, "start_presence", no_device)
    with pytest.raises(Stop) as served:
        entrypoints.web_ui(config(), tmp_path, "127.0.0.1", 0)
    (household,) = households
    assert made(household) == {"events", "speaker_id", "reminders", "presence"}
    assert served.value.args[0]["reminders"] is household.reminders


@pytest.mark.parametrize(("push_to_talk", "reminders"), [(False, {"reminders"}), (True, set())])
def test_the_assistant_asks_for_every_part_but_the_reminders_with_push_to_talk(
    tmp_path, monkeypatch, households, push_to_talk, reminders
):
    def pipeline(*a, **kw):
        raise Stop(kw)

    monkeypatch.setattr(entrypoints, "make_pipeline", pipeline)
    with pytest.raises(Stop) as asked:
        entrypoints.run(config(), push_to_talk, tmp_path)
    (household,) = households
    assert made(household) == (PARTS - {"reminders"}) | reminders
    kw = asked.value.args[0]
    assert (kw["skills"], kw["feeds"], kw["myself"]) == (household.skills, household.feeds, household.myself)
    assert (kw["reminders"] is None) == push_to_talk


def test_typed_questions_ask_for_neither_the_voices_nor_the_reminders(tmp_path, monkeypatch, households):
    def pipeline(*a, **kw):
        raise Stop(kw)

    monkeypatch.setattr(entrypoints, "make_pipeline", pipeline)
    with pytest.raises(Stop):
        entrypoints.typed(config(), tmp_path)
    (household,) = households
    assert made(household) == PARTS - {"speaker_id", "reminders"}
