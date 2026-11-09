import dataclasses
import subprocess
from pathlib import Path

import pytest

from voice_assistant.config import BackupConfig, HomeConfig, load_config
from voice_assistant.haconfig import CHANGED_IN_HA, HAConfig, HAConfigError, new_key, seal, unseal


def shipped_config():
    return load_config(Path(__file__).parents[1] / "config.toml", local=False)


@pytest.fixture
def setup(tmp_path):
    """A Home Assistant config folder, versioned with the machine's own git; Docker's stops and starts are kept."""
    folder = tmp_path / "config"
    (folder / ".storage").mkdir(parents=True)
    (folder / "configuration.yaml").write_text("default_config:\n")
    (folder / ".storage" / "core.config_entries").write_text('{"entries": ["nuki", "hubble"]}')
    (folder / "home-assistant.log").write_text("a log line\n")
    (folder / "home-assistant_v2.db").write_bytes(b"history")
    (folder / ".storage" / "core.restore_state").write_text("{}")
    docker = []

    def run(command, **kw):
        if command[0] == "docker":
            docker.append(command[1:])
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return subprocess.run(command, check=False, **kw)

    git = ["git", "-C", str(folder), "-c", "user.name=TARS", "-c", "user.email=tars@localhost"]
    ha = HAConfig(folder, "homeassistant", run=run, git=git)
    ha.docker = docker
    return ha


def tracked(ha: HAConfig) -> set[str]:
    return set(ha.git("ls-files").stdout.decode().split())


def test_the_first_commit_keeps_the_setup_and_leaves_out_logs_history_and_what_ha_rewrites(setup):
    assert setup.start() and setup.ready()
    assert tracked(setup) == {".gitignore", "configuration.yaml", ".storage/core.config_entries"}
    assert not setup.start()  # already a repository: nothing made again


def test_a_pass_commits_only_when_something_changed(setup):
    setup.start()
    assert setup.commit(CHANGED_IN_HA) is None
    (setup.folder / "home-assistant.log").write_text("more\n")  # ignored: still nothing
    assert setup.commit(CHANGED_IN_HA) is None
    (setup.folder / "automations.yaml").write_text("- alias: lights\n")
    made = setup.commit(CHANGED_IN_HA)
    assert made and made == setup.head()
    latest = setup.history(1)[0]
    assert latest.message == CHANGED_IN_HA and latest.files == ("automations.yaml",)


def test_restoring_stops_ha_puts_the_files_back_as_a_new_commit_and_starts_it(setup):
    setup.start()
    first = setup.head()
    entries = setup.folder / ".storage" / "core.config_entries"
    entries.write_text('{"entries": ["hubble"]}')  # the Nuki removed
    setup.commit("Removed the Nuki, Alon asked")
    (setup.folder / "scripts.yaml").write_text("bedtime: {}\n")  # added later, in HA's own app, not yet committed
    made = setup.restore(first[:8])
    assert entries.read_text() == '{"entries": ["nuki", "hubble"]}'
    assert not (setup.folder / "scripts.yaml").exists()
    assert setup.docker == [["stop", "homeassistant"], ["start", "homeassistant"]]
    messages = [c.message for c in setup.history()]
    restored = f"Restored to {first[:8]} (Home Assistant's setup as it was when TARS started keeping its history)"
    assert messages[:3] == [restored, CHANGED_IN_HA, "Removed the Nuki, Alon asked"]
    assert made == setup.head()  # nothing rewritten: the restore is one more change, and can itself be undone


def test_ha_starts_again_even_when_the_restore_fails(setup):
    setup.start()
    calls = setup._run

    def broken(command, **kw):
        if "restore" in command:
            return subprocess.CompletedProcess(command, 1, b"", b"fatal: disk full")
        return calls(command, **kw)

    setup._run = broken
    with pytest.raises(HAConfigError, match="disk full"):
        setup.restore(setup.head())
    assert setup.docker[-1] == ["start", "homeassistant"]


def test_restoring_a_change_that_isnt_there_touches_nothing(setup):
    setup.start()
    with pytest.raises(HAConfigError, match="no change 'deadbeef'"):
        setup.restore("deadbeef")
    assert setup.docker == []


def test_a_bundle_makes_the_same_history_again_in_an_empty_folder_and_nowhere_else(setup, tmp_path):
    setup.start()
    (setup.folder / "scenes.yaml").write_text("- name: movie\n")
    setup.commit(CHANGED_IN_HA)
    again = HAConfig(tmp_path / "new-pi", "homeassistant", git=["git", "-C", str(tmp_path / "new-pi")])
    again.clone(setup.bundle())
    assert [c.message for c in again.history()] == [c.message for c in setup.history()]
    assert (again.folder / "scenes.yaml").read_text() == "- name: movie\n"
    assert not (again.folder / ".tars-restoring.bundle").exists() and not again.git("status", "--porcelain").stdout
    with pytest.raises(HAConfigError, match="isn't empty"):
        again.clone(setup.bundle())


