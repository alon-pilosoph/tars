"""The backup to S3, against a pretend bucket: everything TARS keeps is uploaded once, old requests make room on the
machine only once the bucket has them whole, what TARS stops keeping leaves the bucket too (never all of it by
mistake), a daily snapshot can be put back on a new machine, and the bucket's lifecycle rules follow [backup]."""

import io
import json
import sqlite3

import numpy as np
import pytest

from voice_assistant import check
from voice_assistant.backup import (
    ARCHIVE_RULE,
    DAY_S,
    S3,
    SNAPSHOTS_RULE,
    Backup,
    BackupError,
    Status,
    make_s3,
    restore_snapshot,
)
from voice_assistant.config import BackupConfig, ConfigError
from voice_assistant.conversations import SentItem

NOW = 1_800_000_000.0
DB_AS = "voice_data/events/events.db"


class ClientError(Exception):
    """As botocore raises them: S3's error code and message in `response`."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message or code}}


class PretendS3:
    """boto3's S3 client, as far as a backup can tell: one bucket of objects. `archived`: keys moved to Deep Archive
    and not restored. `lifecycle_allowed`: whether the keys may change the bucket's lifecycle rules."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.archived: set[str] = set()
        self.restoring: list[str] = []
        self.rules: list[dict] = [{"ID": "someone-elses", "Filter": {"Prefix": "photos/"}, "Status": "Enabled"}]
        self.lifecycle_allowed, self.down = True, False
        self.puts = 0
        self.lifecycle_puts = 0

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

    def list_objects_v2(self, Bucket, Prefix):
        self._bucket(Bucket)
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}

    def get_bucket_lifecycle_configuration(self, Bucket):
        self._bucket(Bucket)
        if not self.rules:
            raise ClientError("NoSuchLifecycleConfiguration")
        return {"Rules": [dict(r) for r in self.rules]}

    def put_bucket_lifecycle_configuration(self, Bucket, LifecycleConfiguration):
        self._bucket(Bucket)
        if not self.lifecycle_allowed:
            raise ClientError("AccessDenied", "not allowed to s3:PutLifecycleConfiguration")
        self.rules, self.lifecycle_puts = LifecycleConfiguration["Rules"], self.lifecycle_puts + 1


def config(**kw) -> BackupConfig:
    return BackupConfig(enabled=True, bucket="tars-home", region="eu-central-1", **kw)


@pytest.fixture
def s3():
    return PretendS3()


@pytest.fixture
def now():
    return [NOW]


def backup(log, s3, now, tmp_path=None, **kw) -> Backup:
    extra = {"voice_data/voiceprints.npz": tmp_path / "voiceprints.npz"} if tmp_path else {}
    return Backup(log, config(**kw), S3(s3, "tars-home"), DB_AS, extra, clock=lambda: now[0])


def pcm(seconds=0.2):
    return np.zeros(int(16_000 * seconds), dtype=np.int16)


def a_request(log, convos, ts):
    """A wake that's training data (labeled real), the request after it, and the conversation it started."""
    wake = log.add_wake(pcm(), 0.9, "answer", "hey tars", 0.9, ts=ts)
    log.set_label(wake, "real")
    request = log.add_request(wake, pcm(), "what time is it", "alon", 0.8, None)
    conversation = convos.start(wake, ts=ts)
    convos.add_person_turn(conversation, "what time is it", audio=request, ts=ts)
    return wake, request, conversation


def a_passing_wake(log, ts):
    """A wake the check turned away, nobody answered in Review: not training data."""
    return log.add_wake(pcm(), 0.4, "ignore", "hey tara", 0.3, ts=ts)


def files_of(log, event_id):
    return log.store.rows("SELECT audio, utterance_audio FROM events WHERE id=?", (event_id,))[0]


def test_a_pass_uploads_every_recording_and_sent_file_once(log, convos, s3, now):
    _, request, conversation = a_request(log, convos, NOW)
    convos.add_item(
        conversation, None, SentItem("file", "List", file_name="list.md", file_bytes=b"# eggs", mime="text/markdown")
    )
    status = backup(log, s3, now).run()
    keys = sorted(s3.objects)
    assert status.uploaded == 3 and status.error == ""
    assert keys[0].startswith("tars/audio/") and any(k.startswith("tars/files/") for k in keys)
    assert s3.objects[f"tars/{request}"] == (log.folder / request).read_bytes()
    puts = s3.puts
    assert backup(log, s3, now).run().uploaded == 0 and s3.puts == puts  # the snapshot isn't due again either


