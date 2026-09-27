"""The wake models' versions: installing a trained pair, switching back, the running assistant following along,
and never being stopped by a damaged file. Also which wakes training learns from."""

import json
import shutil
import threading
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from voice_assistant.events import NOT_REAL, REAL, EventLog, learning_label
from voice_assistant.verify import ANSWER, ASK, IGNORE, VerifiedTrigger
from voice_assistant.versions import ABOUT, CHECK, INSTALLED, MODEL, ModelVersions
from voice_assistant.webui import create_app

from .conftest import FakeTrigger, SilentMic

REPO = Path(__file__).parents[1]
GENERIC = REPO / "models" / "generic"
needs_models = pytest.mark.skipif(not (GENERIC / "hey_tars.tflite").exists(), reason="no generic wake model")


@pytest.fixture
def root(tmp_path):
    """A project with the installed pair in models/generic/."""
    (tmp_path / "models").mkdir()
    shutil.copytree(GENERIC, tmp_path / "models" / "generic")
    return tmp_path


def versions_in(root: Path) -> ModelVersions:
    return ModelVersions(
        root / "events", root, "models/generic/hey_tars.tflite", "models/generic/hey_tars_check.json", 0.5, 3.0
    )


def trained(root: Path, name: str = "run", bias: float = 1.0, threshold: float = 0.6, window: float = 2.5) -> Path:
    """A pair as training.household leaves it: the installed files, with a different layer and settings."""
    folder = root / name
    folder.mkdir()
    shutil.copyfile(GENERIC / "hey_tars.tflite", folder / MODEL)
    check = json.loads((GENERIC / "hey_tars_check.json").read_text())
    (folder / CHECK).write_text(json.dumps({**check, "bias": bias}))
    (folder / ABOUT).write_text(
        json.dumps(
            {
                "threshold": threshold,
                "check_window_s": window,
                "note": "trained",
                "results": [{"name": "Your held-out hey TARS", "current": "1 of 2", "candidate": "2 of 2"}],
            }
        )
    )
    return folder


def test_training_learns_from_people_and_from_what_happened_but_not_from_the_check_itself():
    def event(label=None, auto=None, outcome=ANSWER):
        return {"label": label, "auto_label": auto, "outcome": outcome}

    assert learning_label(event(auto=REAL)) == REAL  # a request followed
    assert learning_label(event(auto=NOT_REAL, outcome=ASK)) == NOT_REAL  # nobody answered "Did you call me?"
    assert learning_label(event(auto=NOT_REAL, outcome=IGNORE)) is None  # only the check's own verdict
    assert learning_label(event(label=REAL, auto=NOT_REAL, outcome=IGNORE)) == REAL  # a person said it was real


def test_what_is_waiting_to_be_learned_from(tmp_path):
    log = EventLog(tmp_path / "events")
    pcm = np.zeros(1600, np.int16)
    log.set_label(log.add_wake(pcm, 0.9, IGNORE, "hey cars", 0.1, ts=100), REAL)  # answered in Review
    log.add_wake(pcm, 0.9, IGNORE, "hey cars", 0.1, ts=101)  # the check's own verdict: waits for Review
    log.set_label(log.add_wake(pcm, 0.9, ANSWER, "hey tars", 0.9, ts=102), NOT_REAL)
    log.set_label(log.add_near_miss(pcm, 0.4, ts=103), REAL)
    assert log.learning() == {"real": 1, "not_real": 1, "missed": 1, "to_review": 1}
    assert log.learning(since=101.5) == {"real": 0, "not_real": 1, "missed": 1, "to_review": 0}


@needs_models
def test_installing_a_trained_pair_puts_all_of_it_in_use(root):
    versions = versions_in(root)
    assert versions.in_use().version == INSTALLED
    assert versions.install(trained(root)) == "v1"
    pair = versions.in_use()
    assert (pair.version, pair.threshold, pair.check_window_s, pair.check["bias"]) == ("v1", 0.6, 2.5, 1.0)
    assert (
        pair.wake_model == "events/wake_models/generic/v1/hey_tars.tflite" and pair.results[0]["candidate"] == "2 of 2"
    )
    assert [v["version"] for v in versions.versions()] == ["v1", INSTALLED]


@needs_models
def test_rolling_back_and_forward(root):
    versions = versions_in(root)
    versions.install(trained(root))
    versions.use(INSTALLED)
    assert versions.in_use().version == INSTALLED
    assert [v["active"] for v in versions.versions()] == [False, True]
    versions.use("v1")
    assert versions.in_use().version == "v1"
    with pytest.raises(KeyError):
        versions.use("v9")


@needs_models
def test_a_pair_tested_against_another_version_is_not_installed(root):
    versions = versions_in(root)
    versions.install(trained(root, "first"))
    versions.use(INSTALLED)  # someone switched back after the training run started from v1
    with pytest.raises(ValueError, match="tested against v1"):
        versions.install(trained(root, "second"), based_on="v1")
    assert versions.in_use().version == INSTALLED


@needs_models
@pytest.mark.parametrize(
    "break_it",
    [
        lambda f: (f / MODEL).write_bytes(b"not a model"),
        lambda f: (f / CHECK).write_text("{half"),
        lambda f: (f / ABOUT).write_text(json.dumps({"threshold": 7, "check_window_s": 2})),
    ],
)
def test_a_broken_pair_is_refused(root, break_it):
    folder = trained(root)
    break_it(folder)
    with pytest.raises(Exception):  # noqa: B017 - whatever is wrong with it, nothing is installed
        versions_in(root).install(folder)
    assert versions_in(root).in_use().version == INSTALLED


