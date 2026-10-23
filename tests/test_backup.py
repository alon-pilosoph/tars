"""The backup to S3, against a pretend bucket: a bucket is one database's backup, and no other's writes go there;
everything TARS keeps is uploaded once; old recordings make room on the machine only once the bucket has them whole,
and never what's read from the machine later; what TARS stops keeping leaves the bucket too, but never most of it by
mistake; a snapshot every six hours, the newest always kept, goes back onto a new machine only with TARS stopped,
whose first pass brings back what must be on it; and the bucket's lifecycle rule follows [backup]."""

import dataclasses
import io
import json
import sqlite3
import tarfile
from pathlib import Path

import numpy as np
import pytest

from voice_assistant import __main__ as cli
from voice_assistant.audio import SAMPLE_RATE
from voice_assistant.backup import (
    ARCHIVE_RULE,
    NOT_OURS,
    OWNER,
    S3,
    SNAPSHOT_DB,
    Backup,
    BackupError,
    Busy,
    Status,
    exclusive,
    make_s3,
    restore_snapshot,
)
from voice_assistant.config import BackupConfig, Config, ConfigError, load_config
from voice_assistant.conversations import SentItem
from voice_assistant.events import DAY_S, NOT_PERSON, PERSON, VOICEPRINT_REQUESTS, EventLog

NOW = 1_800_000_000.0
REPO_CONFIG = Path(__file__).parents[1] / "config.toml"


class ClientError(Exception):
    """As botocore raises them: S3's error code and message in `response`."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message or code}}


class PretendS3:
    """boto3's S3 client, as far as a backup can tell: one bucket of objects. `archived`: keys moved to Deep Archive
    and not restored. `lifecycle_allowed`: whether the key may change the bucket's lifecycle rules. `page`: how many
    keys a listing returns at most."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.archived: set[str] = set()
        self.restoring: list[str] = []
        self.rules: list[dict] = [{"ID": "someone-elses", "Filter": {"Prefix": "photos/"}, "Status": "Enabled"}]
        self.lifecycle_allowed, self.lifecycle_down, self.down = True, False, False
        self.puts = self.lifecycle_puts = 0
        self.page = 1000

    def _bucket(self, Bucket):
        if self.down:
            raise ConnectionError("no route to host")
        assert Bucket == "tars-home"

    def head_bucket(self, Bucket):
        self._bucket(Bucket)

    def put_object(self, Bucket, Key, Body, ServerSideEncryption):
        self._bucket(Bucket)
        assert ServerSideEncryption == "AES256"
        self.objects[Key], self.puts = Body, self.puts + 1

    def head_object(self, Bucket, Key):
        self._bucket(Bucket)
        if Key not in self.objects:
            raise ClientError("404", "Not Found")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):
        self._bucket(Bucket)
        if Key in self.archived:
            raise ClientError("InvalidObjectState", "The operation is not valid for the object's storage class")
        if Key not in self.objects:
            raise ClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def restore_object(self, Bucket, Key, RestoreRequest):
        self._bucket(Bucket)
        if Key in self.restoring:
            raise ClientError("RestoreAlreadyInProgress")
        assert RestoreRequest["Tier"] == "Bulk"
        self.restoring.append(Key)

    def delete_objects(self, Bucket, Delete):
        self._bucket(Bucket)
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"], None)
        return {}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        self._bucket(Bucket)
        keys = [k for k in sorted(self.objects) if k.startswith(Prefix)]
        start = int(ContinuationToken or 0)
        page = keys[start : start + self.page]
        more = start + self.page < len(keys)
        found = {"Contents": [{"Key": k} for k in page], "IsTruncated": more}
        return found | ({"NextContinuationToken": str(start + self.page)} if more else {})

    def get_bucket_lifecycle_configuration(self, Bucket):
        self._bucket(Bucket)
        if self.lifecycle_down:
            raise ConnectionError("read timed out")
        if not self.rules:
            raise ClientError("NoSuchLifecycleConfiguration")
        return {"Rules": [dict(r) for r in self.rules]}

    def put_bucket_lifecycle_configuration(self, Bucket, LifecycleConfiguration):
        self._bucket(Bucket)
        if not self.lifecycle_allowed:
            raise ClientError("AccessDenied", "not allowed to s3:PutLifecycleConfiguration")
        assert LifecycleConfiguration["Rules"], "S3 refuses an empty list of rules"
        self.rules, self.lifecycle_puts = LifecycleConfiguration["Rules"], self.lifecycle_puts + 1

    def delete_bucket_lifecycle(self, Bucket):
        self._bucket(Bucket)
        self.rules = []