def test_an_old_request_makes_room_once_the_bucket_has_it_and_training_data_stays(log, convos, s3, now):
    wake, request, _ = a_request(log, convos, NOW - 40 * DAY_S)
    passing = a_passing_wake(log, NOW - 40 * DAY_S + 1)
    recent = a_passing_wake(log, NOW - 2 * DAY_S)
    status = backup(log, s3, now).run()
    assert status.offloaded == 2 and status.only_in_bucket == 2
    assert not (log.folder / request).exists() and not (log.folder / files_of(log, passing)["audio"]).exists()
    assert (log.folder / files_of(log, wake)["audio"]).exists()  # training learns from it: it stays
    assert (log.folder / files_of(log, recent)["audio"]).exists()  # newer than keep_local_days
    assert files_of(log, wake)["utterance_audio"] == request  # the rows still name it: it's in the bucket


def test_nothing_makes_room_unless_the_bucket_has_it_whole(log, convos, s3, now):
    _, request, _ = a_request(log, convos, NOW - 40 * DAY_S)
    job = backup(log, s3, now)
    job.run()
    (log.folder / request).write_bytes(b"x")  # pretend it was cut short: put back, and the bucket's copy is wrong
    log.store.write("UPDATE remote_files SET offloaded=NULL")
    s3.objects[f"tars/{request}"] = b"short"
    status = job.run()
    assert (log.folder / request).exists() and any("wasn't whole" in n for n in status.notes)
    assert job.run().uploaded == 1  # and it's uploaded again


def test_keep_local_days_0_keeps_everything_on_the_machine(log, convos, s3, now):
    _, request, _ = a_request(log, convos, NOW - 400 * DAY_S)
    assert backup(log, s3, now, keep_local_days=0).run().offloaded == 0 and (log.folder / request).exists()


def test_what_tars_stops_keeping_leaves_the_bucket_too(log, convos, s3, now):
    a_request(log, convos, NOW)
    passing = a_passing_wake(log, NOW + 1)
    gone = f"tars/{files_of(log, passing)['audio']}"
    job = backup(log, s3, now)
    job.run()
    assert gone in s3.objects
    log.delete(passing)  # in the web UI's Review
    assert job.run().deleted == 1 and gone not in s3.objects
    assert len([k for k in s3.objects if k.startswith("tars/audio/")]) == 2  # the request's two recordings stay


def test_a_pass_never_deletes_most_of_the_bucket_by_mistake(log, convos, s3, now):
    for i in range(30):
        a_passing_wake(log, NOW - i)
    job = backup(log, s3, now)
    job.run()
    log.store.write("DELETE FROM events")  # as if the database had lost them
    status = job.run()
    assert "looks like a mistake" in status.error and len(s3.objects) >= 30 and status.deleted == 0


def test_a_snapshot_a_day_puts_everything_back_on_a_new_machine(log, convos, s3, now, tmp_path):
    a_request(log, convos, NOW)
    (tmp_path / "voiceprints.npz").write_bytes(b"voices")
    job = backup(log, s3, now, tmp_path)
    assert job.run().last_snapshot == NOW
    now[0] += 3600
    job.run()
    now[0] += DAY_S
    job.run()
    snapshots = [k for k in s3.objects if k.startswith("tars/snapshots/")]
    assert len(snapshots) == 2 and snapshots[0].endswith(".tar.gz")
    new = tmp_path / "new-pi"
    assert restore_snapshot(S3(s3, "tars-home"), "tars", new, DB_AS) == max(snapshots)
    assert (new / "voice_data/voiceprints.npz").read_bytes() == b"voices"
    with sqlite3.connect(new / DB_AS) as db:
        assert db.execute("SELECT count(*) FROM conversations").fetchone() == (1,)
    with pytest.raises(ValueError, match="is there already"):
        restore_snapshot(S3(s3, "tars-home"), "tars", new, DB_AS)