@needs_models
def test_a_damaged_version_falls_back_to_the_installed_pair(root, capsys):
    versions = versions_in(root)
    versions.install(trained(root))
    (versions.folder / "v1" / CHECK).write_text("")  # a power cut before it reached the disk
    assert versions.in_use().version == INSTALLED and "Couldn't load" in capsys.readouterr().out
    with pytest.raises(KeyError):
        versions.use("v1")


@needs_models
@pytest.mark.parametrize("damage", ["", "{half a file", "[]"])
def test_a_damaged_history_is_set_aside_by_the_next_change(root, damage):
    versions = versions_in(root)
    versions.folder.mkdir(parents=True)
    (versions.folder / "history.json").write_text(damage)
    assert versions.in_use().version == INSTALLED and "couldn't be read" in versions.problem()
    assert versions.install(trained(root)) == "v1"
    assert versions.problem() is None and list(versions.folder.glob("history-*.old.json"))


@needs_models
def test_pairs_made_from_other_installed_models_are_set_aside(root):
    versions = versions_in(root)
    versions.install(trained(root, "first"))
    check = root / "models/generic/hey_tars_check.json"
    check.write_text(json.dumps({**json.loads(check.read_text()), "bias": 0.1}))  # an update replaced it
    assert versions.in_use().version == INSTALLED and "have changed" in versions.problem()
    assert versions.install(trained(root, "second")) == "v2"  # numbers go on: v1's folder is still there


class RecordingVerifier:
    def __init__(self):
        self.used: list[dict] = []

    def use_check(self, spec):
        self.used.append(spec)

    def decide(self, pcm):
        return ANSWER, "hey tars"


class LoadedTriggers:
    """Stands in for loading a wake model: records what was loaded, and fires at the given blocks."""

    def __init__(self, fire_at):
        self.loaded: list[tuple[str, float]] = []
        self.fire_at = fire_at

    def __call__(self, path, threshold):
        self.loaded.append((Path(path).parts[-2], threshold))
        trigger = FakeTrigger(fire_at=self.fire_at)
        trigger.threshold = threshold
        return trigger


@needs_models
def test_the_running_assistant_switches_the_whole_pair(root, capsys, monkeypatch):
    monkeypatch.setattr("voice_assistant.verify.FOLLOW_EVERY_S", 0.08)  # look every block
    versions = versions_in(root)
    verifier, triggers = RecordingVerifier(), LoadedTriggers(fire_at=[3])
    listening = VerifiedTrigger(FakeTrigger(fire_at=[]), verifier, versions=versions, make_trigger=triggers)
    assert triggers.loaded == [("generic", 0.5)] and len(verifier.used) == 1  # the pair in use, from the start
    versions.install(trained(root))
    assert listening.wait(SilentMic(10)) == ANSWER
    assert triggers.loaded[-1] == ("v1", 0.6) and verifier.used[-1]["bias"] == 1.0
    assert listening._window_s == 2.5 and "wake models v1" in capsys.readouterr().out


@needs_models
def test_a_pair_that_fails_to_load_mid_run_keeps_the_one_it_has(root, capsys, monkeypatch):
    monkeypatch.setattr("voice_assistant.verify.FOLLOW_EVERY_S", 0.08)
    versions = versions_in(root)
    triggers = LoadedTriggers(fire_at=[3])
    listening = VerifiedTrigger(FakeTrigger(fire_at=[]), RecordingVerifier(), versions=versions, make_trigger=triggers)

    def broken(path, threshold):
        raise RuntimeError("the interpreter crashed")

    listening._make_trigger = broken
    versions.install(trained(root))
    with pytest.raises(StopIteration):  # it kept listening with the installed pair; the fake mic ran out
        listening.wait(SilentMic(2))
    assert "keeping installed" in capsys.readouterr().out


@needs_models
def test_the_models_page_shows_the_pair_its_results_and_what_is_waiting(root):
    versions = versions_in(root)
    log = EventLog(root / "events")
    client = TestClient(create_app(log, versions=versions))
    versions.install(trained(root))
    page = client.get("/api/models").json()
    assert page["active"]["version"] == "v1" and page["results"][0]["candidate"] == "2 of 2"
    assert page["learning"] == {"real": 0, "not_real": 0, "missed": 0, "to_review": 0}
    assert client.post("/api/models/use", json={"version": INSTALLED}).json() == {"ok": True}
    assert client.get("/api/models").json()["active"]["version"] == INSTALLED
    assert client.post("/api/models/use", json={"version": "v9"}).status_code == 404


def test_without_versions_the_page_shows_what_config_installs(log):
    client = TestClient(create_app(log, installed={"wake_model": "hey_jarvis", "threshold": 0.5}))
    assert client.get("/api/models").json()["active"] == {
        "version": INSTALLED,
        "wake_model": "hey_jarvis",
        "threshold": 0.5,
    }
    assert client.post("/api/models/use", json={"version": INSTALLED}).status_code == 404
    assert client.post("/api/retrain").status_code == 404  # training is a command on the Mac now


@needs_models
def test_installs_and_the_web_ui_never_lose_each_others_changes(root):
    versions = versions_in(root)
    folders = [trained(root, f"run{i}") for i in range(4)]
    threads = [threading.Thread(target=versions.install, args=(f,)) for f in folders]
    threads += [threading.Thread(target=versions.use, args=(INSTALLED,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(v["version"] for v in versions.versions()) == [INSTALLED, "v1", "v2", "v3", "v4"]
