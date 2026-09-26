"""Retraining the double-check from the household's wakes: which wakes it learns from, when a candidate replaces the
layer in use, the version history, and the running assistant picking up a new version."""

import json
import threading
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from voice_assistant import retrain
from voice_assistant.checks import INSTALLED, CheckVersions
from voice_assistant.events import NOT_REAL, REAL, EventLog
from voice_assistant.retrain import Retrainer, training_label
from voice_assistant.verify import ANSWER, ASK, IGNORE, VerifiedTrigger
from voice_assistant.webui import create_app

from .conftest import FakeTrigger, SilentMic

# A learned layer over two phrases: features are (how far "hey tars" is behind the top guess, the same for
# "hey cars", heard nothing). It accepts a clear "hey tars" and turns down a clear "hey cars".
SPEC = {
    "phrases": ["hey tars", "hey cars"],
    "extra_grammar": [],
    "max_alternatives": 8,
    "floor": -40.0,
    "weights": [0.2, -0.2, -3.0],
    "bias": 0.0,
    "threshold": 0.5,
}
TARS, CARS = [0.0, -40.0, 0.0], [-40.0, 0.0, 0.0]
SOFT_T = [-10.0, 0.0, 0.0]  # a real "hey TARS" the recognizer ranks just behind "hey cars"
INSTALLED_FILE = "models/test/hey_tars_check.json"


def write_setup(root: Path, setup: str = "test") -> None:
    """models/<setup>/: the installed layer, and the data it was trained and tested on."""
    folder = root / "models" / setup
    folder.mkdir(parents=True)
    (folder / "hey_tars_check.json").write_text(json.dumps(SPEC))
    rng = np.random.default_rng(0)
    clips = np.array([TARS] * 50 + [CARS] * 50, np.float32) + rng.normal(0, 1, (100, 3)).astype(np.float32)
    clips[:, 2] = 0
    np.savez(
        folder / "hey_tars_check_data.npz",
        train_X=clips,
        train_y=np.array([1] * 50 + [0] * 50, np.int8),
        train_w=np.ones(100, np.float32),
        test_set=np.array(["other voices hey TARS"] * 20 + ["other lookalikes"] * 20),
        test_cond=np.array(["clean", "tv_10dB"] * 20),
        test_should=np.array([True] * 20 + [False] * 20),
        test_fired=np.ones(40, bool),
        test_X=np.array([TARS] * 20 + [CARS] * 20, np.float32),
        long_name=np.array(["TV"]),
        long_X=np.array([CARS], np.float32),
        long_hours=json.dumps({"TV": 1.0}),
    )


class FeatureTable:
    """Stands in for the recognizer: each test wake's audio is filled with the index of its features."""

    def __init__(self):
        self.rows: list[list[float]] = []
        self.heard = 0

    def audio(self, features: list[float]) -> np.ndarray:
        self.rows.append(features)
        return np.full(1600, len(self.rows) - 1, np.int16)

    def features(self, pcm: np.ndarray) -> np.ndarray:
        self.heard += 1
        return np.array(self.rows[int(pcm[0])])


@pytest.fixture
def setup(tmp_path):
    write_setup(tmp_path)
    log = EventLog(tmp_path / "events")
    table = FeatureTable()
    versions = CheckVersions(log.folder, tmp_path, INSTALLED_FILE)
    retrainer = Retrainer(log, versions, tmp_path, lambda: table, {"wake_model": "test.tflite"})
    return log, table, versions, retrainer


def add_wakes(log, table, features, label, n):
    for _ in range(n):
        log.set_label(log.add_wake(table.audio(features), 0.9, IGNORE, "hey cars", 0.1), label)


def test_it_learns_from_people_and_from_what_happened_but_not_from_the_check_itself():
    def event(label=None, auto=None, outcome=ANSWER):
        return {"label": label, "auto_label": auto, "outcome": outcome}

    assert training_label(event(auto=REAL)) == REAL  # a request followed
    assert training_label(event(auto=NOT_REAL, outcome=ASK)) == NOT_REAL  # nobody answered "Did you call me?"
    assert training_label(event(auto=NOT_REAL, outcome=IGNORE)) is None  # only the check's own verdict
    assert training_label(event(label=REAL, auto=NOT_REAL, outcome=IGNORE)) == REAL  # a person said it was real


def test_a_candidate_that_hears_the_household_better_replaces_the_check(setup):
    log, table, versions, retrainer = setup
    add_wakes(log, table, SOFT_T, REAL, 25)
    result = retrainer.retrain()
    assert result["status"] == "swapped" and result["version"] == "check v1" and result["labeled"] == 20
    rows = {m["name"]: m for m in result["metrics"]}
    assert rows["Your held-out hey TARS"] == {
        "name": "Your held-out hey TARS",
        "current": "0 of 5",
        "candidate": "5 of 5",
        "lower_is_better": False,
    }
    assert rows["Lookalikes let through"]["candidate"] == "0.0%"
    in_use = versions.in_use()
    assert in_use.version == "check v1" and in_use.name.endswith("checks/test/v1.json")
    assert in_use.spec["threshold"] == SPEC["threshold"]
    assert [v["version"] for v in versions.versions()] == ["check v1", INSTALLED]
    info = retrainer.info()
    assert info["last_retrain"]["version"] == "check v1" and info["active"]["check_version"] == "check v1"