def test_a_missing_container_is_said_plainly(tmp_path):
    (tmp_path / "config").mkdir()

    def run(command, **kw):
        return subprocess.CompletedProcess(command, 1, b"", b"Error: No such object: homeassistant")

    with pytest.raises(HAConfigError, match="No such object"):
        HAConfig(tmp_path / "config", "homeassistant", run=run).start()


def test_check_says_how_recent_the_last_change_is_and_what_keeps_it(setup):
    from voice_assistant import check

    shipped = shipped_config()
    cfg = dataclasses.replace(shipped, home=HomeConfig(config_dir="config"), backup=BackupConfig(enabled=True))
    assert check.ha_config(shipped, lambda: None).status == check.SKIP
    assert check.ha_config(cfg, lambda: setup).detail == "no history yet"
    setup.start()
    found = check.ha_config(cfg, lambda: setup)
    assert found.status == check.OK and "just now: Home Assistant's setup as it was" in found.detail
    unsealed = check.ha_config(cfg, lambda: setup, sealed=False)
    assert unsealed.status == check.WARN and "not in the backup" in unsealed.detail
    assert "--ha-config-key" in unsealed.fix
    off = check.ha_config(dataclasses.replace(cfg, backup=BackupConfig()), lambda: setup)
    assert off.status == check.WARN and "the backup's passes do" in off.detail


def test_sealed_only_the_key_opens_it_and_a_wrong_or_broken_one_says_so():
    pytest.importorskip("cryptography")
    key = new_key()
    sealed = seal(b"the house's setup", key)
    assert b"the house's setup" not in sealed and unseal(sealed, key) == b"the house's setup"
    with pytest.raises(HAConfigError, match="won't open with this HA_CONFIG_KEY"):
        unseal(sealed, new_key())
    with pytest.raises(HAConfigError, match="won't open"):
        unseal(sealed[:-1] + bytes([sealed[-1] ^ 1]), key)
    with pytest.raises(HAConfigError, match="isn't a key"):
        seal(b"x", "not a key")
    with pytest.raises(HAConfigError, match="isn't a sealed history"):
        unseal(b"a plain git bundle", key)


def pass_setup(setup, monkeypatch):
    from voice_assistant import __main__ as cli

    cfg = dataclasses.replace(shipped_config(), home=HomeConfig(config_dir="config"))
    root = setup.folder.parent
    monkeypatch.setattr(cli, "make_ha_config", lambda c, r: setup)
    return cli, cfg, root


def test_without_a_key_a_pass_keeps_the_history_here_and_puts_nothing_in_the_backup(setup, monkeypatch, capsys):
    cli, cfg, root = pass_setup(setup, monkeypatch)
    cli.ha_config_pass(cfg, root)
    out = capsys.readouterr().out
    assert setup.ready() and "its history starts now" in out and "not in the backup" in out
    assert not cli.ha_bundle_path(cfg, root).exists()
    setup._git = ["git", "-C", str(root / "nowhere")]
    cli.ha_config_pass(cfg, root)
    assert "couldn't keep its history" in capsys.readouterr().out  # and no exception: the backup goes on


def test_with_a_key_the_backup_gets_it_sealed_and_a_new_machine_opens_it(setup, monkeypatch, capsys, tmp_path):
    pytest.importorskip("cryptography")
    cli, cfg, root = pass_setup(setup, monkeypatch)
    (root / ".env").write_text("HA_CONFIG_KEY=\n")  # as .env.example has it: empty
    cli.ha_config_key(root)
    assert "isn't shown here" in capsys.readouterr().out
    key = (root / ".env").read_text().strip().splitlines()[-1].split("=", 1)[1]
    assert key not in capsys.readouterr().out
    with pytest.raises(SystemExit, match="already"):
        cli.ha_config_key(root)  # a second would orphan what the first sealed
    cli.ha_config_pass(cfg, root)
    sealed = cli.ha_bundle_path(cfg, root)
    assert sealed.read_bytes().startswith(b"TARS sealed") and b"core.config_entries" not in sealed.read_bytes()
    assert cli.snapshot_files(cfg, root)["ha-config.bundle.sealed"] == sealed
    # A new machine: the snapshot's restored, .env has the key, and the folder isn't there yet.
    new = tmp_path / "new"
    (new / cfg.learning.folder).mkdir(parents=True)
    (new / ".env").write_text(f"HA_CONFIG_KEY={key}\n")
    (new / cfg.learning.folder / sealed.name).write_bytes(sealed.read_bytes())
    target = HAConfig(new / "config", "homeassistant", git=["git", "-C", str(new / "config")])
    monkeypatch.setattr(cli, "make_ha_config", lambda c, r: target)
    cli.ha_config_from_backup(cfg, new)
    assert (new / "config" / ".storage" / "core.config_entries").exists() and target.history()
