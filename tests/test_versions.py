"""The wake models' versions: installing a trained pair, switching back, the running assistant following along,
and never being stopped by a damaged file."""

import json
import shutil
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voice_assistant.verify import ANSWER, VerifiedTrigger
from voice_assistant.versions import ABOUT, CHECK, INSTALLED, MODEL, FixedPair, ModelVersions, Pair
from voice_assistant.webui import create_app

from .conftest import GENERIC, FakeTrigger, SilentMic, needs_models

pytestmark = needs_models


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


def version_names(versions: ModelVersions) -> list[str]:
    return [v["version"] for v in versions.overview()[1]]


def test_installing_a_trained_pair_puts_all_of_it_in_use(root):
    versions = versions_in(root)
    assert versions.in_use().version == INSTALLED
    assert versions.install(trained(root)) == "v1"
    pair = versions.in_use()
    assert (pair.version, pair.threshold, pair.check_window_s, pair.check["bias"]) == ("v1", 0.6, 2.5, 1.0)
    assert pair.wake_model == "events/wake_models/generic/v1/hey_tars.tflite"
    assert pair.results[0]["candidate"] == "2 of 2"
    assert pair.replaced == INSTALLED  # what its results compare it with
    assert version_names(versions) == ["v1", INSTALLED]


def test_rolling_back_and_forward(root):
    versions = versions_in(root)
    versions.install(trained(root))
    versions.use(INSTALLED)
    assert versions.in_use().version == INSTALLED
    assert [v["active"] for v in versions.overview()[1]] == [False, True]
    versions.use("v1")
    assert versions.in_use().version == "v1"
    with pytest.raises(KeyError):
        versions.use("v9")


def test_a_pair_tested_against_another_version_is_not_installed(root):
    versions = versions_in(root)
    versions.install(trained(root, "first"))
    versions.use(INSTALLED)  # someone switched back after the training run started from v1
    with pytest.raises(ValueError, match="tested against v1"):
        versions.install(trained(root, "second"), based_on="v1")
    assert versions.in_use().version == INSTALLED


@pytest.mark.parametrize(
    "break_it",
    [
        lambda f: (f / MODEL).write_bytes(b"not a model"),
        lambda f: (f / CHECK).write_text("{half"),
        lambda f: (f / CHECK).write_text(json.dumps({**json.loads((f / CHECK).read_text()), "weights": [0.1]})),
        lambda f: (f / ABOUT).write_text(json.dumps({"threshold": 7, "check_window_s": 2})),
    ],
)
def test_a_broken_pair_is_refused(root, break_it):
    folder = trained(root)
    break_it(folder)
    with pytest.raises(Exception):  # noqa: B017 - whatever is wrong with it, nothing is installed
        versions_in(root).install(folder)
    assert versions_in(root).in_use().version == INSTALLED


@pytest.mark.parametrize(
    "damage",
    [
        lambda v1: (v1 / CHECK).write_text(""),  # a power cut before it reached the disk
        lambda v1: (v1 / CHECK).write_text(json.dumps({**json.loads((v1 / CHECK).read_text()), "weights": [0.1]})),
    ],
)
def test_a_damaged_version_falls_back_to_the_installed_pair(root, capsys, damage):
    versions = versions_in(root)
    versions.install(trained(root))
    damage(versions.folder / "v1")
    assert versions.in_use().version == INSTALLED and "couldn't load" in capsys.readouterr().out
    with pytest.raises(KeyError):
        versions.use("v1")


def test_the_models_page_shows_a_damaged_version_as_not_in_use(root):
    versions = versions_in(root)
    versions.install(trained(root))
    (versions.folder / "v1" / MODEL).write_bytes(b"cut short")
    pair, rows, problem = versions.overview()
    assert pair.version == INSTALLED and [r["version"] for r in rows if r["active"]] == [INSTALLED]
    assert "Couldn't load the wake models v1" in problem


def test_a_damaged_wake_model_cant_be_chosen(root):
    versions = versions_in(root)
    versions.install(trained(root))
    versions.use(INSTALLED)
    (versions.folder / "v1" / MODEL).write_bytes(b"cut short")
    with pytest.raises(KeyError, match="can't be loaded"):
        versions.use("v1")


@pytest.mark.parametrize("damage", ["", "{half a file", "[]"])
def test_a_damaged_history_is_set_aside_by_the_next_change(root, damage):
    versions = versions_in(root)
    versions.folder.mkdir(parents=True)
    (versions.folder / "history.json").write_text(damage)
    assert versions.in_use().version == INSTALLED and "couldn't be read" in versions.overview()[2]
    assert versions.install(trained(root)) == "v1"
    assert versions.overview()[2] is None and list(versions.folder.glob("history-*.old.json"))


def test_pairs_made_from_other_installed_models_are_set_aside(root):
    versions = versions_in(root)
    versions.install(trained(root, "first"))
    check = root / "models/generic/hey_tars_check.json"
    check.write_text(json.dumps({**json.loads(check.read_text()), "bias": 0.1}))  # an update replaced it
    assert versions.in_use().version == INSTALLED and "have changed" in versions.overview()[2]
    assert versions.install(trained(root, "second")) == "v2"  # numbers go on: v1's folder is still there