def test_a_candidate_that_lets_lookalikes_through_is_not_used(setup):
    log, table, versions, retrainer = setup
    add_wakes(log, table, CARS, REAL, 25)  # "real" wakes that sound exactly like the lookalikes
    result = retrainer.retrain()
    assert result["status"] == "kept" and "version" not in result
    assert versions.in_use().version == INSTALLED
    assert retrainer.info()["last_retrain"]["status"] == "kept"


def test_too_few_wakes_to_test_on_says_so_and_changes_nothing(setup):
    log, table, versions, retrainer = setup
    add_wakes(log, table, SOFT_T, REAL, 4)  # ids 1-4: none of them is held out
    result = retrainer.retrain()
    assert result["status"] == "skipped" and "every fifth" in result["summary"]
    assert [v["version"] for v in versions.versions()] == [INSTALLED]


def test_without_the_training_data_it_says_what_is_missing(setup, tmp_path):
    retrainer = setup[-1]
    (tmp_path / "models/test/hey_tars_check_data.npz").unlink()
    assert "hey_tars_check_data.npz" in retrainer.retrain()["summary"]


def test_the_recognizer_runs_once_per_wake(setup):
    log, table, _, retrainer = setup
    add_wakes(log, table, SOFT_T, REAL, 10)
    first = retrainer.retrain()
    assert table.heard == 10
    add_wakes(log, table, SOFT_T, REAL, 5)
    second = retrainer.retrain()
    assert table.heard == 15 and first["labeled"] == 8 and second["labeled"] == 12


def test_rolling_back_and_forward(setup):
    log, table, versions, retrainer = setup
    add_wakes(log, table, SOFT_T, REAL, 25)
    retrainer.retrain()
    retrainer.use(INSTALLED)
    assert versions.in_use().version == INSTALLED
    assert [v["active"] for v in versions.versions()] == [False, True]
    retrainer.use("check v1")
    assert versions.in_use().version == "check v1"
    with pytest.raises(KeyError):
        retrainer.use("check v9")


def test_a_rollback_made_while_retraining_wins(setup, monkeypatch):
    log, table, versions, retrainer = setup
    add_wakes(log, table, SOFT_T, REAL, 25)
    retrainer.retrain()
    retrainer.use(INSTALLED)
    compare = retrain.compare

    def switch_meanwhile(*args):
        versions.use("check v1")  # someone taps "Use this" while the candidate is being tested
        return compare(*args)

    monkeypatch.setattr(retrain, "compare", switch_meanwhile)
    result = retrainer.retrain()
    assert result["status"] == "kept" and "changed from installed" in result["summary"]
    assert versions.in_use().version == "check v1"
    assert [v["version"] for v in versions.versions()] == ["check v1", INSTALLED]


def test_each_setup_keeps_its_own_history(tmp_path):
    write_setup(tmp_path, "generic")
    write_setup(tmp_path, "personal")
    generic = CheckVersions(tmp_path / "events", tmp_path, "models/generic/hey_tars_check.json")
    personal = CheckVersions(tmp_path / "events", tmp_path, "models/personal/hey_tars_check.json")
    generic.add(SPEC, "retrained", {"status": "swapped"}, based_on=INSTALLED)
    assert generic.in_use().name.endswith("generic/v1.json")
    assert personal.in_use().version == INSTALLED


def test_versions_made_from_another_installed_layer_are_set_aside(setup, tmp_path):
    log, table, versions, retrainer = setup
    add_wakes(log, table, SOFT_T, REAL, 25)
    retrainer.retrain()
    (tmp_path / INSTALLED_FILE).write_text(json.dumps({**SPEC, "bias": 0.1}))  # an update replaced it
    assert versions.in_use().version == INSTALLED and "has changed" in retrainer.info()["problem"]
    retrainer.use(INSTALLED)  # the next change starts a new history, and numbers go on from the old files
    assert retrainer.info()["problem"] is None
    assert list(versions.folder.glob("history-*.old.json"))
    assert versions.add(SPEC, "retrained", {"status": "swapped"}, based_on=INSTALLED) == "check v2"


@pytest.mark.parametrize("damage", ["", "{half a file", "[]"])
def test_a_damaged_history_still_lets_tars_start_and_the_page_load(setup, damage):
    _, _, versions, retrainer = setup
    versions.folder.mkdir(parents=True)
    (versions.folder / "history.json").write_text(damage)
    assert versions.in_use().version == INSTALLED
    info = retrainer.info()
    assert "couldn't be read" in info["problem"] and info["history"][0]["version"] == INSTALLED