def config(**kw) -> BackupConfig:
    return BackupConfig(enabled=True, bucket="tars-home", region="eu-central-1", **kw)


@pytest.fixture
def s3():
    return PretendS3()


@pytest.fixture
def now():
    return [NOW]


def backup(log, s3, now, files=None, **kw) -> Backup:
    return Backup(log, config(**kw), S3(s3, "tars-home"), files or {}, clock=lambda: now[0])


def places(root: Path) -> dict[str, Path]:
    """Where a restore puts a snapshot's files on a machine whose project is at `root`."""
    return {SNAPSHOT_DB: root / "voice_data/events/events.db", "voiceprints.npz": root / "voice_data/voiceprints.npz"}


def snapshot_keys(s3) -> list[str]:
    return sorted(k for k in s3.objects if k.startswith("tars/snapshots/"))


def pcm(seconds=0.2):
    return np.zeros(int(SAMPLE_RATE * seconds), dtype=np.int16)


def a_request(log, convos, ts, speaker="alon"):
    """A wake that's training data (labeled real), the request after it, and the conversation it started."""
    wake = log.add_wake(pcm(), 0.9, "answer", "hey tars", 0.9, ts=ts)
    log.set_label(wake, "real")
    request = log.add_request(wake, pcm(), "what time is it", speaker, 0.8, None)
    conversation = convos.start(wake, ts=ts)
    convos.add_person_turn(conversation, "what time is it", audio=request, ts=ts)
    return wake, request, conversation


def a_passing_wake(log, ts):
    """A wake the check turned away, nobody answered in Review: not training data."""
    return log.add_wake(pcm(), 0.4, "ignore", "hey tara", 0.3, ts=ts)


def audio_of(log, event_id):
    return log.store.rows("SELECT audio FROM events WHERE id=?", (event_id,))[0]["audio"]


def on_machine(log, rel) -> bool:
    return (log.folder / rel).exists()


# Uploading, and making room.


def test_a_pass_uploads_every_recording_and_sent_file_once(log, convos, s3, now):
    _, request, conversation = a_request(log, convos, NOW)
    sent = SentItem("file", "List", file_name="list.md", file_bytes=b"# eggs", mime="text/markdown")
    convos.add_item(conversation, None, sent)
    status = backup(log, s3, now).run()
    assert status.uploaded == 3 and status.error == ""
    assert len([k for k in s3.objects if k.startswith("tars/audio/")]) == 2
    assert len([k for k in s3.objects if k.startswith("tars/files/")]) == 1
    assert s3.objects[f"tars/{request}"] == (log.folder / request).read_bytes()
    puts = s3.puts
    assert backup(log, s3, now).run().uploaded == 0 and s3.puts == puts  # nor is the snapshot due again


def test_an_old_recording_makes_room_once_the_bucket_has_it_and_training_data_stays(log, convos, s3, now):
    wake, request, _ = a_request(log, convos, NOW - 40 * DAY_S, speaker=None)
    passing = audio_of(log, a_passing_wake(log, NOW - 40 * DAY_S + 1))
    recent = audio_of(log, a_passing_wake(log, NOW - 2 * DAY_S))
    status = backup(log, s3, now).run()
    assert status.offloaded == 2 and status.only_in_bucket == 2
    assert not on_machine(log, request) and not on_machine(log, passing)
    assert on_machine(log, audio_of(log, wake))  # training learns from it
    assert on_machine(log, recent)  # newer than keep_local_days
    assert log.store.rows("SELECT utterance_audio FROM events WHERE id=?", (wake,))[0]["utterance_audio"] == request