def test_the_lifecycle_rules_follow_backup_and_leave_others_alone(log, s3, now):
    backup(log, s3, now, archive_after_days=90, keep_snapshots_days=14).run()
    rules = {r["ID"]: r for r in s3.rules}
    assert set(rules) == {"someone-elses", ARCHIVE_RULE, SNAPSHOTS_RULE}
    assert rules[ARCHIVE_RULE]["Transitions"] == [{"Days": 90, "StorageClass": "DEEP_ARCHIVE"}]
    assert rules[ARCHIVE_RULE]["Filter"] == {"Prefix": "tars/audio/"}
    assert rules[SNAPSHOTS_RULE]["Expiration"] == {"Days": 14}
    puts = s3.lifecycle_puts
    backup(log, s3, now, archive_after_days=90, keep_snapshots_days=14).run()
    assert s3.lifecycle_puts == puts  # already as wanted: left alone
    backup(log, s3, now, archive_after_days=0, keep_snapshots_days=14).run()
    assert {r["ID"] for r in s3.rules} == {"someone-elses", SNAPSHOTS_RULE}


def test_keys_that_may_not_set_lifecycle_rules_leave_a_note_and_the_backup_goes_on(log, convos, s3, now):
    a_request(log, convos, NOW)
    s3.lifecycle_allowed = False
    status = backup(log, s3, now).run()
    assert status.error == "" and status.uploaded == 2 and status.lifecycle.startswith("not set")
    assert any("set them by hand" in n for n in status.notes)


def test_a_step_that_fails_doesnt_stop_the_others_and_is_kept(log, convos, s3, now):
    a_request(log, convos, NOW)
    s3.down = True
    job = backup(log, s3, now)
    status = job.run()
    assert status.error.startswith("uploading: S3 put_object") and status.last_ok == 0
    assert Status.load(job.status_path).error == status.error
    s3.down = False
    assert job.run().last_ok == NOW


def test_what_made_room_comes_back_and_archived_audio_is_asked_to_be_restored(log, convos, s3, now):
    _, request, _ = a_request(log, convos, NOW - 40 * DAY_S)
    passing = files_of(log, a_passing_wake(log, NOW - 40 * DAY_S + 1))["audio"]
    job = backup(log, s3, now)
    job.run()
    s3.archived.add(f"tars/{passing}")
    assert job.fetch() == {"fetched": 1, "archived": 1, "restoring": 0}
    assert (log.folder / request).exists() and not (log.folder / passing).exists()
    assert job.fetch(restore=True) == {"fetched": 0, "archived": 0, "restoring": 1}
    assert job.fetch(restore=True)["restoring"] == 1  # asked again while it's on its way: no error
    assert s3.restoring == [f"tars/{passing}"]


def test_the_web_ui_plays_a_recording_thats_only_in_the_bucket(log, convos, s3, now):
    from fastapi.testclient import TestClient

    from voice_assistant.webui import create_app

    wake, request, _ = a_request(log, convos, NOW - 40 * DAY_S)
    job = backup(log, s3, now)
    job.run()
    assert not (log.folder / request).exists()
    client = TestClient(create_app(log, bucket=job.read), base_url="http://localhost")
    played = client.get(f"/api/audio/{wake}/request")
    assert played.status_code == 200 and played.content == s3.objects[f"tars/{request}"]
    s3.archived.add(f"tars/{request}")
    assert client.get(f"/api/audio/{wake}/request").status_code == 404
    assert TestClient(create_app(log), base_url="http://localhost").get(f"/api/audio/{wake}/request").status_code == 404


def test_a_wake_labeled_after_it_made_room_comes_back_for_training(log, convos, s3, now):
    passing = a_passing_wake(log, NOW - 40 * DAY_S)
    audio = files_of(log, passing)["audio"]
    job = backup(log, s3, now)
    job.run()
    assert not (log.folder / audio).exists()
    log.set_label(passing, "real")  # in Review, weeks later: training learns from it now
    assert job.run().brought_back == 1 and (log.folder / audio).exists()


def test_a_file_two_rows_name_stays_as_long_as_either_needs_it(log, convos, s3, now):
    wake, _, _ = a_request(log, convos, NOW - 40 * DAY_S)
    shared = files_of(log, wake)["audio"]
    log.store.write("UPDATE events SET audio=? WHERE id=?", (shared, a_passing_wake(log, NOW - 40 * DAY_S + 1)))
    backup(log, s3, now).run()
    assert (log.folder / shared).exists()