def test_a_damaged_version_file_falls_back_to_the_installed_layer(setup, capsys):
    log, table, versions, retrainer = setup
    add_wakes(log, table, SOFT_T, REAL, 25)
    retrainer.retrain()
    (versions.folder / "v1.json").write_text("")  # a power cut before it reached the disk
    assert versions.in_use().version == INSTALLED and "Couldn't load" in capsys.readouterr().out
    with pytest.raises(KeyError):
        versions.use("check v1")


def test_a_wake_whose_audio_is_gone_or_cut_short_is_left_out(setup):
    log, table, _, retrainer = setup
    add_wakes(log, table, SOFT_T, REAL, 10)
    (log.folder / log.get(1)["audio"]).unlink()
    (log.folder / log.get(2)["audio"]).write_bytes(b"")
    result = retrainer.retrain()
    assert result["labeled"] == 6 and table.heard == 8  # ids 3, 4, 6-9 trained; 5 and 10 held out
    retrainer.retrain()
    assert table.heard == 8  # the rest were cached


def test_a_failed_retrain_is_shown_and_keeps_what_the_recognizer_did(setup):
    log, table, _, retrainer = setup
    add_wakes(log, table, SOFT_T, REAL, 10)
    features = table.features

    def fail_on_the_sixth(pcm):
        if table.heard == 5:
            raise RuntimeError("the recognizer crashed")
        return features(pcm)

    table.features = fail_on_the_sixth
    result = retrainer.retrain()
    assert result["status"] == "skipped" and "the recognizer crashed" in result["summary"]
    assert retrainer.info()["last_retrain"]["summary"] == result["summary"]
    table.features = features
    assert retrainer.retrain()["status"] in ("swapped", "kept") and table.heard == 10


def test_a_damaged_feature_cache_is_made_again(setup):
    log, table, versions, retrainer = setup
    add_wakes(log, table, SOFT_T, REAL, 25)
    versions.folder.mkdir(parents=True)
    (versions.folder / "features.npz").write_bytes(b"not a zip")
    assert retrainer.retrain()["status"] == "swapped" and table.heard == 25


def test_without_a_learned_layer_the_page_still_shows_the_wake_model(log, tmp_path):
    retrainer = Retrainer(log, None, tmp_path, lambda: None, {"wake_model": "test.tflite"})
    assert retrainer.info()["active"] == {"wake_model": "test.tflite", "check_model": "plain phrase match"}
    assert "no learned layer" in retrainer.retrain()["summary"]


class SwitchableVerifier:
    def __init__(self):
        self.used: list[dict] = []

    def use_check(self, spec):
        self.used.append(spec)

    def decide(self, pcm):
        return ANSWER, "hey tars"


def test_the_running_assistant_switches_to_a_new_version_at_the_next_wake(setup, capsys):
    versions = setup[2]
    verifier = SwitchableVerifier()
    trigger = VerifiedTrigger(FakeTrigger(fire_at=[2, 4, 6]), verifier, versions=versions)
    assert verifier.used == [SPEC]  # it starts with the one in use
    trigger.wait(SilentMic(10))
    assert len(verifier.used) == 1
    versions.add({**SPEC, "bias": 1.0}, "retrained", {"status": "swapped"}, based_on=INSTALLED)
    trigger.wait(SilentMic(10))
    assert verifier.used[-1]["bias"] == 1.0 and "now check v1" in capsys.readouterr().out
    (versions.folder / "v1.json").write_text(json.dumps({**SPEC, "bias": 2.5}))  # the same file, rewritten
    trigger.wait(SilentMic(10))
    assert verifier.used[-1]["bias"] == 2.5


def test_a_broken_installed_layer_mid_run_keeps_the_check_it_has(setup, tmp_path, capsys):
    versions = setup[2]
    verifier = SwitchableVerifier()
    trigger = VerifiedTrigger(FakeTrigger(fire_at=[2]), verifier, versions=versions)
    (tmp_path / INSTALLED_FILE).unlink()
    assert trigger.wait(SilentMic(10)) == ANSWER
    assert verifier.used == [SPEC] and "keeping" in capsys.readouterr().out


class SlowModels:
    def __init__(self):
        self.started, self.release = threading.Event(), threading.Event()

    def info(self):
        return {"active": {}, "history": [], "last_retrain": None, "trainable": 0}

    def retrain(self):
        self.started.set()
        self.release.wait(5)
        return {"status": "kept"}

    def use(self, version):
        if version != INSTALLED:
            raise KeyError(version)


def test_the_api_retrains_one_at_a_time_and_switches_versions(log):
    models = SlowModels()
    client = TestClient(create_app(log, models=models))
    first = []
    running = threading.Thread(target=lambda: first.append(client.post("/api/retrain")))
    running.start()
    models.started.wait(5)
    assert client.post("/api/retrain").status_code == 409
    models.release.set()
    running.join(5)
    assert first[0].json() == {"status": "kept"}
    assert client.post("/api/models/use", json={"version": INSTALLED}).json() == {"ok": True}
    assert client.post("/api/models/use", json={"version": "check v9"}).status_code == 404


def test_without_the_models_page_there_is_nothing_to_retrain(client):
    assert client.post("/api/retrain").status_code == 501
    assert client.get("/api/models").json()["history"] == []