def test_each_voices_newest_requests_stay_for_its_voiceprint(log, convos, s3, now):
    voices = {"Stacey": log.new_cluster("Stacey", PERSON), "unnamed": log.new_cluster(), "TV": log.new_cluster()}
    log.rename_cluster(voices["TV"], "TV", NOT_PERSON)
    requests: dict[str, list[str]] = {name: [] for name in voices}
    for name, cluster in voices.items():
        for i in range(VOICEPRINT_REQUESTS + 2):
            wake, request, _ = a_request(log, convos, NOW - 40 * DAY_S + i, speaker=None)
            log.assign(wake, cluster, pinned=True)
            requests[name].append(request)
    backup(log, s3, now).run()
    for name in ("Stacey", "unnamed"):  # an unnamed voice may be named later, and needs them then
        assert not on_machine(log, requests[name][0]) and not on_machine(log, requests[name][1])  # the two oldest
        assert all(on_machine(log, r) for r in requests[name][2:])
    assert not any(on_machine(log, r) for r in requests["TV"])  # not a person: no voiceprint to build


def test_nothing_makes_room_unless_the_bucket_has_it_whole(log, convos, s3, now):
    _, request, _ = a_request(log, convos, NOW - 40 * DAY_S, speaker=None)
    job = backup(log, s3, now, keep_local_days=0)
    job.run()  # uploaded, nothing made room
    s3.objects[f"tars/{request}"] = b"short"
    job = backup(log, s3, now)
    status = job.run()
    assert on_machine(log, request) and any("wasn't whole" in n for n in status.notes)
    assert job.run().uploaded == 1  # and it's uploaded again


def test_keep_local_days_0_keeps_everything_on_the_machine(log, convos, s3, now):
    _, request, _ = a_request(log, convos, NOW - 400 * DAY_S, speaker=None)
    assert backup(log, s3, now, keep_local_days=0).run().offloaded == 0 and on_machine(log, request)


def test_a_wake_labeled_after_it_made_room_comes_back_for_training(log, s3, now):
    passing = a_passing_wake(log, NOW - 40 * DAY_S)
    audio = audio_of(log, passing)
    job = backup(log, s3, now)
    job.run()
    assert not on_machine(log, audio)
    log.set_label(passing, "real")  # in Review, weeks later: training learns from it now
    assert job.run().brought_back == 1 and on_machine(log, audio)


def test_a_file_two_rows_name_stays_as_long_as_either_needs_it(log, convos, s3, now):
    wake, _, _ = a_request(log, convos, NOW - 40 * DAY_S)
    shared = audio_of(log, wake)
    log.store.write("UPDATE events SET audio=? WHERE id=?", (shared, a_passing_wake(log, NOW - 40 * DAY_S + 1)))
    backup(log, s3, now).run()
    assert on_machine(log, shared)


# Deleting.


def test_what_tars_stops_keeping_leaves_the_bucket_too(log, convos, s3, now):
    a_request(log, convos, NOW)
    passing = a_passing_wake(log, NOW + 1)
    gone = f"tars/{audio_of(log, passing)}"
    job = backup(log, s3, now)
    job.run()
    assert gone in s3.objects
    log.delete(passing)  # in the web UI's Review
    assert job.run().deleted == 1 and gone not in s3.objects
    assert len([k for k in s3.objects if k.startswith("tars/audio/")]) == 2  # the request's two recordings stay


def test_a_pass_never_deletes_most_of_the_bucket_unless_told_to(log, s3, now):
    for i in range(30):
        a_passing_wake(log, NOW - i)
    job = backup(log, s3, now)
    job.run()
    log.store.write("DELETE FROM events")  # as if the database had lost them
    status = job.run()
    assert "looks like a mistake" in status.error and "--delete-anyway" in status.error
    assert len([k for k in s3.objects if k.startswith("tars/audio/")]) == 30 and status.deleted == 0
    assert job.run(delete_anyway=True).deleted == 30 and not any(k.startswith("tars/audio/") for k in s3.objects)


# Snapshots, and a new machine.