def test_s3s_errors_keep_their_code():
    s3 = PretendS3()
    with pytest.raises(BackupError) as e:
        S3(s3, "tars-home").get("tars/nothing")
    assert e.value.code == "NoSuchKey"
    assert S3(s3, "tars-home").size("tars/nothing") is None


def test_the_keys_come_from_env_only(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "from-the-shell")
    with pytest.raises(ValueError, match="no AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY in .env"):
        make_s3(config(), lambda name: None)


def test_check_says_how_the_last_pass_went(log, convos, s3, now):
    import dataclasses
    from pathlib import Path

    from voice_assistant.config import load_config

    cfg = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    off = check.backup(cfg, lambda: pytest.fail("not made"))
    assert off[0].status == check.SKIP
    on = dataclasses.replace(cfg, backup=config())
    job = backup(log, s3, now)
    (never,) = check.backup(on, lambda: job, now=NOW)
    assert never.status == check.WARN and "no pass has run yet" in never.detail
    job.run()
    (fine,) = check.backup(on, lambda: job, now=NOW + 60)
    assert fine.status == check.OK and "s3://tars-home/tars" in fine.detail
    (late,) = check.backup(on, lambda: job, now=NOW + 3 * 3600)
    assert late.status == check.WARN
    s3.down = True
    (down,) = check.backup(on, lambda: job, now=NOW)
    assert down.status == check.FAIL

    def refused():
        raise ConfigError("The backup: no AWS_ACCESS_KEY_ID in .env")

    assert check.backup(on, refused)[0].status == check.FAIL


@pytest.mark.parametrize(
    "setting, says",
    [
        ("[backup]\nenabled = true", "[backup] bucket must name"),
        ('[backup]\nenabled = true\nbucket = "tars-home"', "[backup] region must say"),
        ('[backup]\nbucket = "Tars_Home"', "must be an S3 bucket name"),
        ('[backup]\nregion = "europe"', "must be an AWS region"),
        ('[backup]\nprefix = "/tars/"', "no slash at either end"),
        ("[backup]\nkeep_snapshots_days = 0", "at least 1"),
    ],
)
def test_a_backup_setting_that_cannot_work_stops_at_startup(tmp_path, setting, says):
    from voice_assistant.config import load_config

    (tmp_path / "config.toml").write_text(setting)
    with pytest.raises(ConfigError, match=says.replace("[", "\\[")):
        load_config(tmp_path / "config.toml", local=False)


def test_a_status_from_another_version_reads_as_none(tmp_path):
    (tmp_path / "backup.json").write_text(json.dumps({"last_run": 5, "something_new": 1}))
    assert Status.load(tmp_path / "backup.json").last_run == 5
    (tmp_path / "backup.json").write_text("not json")
    assert Status.load(tmp_path / "backup.json") == Status()


def test_s3_is_made_with_the_keys_from_env_for_the_buckets_region():
    pytest.importorskip("boto3")
    keys = {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "secret"}
    s3 = make_s3(config(), keys.get)
    assert s3.bucket == "tars-home" and s3._client.meta.region_name == "eu-central-1"


def test_tars_snapshot_holds_the_database_voiceprints_its_own_wake_models_and_local_settings(tmp_path, monkeypatch):
    import dataclasses
    import tarfile
    from pathlib import Path

    from voice_assistant import __main__ as cli
    from voice_assistant import backup as backup_module
    from voice_assistant.config import load_config

    monkeypatch.setattr(backup_module, "make_s3", lambda cfg, secret: S3(PretendS3(), "tars-home"))
    cfg = dataclasses.replace(load_config(Path(__file__).parents[1] / "config.toml", local=False), backup=config())
    (tmp_path / "voice_data").mkdir()
    (tmp_path / "voice_data/voiceprints.npz").write_bytes(b"voices")
    (tmp_path / "models/personal").mkdir(parents=True)
    (tmp_path / "models/personal/hey_tars.tflite").write_bytes(b"model")
    (tmp_path / "config.local.toml").write_text('[audio]\ninput_device = "PowerConf"\n')
    (tmp_path / ".env").write_text("OPENAI_API_KEY=secret\n")
    job = cli.make_backup(cfg, tmp_path)
    with tarfile.open(fileobj=io.BytesIO(job.snapshot_bytes()), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert {DB_AS, "voice_data/voiceprints.npz", "models/personal/hey_tars.tflite", "config.local.toml"} <= names
    assert ".env" not in names  # the keys never leave the machine