class RecordingVerifier:
    def __init__(self):
        self.used: list[dict] = []
        self.last_confidence = None

    def use_check(self, spec):
        self.used.append(spec)

    def decide(self, pcm):
        return ANSWER, "hey tars"


class LoadedTriggers:
    """Stands in for loading a wake model: records what was loaded, and fires at the given blocks. A version in
    `broken` fails to load, like a damaged model file."""

    def __init__(self, fire_at, broken=()):
        self.loaded: list[tuple[str, float]] = []
        self.fire_at, self.broken = fire_at, broken

    def __call__(self, path, threshold):
        folder = Path(path).parts[-2]
        if folder in self.broken:
            raise ValueError("The model is not a valid Flatbuffer buffer")
        self.loaded.append((folder, threshold))
        trigger = FakeTrigger(fire_at=self.fire_at)
        trigger.threshold = threshold
        return trigger


def listening(root: Path, versions: ModelVersions, triggers=None, verifier=None) -> VerifiedTrigger:
    verifier = verifier or RecordingVerifier()
    return VerifiedTrigger(
        versions,
        root / "models",
        make_trigger=triggers or LoadedTriggers(fire_at=[3]),
        make_verifier=lambda phrase, folder: verifier,
    )


def test_the_running_assistant_switches_the_whole_pair(root, capsys, monkeypatch):
    monkeypatch.setattr("voice_assistant.verify.FOLLOW_EVERY_S", 0.08)  # look every block
    versions = versions_in(root)
    verifier, triggers = RecordingVerifier(), LoadedTriggers(fire_at=[3])
    trigger = listening(root, versions, triggers, verifier)
    assert triggers.loaded == [("generic", 0.5)] and len(verifier.used) == 1  # the pair in use, from the start
    versions.install(trained(root))
    assert trigger.wait(SilentMic(10)) == ANSWER
    assert triggers.loaded[-1] == ("v1", 0.6) and verifier.used[-1]["bias"] == 1.0
    assert trigger.pair.check_window_s == 2.5 and "wake models v1" in capsys.readouterr().out


def test_a_pair_that_fails_to_load_mid_run_keeps_the_installed_one(root, capsys, monkeypatch):
    monkeypatch.setattr("voice_assistant.verify.FOLLOW_EVERY_S", 0.08)
    versions = versions_in(root)
    trigger = listening(root, versions, LoadedTriggers(fire_at=[3], broken={"v1"}))
    versions.install(trained(root))
    with pytest.raises(StopIteration):  # it kept listening with the installed pair; the fake mic ran out
        trigger.wait(SilentMic(2))
    assert "couldn't load the wake models v1" in capsys.readouterr().out and trigger.pair.version == INSTALLED


def test_a_damaged_wake_model_at_startup_falls_back_to_the_installed_pair(root, capsys):
    versions = versions_in(root)
    versions.install(trained(root))
    (versions.folder / "v1" / MODEL).write_bytes(b"cut short")  # only loading it shows the damage
    trigger = VerifiedTrigger(versions, root / "models", make_verifier=lambda phrase, folder: RecordingVerifier())
    assert trigger.pair.version == INSTALLED and "couldn't load the wake models v1" in capsys.readouterr().out


def test_a_broken_installed_pair_is_a_startup_error(root):
    versions = versions_in(root)
    with pytest.raises(ValueError):
        listening(root, versions, LoadedTriggers(fire_at=[], broken={"generic"}))


def test_the_models_page_shows_the_pair_its_results_and_what_is_waiting(root, log):
    versions = versions_in(root)
    client = TestClient(create_app(log, pairs=versions))
    versions.install(trained(root))
    page = client.get("/api/models").json()
    assert page["active"]["version"] == "v1" and page["active"]["replaced"] == INSTALLED
    assert page["results"][0]["candidate"] == "2 of 2"
    assert page["learning"] == {"real": 0, "not_real": 0, "missed": 0, "to_review": 0}
    assert client.post("/api/models/use", json={"version": INSTALLED}).json() == {"ok": True}
    assert client.get("/api/models").json()["active"]["version"] == INSTALLED
    assert client.post("/api/models/use", json={"version": "v9"}).status_code == 404


def test_without_versions_the_page_shows_what_config_installs(log):
    pair = Pair(INSTALLED, "hey_jarvis", "", None, 0.5, 2.5, Path("hey_jarvis"))
    client = TestClient(create_app(log, pairs=FixedPair(pair)))
    page = client.get("/api/models").json()
    assert page["active"] == {
        "version": INSTALLED,
        "wake_model": "hey_jarvis",
        "threshold": 0.5,
        "check_model": None,
        "check_window_s": 2.5,
        "replaced": None,
    }
    assert page["history"] == [] and client.post("/api/models/use", json={"version": INSTALLED}).status_code == 404


def test_installs_and_the_web_ui_never_lose_each_others_changes(root):
    """An install runs as its own process and "Use this" in the web UI's: two ModelVersions, sharing only files."""
    install, web = versions_in(root), versions_in(root)
    folders = [trained(root, f"run{i}") for i in range(4)]
    threads = [threading.Thread(target=install.install, args=(f,)) for f in folders]
    threads += [threading.Thread(target=web.use, args=(INSTALLED,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(version_names(web)) == [INSTALLED, "v1", "v2", "v3", "v4"]