def test_a_snapshot_every_six_hours_and_the_old_ones_go(log, s3, now):
    job = backup(log, s3, now, keep_snapshots_days=3)
    assert job.run().last_snapshot == NOW
    now[0] += 3600
    job.run()
    assert len(snapshot_keys(s3)) == 1  # not due again yet
    now[0] += 5 * 3600
    job.run()
    assert len(snapshot_keys(s3)) == 2
    for _ in range(5):
        now[0] += DAY_S
        job.run()
    assert len(snapshot_keys(s3)) == 4  # today's, and the three days before


def test_the_newest_snapshot_stays_however_old_it_is(log, s3, now):
    job = backup(log, s3, now, keep_snapshots_days=3)
    job.run()
    s3.objects["tars/snapshots/notes.txt"] = b"someone's own"
    now[0] += 40 * DAY_S  # the machine was off: the snapshot it took is the only one to restore
    s3.down = True
    job.run()  # the snapshot fails, and nothing else is deleted either
    s3.down = False
    assert len(snapshot_keys(s3)) == 2
    job.run()
    assert len(snapshot_keys(s3)) == 2  # the new one, and what isn't a snapshot
    assert "tars/snapshots/notes.txt" in s3.objects


def snapshot_of(job):
    with tarfile.open(fileobj=io.BytesIO(job.snapshot_bytes()), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def test_a_new_machine_gets_the_latest_snapshot_back(log, convos, s3, now, tmp_path):
    a_request(log, convos, NOW)
    (tmp_path / "voiceprints.npz").write_bytes(b"voices")
    job = backup(log, s3, now, {"voiceprints.npz": tmp_path / "voiceprints.npz"})
    job.run()
    s3.page = 1  # the latest is found across listings too
    now[0] += 2 * DAY_S
    job.run()
    new = tmp_path / "new-pi"
    latest = restore_snapshot(S3(s3, "tars-home"), config(), places(new))
    assert latest == snapshot_keys(s3)[-1]
    assert (new / "voice_data/voiceprints.npz").read_bytes() == b"voices"
    with sqlite3.connect(new / "voice_data/events/events.db") as db:
        assert db.execute("SELECT count(*) FROM conversations").fetchone() == (1,)
    assert not [p for p in new.rglob("*") if p.name.endswith(".restoring")]


def test_a_restored_machines_first_pass_brings_back_what_must_be_on_it(log, convos, s3, now, tmp_path):
    wake, request, conversation = a_request(log, convos, NOW, speaker=None)
    convos.add_item(conversation, None, SentItem("file", "List", file_name="list.md", file_bytes=b"# eggs"))
    sent = log.store.rows("SELECT file FROM items")[0]["file"]
    backup(log, s3, now).run()  # all of it uploaded, and still on the old machine: the snapshot says so
    new = tmp_path / "new-pi"
    restore_snapshot(S3(s3, "tars-home"), config(), places(new))
    restored = EventLog(new / "voice_data/events")
    status = backup(restored, s3, now).run()
    assert status.error == "" and status.brought_back == 2
    assert on_machine(restored, audio_of(restored, wake))  # training learns from it
    assert (restored.folder / sent).read_bytes() == b"# eggs"  # the web UI hands it out from the machine
    assert not on_machine(restored, request) and status.only_in_bucket == 1  # nothing reads it here: it stays there
    assert any("weren't on this machine" in n for n in status.notes)
    assert backup(restored, s3, now).read(request) == (log.folder / request).read_bytes()  # the web UI plays it
    assert backup(restored, s3, now).run().brought_back == 0  # once


def test_a_new_machine_never_writes_over_the_backup_and_carries_it_on_once_restored(log, convos, s3, now, tmp_path):
    a_request(log, convos, NOW)
    backup(log, s3, now).run()
    bucket = dict(s3.objects)
    new = tmp_path / "new-pi"
    fresh = EventLog(new / "voice_data/events")  # installed, and it heard the TV once before anyone restored
    a_passing_wake(fresh, NOW + DAY_S)
    now[0] += 2 * DAY_S
    status = backup(fresh, s3, now).run()
    assert status.error.startswith("tars/ in the bucket is the backup of another database")
    assert s3.objects == bucket  # nothing written: no snapshot of its own to be restored by mistake
    with pytest.raises(ValueError, match="has a history of its own"):
        restore_snapshot(S3(s3, "tars-home"), config(), places(new))
    (new / "voice_data/events/events.db").rename(new / "voice_data/events/new.db")  # moved away, as it says
    restore_snapshot(S3(s3, "tars-home"), config(), places(new))
    restored = EventLog(new / "voice_data/events")
    assert len(restored.store.rows("SELECT id FROM conversations")) == 1
    assert backup(restored, s3, now).run().error == ""  # the same database: the backup carries on


def test_a_bucket_thats_another_databases_is_never_written_to(log, s3, now):
    s3.objects[f"tars/{OWNER}"] = json.dumps({"database": "someone-else", "host": "oldpi", "since": NOW}).encode()
    a_passing_wake(log, NOW)
    status = backup(log, s3, now).run()
    assert "another database (on oldpi" in status.error and "--restore-backup" in status.error
    assert list(s3.objects) == [f"tars/{OWNER}"] and s3.lifecycle_puts == 0
    with pytest.raises(BackupError) as e:
        backup(log, s3, now)._claim()
    assert e.value.code == NOT_OURS


def test_a_new_machines_empty_database_is_restored_over(log, convos, s3, now, tmp_path):
    a_request(log, convos, NOW)
    backup(log, s3, now).run()
    new = tmp_path / "new-pi"
    EventLog(new / "voice_data/events")  # TARS was started once: an empty database, closed
    restore_snapshot(S3(s3, "tars-home"), config(), places(new))
    with sqlite3.connect(new / "voice_data/events/events.db") as db:
        assert db.execute("SELECT count(*) FROM events").fetchone() == (1,)


def test_a_restore_never_writes_over_a_history_or_an_open_database(log, convos, s3, now, tmp_path):
    a_request(log, convos, NOW)
    backup(log, s3, now).run()
    stopped = tmp_path / "stopped"
    (stopped / "voice_data/events").mkdir(parents=True)
    log.store.connect().execute(f"VACUUM INTO '{stopped / 'voice_data/events/events.db'}'")
    for _ in range(2):  # looking leaves nothing behind that would read as open the second time
        with pytest.raises(ValueError, match="has a history of its own"):
            restore_snapshot(S3(s3, "tars-home"), config(), places(stopped))
    new = tmp_path / "new-pi"
    (new / "voice_data/events").mkdir(parents=True)
    (new / "voice_data/events/events.db-wal").write_bytes(b"")
    with pytest.raises(ValueError, match="is open"):
        restore_snapshot(S3(s3, "tars-home"), config(), places(new))


def test_a_restore_waits_for_a_pass_or_a_fetch(log, convos, s3, now, tmp_path):
    a_request(log, convos, NOW)
    backup(log, s3, now).run()
    new = tmp_path / "new-pi"
    with exclusive(new / "voice_data/events"), pytest.raises(Busy):
        restore_snapshot(S3(s3, "tars-home"), config(), places(new))


def test_a_restore_that_fails_partway_changes_nothing(log, convos, s3, now, tmp_path, monkeypatch):
    import shutil

    a_request(log, convos, NOW)
    (tmp_path / "voiceprints.npz").write_bytes(b"voices")
    backup(log, s3, now, {"voiceprints.npz": tmp_path / "voiceprints.npz"}).run()
    new = tmp_path / "new-pi"
    copies = []

    copyfile = shutil.copyfile

    def disk_full(source, target, **kw):
        if copies:
            raise OSError(28, "No space left on device")
        copies.append(target)
        return copyfile(source, target, **kw)

    monkeypatch.setattr(shutil, "copyfile", disk_full)
    with pytest.raises(OSError, match="No space"):
        restore_snapshot(S3(s3, "tars-home"), config(), places(new))
    assert copies and {p.name for p in new.rglob("*") if p.is_file()} == {"backup.lock"}


def test_tars_snapshot_holds_the_database_voiceprints_its_wake_models_and_local_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "make_s3", lambda cfg, root: S3(PretendS3(), "tars-home"))
    cfg = dataclasses.replace(load_config(REPO_CONFIG, local=False), backup=config())
    (tmp_path / "voice_data").mkdir()
    (tmp_path / "voice_data/voiceprints.npz").write_bytes(b"voices")
    (tmp_path / "models/personal").mkdir(parents=True)
    (tmp_path / "models/personal/hey_tars.tflite").write_bytes(b"model")
    (tmp_path / "config.local.toml").write_text('[audio]\ninput_device = "PowerConf"\n')
    (tmp_path / ".env").write_text("OPENAI_API_KEY=secret\n")
    names = set(snapshot_of(cli.make_backup(cfg, tmp_path)))
    assert {SNAPSHOT_DB, "voiceprints.npz", "models/personal/hey_tars.tflite", "config.local.toml"} <= names
    assert ".env" not in names  # the keys never leave the machine


def house(root: Path, module: str, source: str) -> Config:
    """The repo's config with the backup on and one plugin, `module`, written into the project at `root`."""
    (root / f"{module}.py").write_text(source + "\n\ndef setup(settings, context):\n    return {}\n")
    cfg = load_config(REPO_CONFIG, local=False)
    return dataclasses.replace(
        cfg, backup=config(), home=dataclasses.replace(cfg.home, plugins={"mine": {"module": module}})
    )


def test_the_snapshot_keeps_what_the_house_learned_and_a_restore_puts_it_back(tmp_path, monkeypatch):
    pretend = S3(PretendS3(), "tars-home")
    monkeypatch.setattr(cli, "make_s3", lambda cfg, root: pretend)
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    cfg = house(old, "learned_house", 'BACKUP_FILES = ("voice_data/ac", "voice_data/ir/tv.json")\n')
    (old / "voice_data/ac").mkdir(parents=True)
    (old / "voice_data/ac/profile.json").write_text('{"modes": ["cool"]}')
    (old / "voice_data/ir").mkdir()
    (old / "voice_data/ir/tv.json").write_text('{"power": 2}')
    (old / "voice_data/ir/scratch.json").write_text("not listed")
    (old / "voice_data/androidtv").mkdir()
    (old / "voice_data/androidtv/adbkey").write_text("key")
    (old / "voice_data/found.json").write_text('{"aa:bb": "192.168.1.9"}')
    cli.make_backup(cfg, old).run()

    new.mkdir()
    restore_snapshot(
        pretend, cfg.backup, {SNAPSHOT_DB: new / "voice_data/events/events.db", **cli.snapshot_files(cfg, new)}
    )
    assert (new / "voice_data/ac/profile.json").read_text() == '{"modes": ["cool"]}'
    assert (new / "voice_data/ir/tv.json").read_text() == '{"power": 2}'
    assert not (new / "voice_data/ir/scratch.json").exists()  # only what the plugin lists
    assert (new / "voice_data/androidtv/adbkey").read_text() == "key"  # the TV box needn't be approved again
    assert (new / "voice_data/found.json").exists()


@pytest.mark.parametrize(
    ("source", "said"),
    [("raise ImportError('no tinytuya')\n", "didn't load"), ('BACKUP_FILES = ("../elsewhere", "/etc")\n', "outside")],
)
def test_a_plugin_that_wont_load_or_reaches_outside_costs_only_its_own_files(
    tmp_path, monkeypatch, capsys, source, said
):
    monkeypatch.setattr(cli, "make_s3", lambda cfg, root: S3(PretendS3(), "tars-home"))
    cfg = house(tmp_path, f"odd_house_{len(said)}", source)
    (tmp_path / "config.local.toml").write_text("")
    names = set(snapshot_of(cli.make_backup(cfg, tmp_path)))
    assert {SNAPSHOT_DB, "config.local.toml"} <= names
    assert not any("elsewhere" in n or n.startswith("etc") for n in names)
    assert said in capsys.readouterr().out


def test_my_house_lists_what_it_learned():
    from examples import my_house

    assert {Path(p).as_posix() for p in my_house.BACKUP_FILES} == {"voice_data/ac", "voice_data/ir"}


@pytest.mark.parametrize(
    ("unit", "state"), [("voice-assistant", "active"), ("voice-assistant-backup.service", "activating")]
)
def test_a_restore_waits_for_tars_to_be_stopped(tmp_path, monkeypatch, unit, state):
    from voice_assistant import check

    monkeypatch.setattr(check, "systemd_state", lambda u: state if u == unit else "inactive")
    cfg = dataclasses.replace(load_config(REPO_CONFIG, local=False), backup=config())
    with pytest.raises(SystemExit, match=f"systemctl --user stop {unit}"):
        cli.restore_backup(cfg, tmp_path)


# The lifecycle rules.


def test_the_lifecycle_rule_follows_backup_and_leaves_others_alone(log, s3, now):
    backup(log, s3, now, archive_after_days=90).run()
    rules = {r["ID"]: r for r in s3.rules}
    assert set(rules) == {"someone-elses", ARCHIVE_RULE}
    assert rules[ARCHIVE_RULE]["Transitions"] == [{"Days": 90, "StorageClass": "DEEP_ARCHIVE"}]
    assert rules[ARCHIVE_RULE]["Filter"] == {"Prefix": "tars/audio/"}
    puts = s3.lifecycle_puts
    backup(log, s3, now, archive_after_days=90).run()
    assert s3.lifecycle_puts == puts  # already as wanted: left alone
    backup(log, s3, now, archive_after_days=0).run()
    assert {r["ID"] for r in s3.rules} == {"someone-elses"}


def test_archiving_is_off_unless_asked_for(log, s3, now):
    backup(log, s3, now).run()
    assert {r["ID"] for r in s3.rules} == {"someone-elses"}
    assert BackupConfig().archive_after_days == 0
    assert load_config(REPO_CONFIG, local=False).backup.archive_after_days == 0


def test_no_archiving_and_no_other_rules_leaves_the_bucket_without_any(log, s3, now):
    s3.rules = []
    backup(log, s3, now, archive_after_days=180).run()
    assert [r["ID"] for r in s3.rules] == [ARCHIVE_RULE]
    backup(log, s3, now, archive_after_days=0).run()
    assert s3.rules == []


def test_a_key_that_may_not_set_lifecycle_rules_leaves_a_note_and_the_backup_goes_on(log, convos, s3, now):
    a_request(log, convos, NOW)
    s3.lifecycle_allowed = False
    status = backup(log, s3, now, archive_after_days=180).run()
    assert status.error == "" and status.uploaded == 2 and not status.lifecycle_ok
    assert any("set them by hand" in n for n in status.notes)


def test_the_lifecycle_rules_not_answering_is_a_failure_not_a_permission_note(log, s3, now):
    s3.lifecycle_down = True
    status = backup(log, s3, now).run()
    assert status.error.startswith("the lifecycle rules") and not any("by hand" in n for n in status.notes)


# Failing, and two at once.


def test_a_step_that_fails_doesnt_stop_the_others_and_is_kept(log, convos, s3, now):
    a_request(log, convos, NOW)
    s3.down = True
    job = backup(log, s3, now)
    status = job.run()
    assert status.error.startswith("S3 get_object") and status.last_ok == 0  # nothing goes without the bucket
    assert Status.load(job.status_path).error == status.error
    s3.down = False
    assert job.run().last_ok == NOW


def test_two_passes_never_run_at_once(log, s3, now):
    job = backup(log, s3, now)
    with exclusive(log.folder), pytest.raises(Busy):
        job.run()


# Bringing files back.


def test_fetching_brings_back_everything_the_machine_lacks_and_asks_for_archived_audio(log, convos, s3, now):
    _, request, _ = a_request(log, convos, NOW - 40 * DAY_S, speaker=None)
    passing = audio_of(log, a_passing_wake(log, NOW - 40 * DAY_S + 1))
    recent = audio_of(log, a_passing_wake(log, NOW))
    job = backup(log, s3, now)
    job.run()
    (log.folder / recent).unlink()  # as on a new machine: it was never made room for, just not there
    s3.archived.add(f"tars/{passing}")
    done = job.fetch()
    assert (done.fetched, done.archived, done.restoring) == (2, 1, 0)
    assert on_machine(log, request) and on_machine(log, recent) and not on_machine(log, passing)
    assert job.fetch(restore=True).restoring == 1
    assert job.fetch(restore=True).restoring == 1  # asked again while it's on its way: no error
    assert s3.restoring == [f"tars/{passing}"]


def test_fetching_never_brings_back_what_tars_no_longer_keeps(log, convos, s3, now):
    passing = a_passing_wake(log, NOW)
    gone = audio_of(log, passing)
    job = backup(log, s3, now)
    job.run()
    log.delete(passing)  # in the web UI's Review, before the next pass takes it out of the bucket
    assert job.fetch().fetched == 0 and not on_machine(log, gone)


def test_what_was_fetched_stays_on_the_machine_a_while(log, convos, s3, now):
    _, request, _ = a_request(log, convos, NOW - 40 * DAY_S, speaker=None)
    job = backup(log, s3, now)
    job.run()
    job.fetch()
    job.run()
    assert on_machine(log, request)  # not made room for again at once
    now[0] += 31 * DAY_S
    job.run()
    assert not on_machine(log, request)


def test_a_file_gone_from_the_bucket_doesnt_stop_the_fetch(log, convos, s3, now):
    _, request, _ = a_request(log, convos, NOW - 40 * DAY_S, speaker=None)
    job = backup(log, s3, now)
    job.run()
    del s3.objects[f"tars/{request}"]
    assert job.fetch().missing == 1


def test_a_file_needed_back_but_gone_from_the_bucket_is_a_note_not_a_failing_backup(log, s3, now):
    passing = a_passing_wake(log, NOW - 40 * DAY_S)
    job = backup(log, s3, now)
    job.run()
    del s3.objects[f"tars/{audio_of(log, passing)}"]
    log.set_label(passing, "real")
    status = job.run()
    assert status.error == "" and any("it's lost" in n for n in status.notes)


def test_the_web_ui_plays_a_recording_thats_only_in_the_bucket(log, convos, s3, now):
    from fastapi.testclient import TestClient

    from voice_assistant.webui import create_app

    wake, request, _ = a_request(log, convos, NOW - 40 * DAY_S, speaker=None)
    job = backup(log, s3, now)
    job.run()
    assert not on_machine(log, request)
    client = TestClient(create_app(log, bucket=job.read), base_url="http://localhost")
    played = client.get(f"/api/audio/{wake}/request")
    assert played.status_code == 200 and played.content == s3.objects[f"tars/{request}"]
    part = client.get(f"/api/audio/{wake}/request", headers={"Range": "bytes=0-1"})  # as Safari asks first
    whole = len(s3.objects[f"tars/{request}"])
    assert part.status_code == 206 and part.content == b"RI" and part.headers["content-range"] == f"bytes 0-1/{whole}"
    s3.archived.add(f"tars/{request}")
    assert client.get(f"/api/audio/{wake}/request").status_code == 404
    assert TestClient(create_app(log), base_url="http://localhost").get(f"/api/audio/{wake}/request").status_code == 404


# S3, and the keys.


def test_s3s_errors_keep_their_code():
    s3 = PretendS3()
    with pytest.raises(BackupError) as e:
        S3(s3, "tars-home").get("tars/nothing")
    assert e.value.code == "NoSuchKey"
    assert S3(s3, "tars-home").size("tars/nothing") is None


def test_the_keys_come_from_env_and_nowhere_else(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "from-the-shell")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "from-the-shell")
    cfg = dataclasses.replace(load_config(REPO_CONFIG, local=False), backup=config())
    (tmp_path / ".env").write_text("OPENAI_API_KEY=x\n")
    with pytest.raises(ConfigError, match="AWS_ACCESS_KEY_ID"):
        cli.make_s3(cfg, tmp_path)


def test_s3_is_made_for_the_buckets_region():
    pytest.importorskip("boto3")
    s3 = make_s3(config(), "AKIA", "secret")
    assert s3.bucket == "tars-home" and s3._client.meta.region_name == "eu-central-1"


def test_a_status_from_another_version_reads_as_none(tmp_path):
    (tmp_path / "backup.json").write_text(json.dumps({"last_run": 5, "something_new": 1}))
    assert Status.load(tmp_path / "backup.json").last_run == 5
    (tmp_path / "backup.json").write_text("not json")
    assert Status.load(tmp_path / "backup.json") == Status()
