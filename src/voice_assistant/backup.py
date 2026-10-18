"""Backing up what TARS keeps to an Amazon S3 bucket, and keeping the machine's card from filling with it.

`voice-assistant --backup` runs one pass, every 15 minutes from its systemd timer (docs/backup.md):

1. Every recording and every file TARS sent that isn't in the bucket yet is uploaded, under [backup] prefix.
2. A request's audio older than [backup] keep_local_days is removed from the machine, once the bucket is checked to
   have it whole, so the card holds only the recent weeks. Audio training learns from, and what TARS sent, stay on
   the machine too: training reads them from there, and the web UI hands sent files out.
3. Whatever TARS no longer keeps (a conversation deleted in the web UI, old audio dropped by [learning]
   keep_audio_days) is deleted from the bucket too: the bucket holds what TARS keeps, no more.
4. Once a day, a snapshot: the database (through sqlite's backup API, so it's consistent while TARS writes), the
   voiceprints and the trained wake models, as one .tar.gz.
5. The bucket's lifecycle rules, kept in step with [backup], move the audio to Glacier Deep Archive after
   archive_after_days, and delete snapshots after keep_snapshots_days.

What's in the bucket is kept in the database (remote_files), so a pass looks only at what's new. The web UI plays a
recording that's no longer on the machine straight from the bucket, until it's archived. `--fetch-audio` brings
recordings back (asking S3 to restore archived ones first), for training or a new machine, and `--restore-backup`
puts the latest snapshot in place on a new machine.
"""

import io
import json
import sqlite3
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from operator import itemgetter
from pathlib import Path

from .config import BackupConfig
from .events import EventLog, learning_label
from .files import atomic_write

KEY_ID, SECRET = "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"
DAY_S = 24 * 3600
SNAPSHOT_EVERY_S = DAY_S
STATUS_FILE = "backup.json"  # in the event log's folder: how the last pass went, for --check
DEEP_ARCHIVE = "DEEP_ARCHIVE"
# The bucket's lifecycle rules this keeps in step with [backup], by their ids; any other rule there is left alone.
ARCHIVE_RULE, SNAPSHOTS_RULE = "tars-audio-to-deep-archive", "tars-snapshots-expire"
DELETE_BATCH = 1000  # S3 deletes at most this many objects a request
# A pass that would delete more than this share of the bucket, and more than a few files, stops instead: a database
# that suddenly keeps nothing is far likelier a mistake than a household deleting its whole history.
MAX_DELETE_SHARE, MAX_DELETE_ANYWAY = 0.5, 20
# Asking S3 to bring an archived recording back: the bulk tier (up to 48 hours, the cheapest), for this many days.
RESTORE_TIER, RESTORE_DAYS = "Bulk", 7


class BackupError(Exception):
    """S3 refused or didn't answer: `code` is S3's error code ("AccessDenied", "NoSuchKey") when it gave one."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class S3:
    """The few calls of S3's API a backup needs, on one bucket. Every failure is a BackupError."""

    def __init__(self, client, bucket: str):
        """`client`: boto3's S3 client (make_s3), or anything with the same methods."""
        self._client, self.bucket = client, bucket

    def _call(self, method: str, **kw):
        try:
            return getattr(self._client, method)(Bucket=self.bucket, **kw)
        except Exception as e:  # botocore's errors, and the network's, all become one
            error = (getattr(e, "response", None) or {}).get("Error", {})
            raise BackupError(f"S3 {method}: {error.get('Message') or e}", str(error.get("Code", ""))) from e

    def check(self) -> None:
        """The bucket is there, and the keys may use it."""
        self._call("head_bucket")

    def put(self, key: str, data: bytes) -> None:
        self._call("put_object", Key=key, Body=data, ServerSideEncryption="AES256")

    def size(self, key: str) -> int | None:
        """The object's size, or None if there's none."""
        try:
            return int(self._call("head_object", Key=key)["ContentLength"])
        except BackupError as e:
            if e.code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def get(self, key: str) -> bytes:
        """BackupError with code InvalidObjectState for one that's archived and not restored."""
        return self._call("get_object", Key=key)["Body"].read()

    def restore(self, key: str) -> None:
        """Asks S3 to bring an archived object back for a while; one already on its way is no error."""
        try:
            self._call("restore_object", Key=key, RestoreRequest={"Days": RESTORE_DAYS, "Tier": RESTORE_TIER})
        except BackupError as e:
            if e.code != "RestoreAlreadyInProgress":
                raise

    def delete(self, keys: list[str]) -> None:
        for i in range(0, len(keys), DELETE_BATCH):
            batch = [{"Key": k} for k in keys[i : i + DELETE_BATCH]]
            done = self._call("delete_objects", Delete={"Objects": batch, "Quiet": True})
            if errors := done.get("Errors"):
                raise BackupError(f"S3 delete_objects: {errors[0].get('Key')}: {errors[0].get('Message')}")

    def latest(self, prefix: str) -> str | None:
        """The last key under `prefix`, by name: snapshots are named by when they were made, and expire after
        [backup] keep_snapshots_days, so there are far fewer than one listing's thousand."""
        found = self._call("list_objects_v2", Prefix=prefix).get("Contents") or []
        return max((o["Key"] for o in found), default=None)

    def lifecycle(self) -> list[dict]:
        try:
            return self._call("get_bucket_lifecycle_configuration").get("Rules", [])
        except BackupError as e:
            if e.code == "NoSuchLifecycleConfiguration":
                return []
            raise

    def set_lifecycle(self, rules: list[dict]) -> None:
        self._call("put_bucket_lifecycle_configuration", LifecycleConfiguration={"Rules": rules})


def make_s3(cfg: BackupConfig, secret: Callable[[str], str | None]) -> S3:
    """S3 with the keys in .env (`secret`), never the shell's or ~/.aws: TARS uses only what's set for it. ValueError
    if a key or boto3 is missing."""
    missing = [name for name in (KEY_ID, SECRET) if not secret(name)]
    if missing:
        raise ValueError(f"no {' or '.join(missing)} in .env (docs/backup.md)")
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError:
        raise ValueError('it needs the optional extra "backup": uv sync --all-extras') from None
    client = boto3.session.Session().client(
        "s3",
        region_name=cfg.region,
        aws_access_key_id=secret(KEY_ID),
        aws_secret_access_key=secret(SECRET),
        config=BotoConfig(connect_timeout=10, read_timeout=60, retries={"max_attempts": 5, "mode": "standard"}),
    )
    return S3(client, cfg.bucket)


@dataclass
class Kept:
    """A file TARS keeps, as the backup sees it: when it was made, and whether it must stay on the machine."""

    ts: float
    stays: bool


@dataclass
class Status:
    """How the last pass went, kept in STATUS_FILE."""

    last_run: float = 0.0
    last_ok: float = 0.0
    error: str = ""
    uploaded: int = 0  # by the last pass
    offloaded: int = 0
    brought_back: int = 0
    deleted: int = 0
    in_bucket: int = 0  # files, after it
    only_in_bucket: int = 0
    last_snapshot: float = 0.0
    lifecycle: str = ""  # what the bucket's rules came to, or why they couldn't be set
    notes: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Status:
        try:
            raw = json.loads(path.read_text())
            return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        except FileNotFoundError, ValueError, TypeError:
            return cls()

    def save(self, path: Path) -> None:
        atomic_write(path, json.dumps(asdict(self), indent=1).encode())


class Backup:
    def __init__(
        self,
        log: EventLog,
        cfg: BackupConfig,
        s3: S3,
        db_as: str,
        snapshot: dict[str, Path] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        """`db_as`: where a snapshot keeps the database, as [learning] folder puts it ("voice_data/events/events.db"),
        so restore_snapshot puts it back there. `snapshot`: what else goes in, by the path it has there, like
        {"voice_data/voiceprints.npz": ...}."""
        self._log, self._store, self._cfg, self._s3, self._clock = log, log.store, cfg, s3, clock
        self._prefix = cfg.prefix.strip("/")
        self._db_as, self._extra = db_as, snapshot or {}
        self.status_path = log.folder / STATUS_FILE

    def check(self) -> None:
        """The bucket answers, and the keys may use it: BackupError if not."""
        self._s3.check()

    def key(self, rel: str) -> str:
        """Where a file in the event log's folder is in the bucket."""
        return f"{self._prefix}/{rel}"

    @property
    def snapshots(self) -> str:
        return f"{self._prefix}/snapshots/"

    def run(self) -> Status:
        """One pass. Each step goes on if another fails; the first failure is in the status, which is saved."""
        status = Status.load(self.status_path)
        status.last_run, status.error, status.notes = self._clock(), "", []
        kept = self.kept()
        steps: list[tuple[str, Callable[[], None]]] = [
            ("uploading", lambda: self._upload(status, kept)),
            ("making room", lambda: self._offload(status, kept)),
            ("bringing back what training needs", lambda: self._bring_back(status, kept)),
            ("deleting what TARS no longer keeps", lambda: self._forget(status, kept)),
            ("the snapshot", lambda: self._snapshot(status)),
            ("the lifecycle rules", lambda: self._lifecycle(status)),
        ]
        for what, step in steps:
            try:
                step()
            except (BackupError, OSError, sqlite3.Error) as e:
                print(f"(backup: {what} failed: {e})", flush=True)
                status.error = status.error or f"{what}: {e}"
        if not status.error:
            status.last_ok = status.last_run
        counts = self._store.rows("SELECT count(*) AS n, count(offloaded) AS away FROM remote_files")[0]
        status.in_bucket, status.only_in_bucket = counts["n"], counts["away"]
        status.save(self.status_path)
        return status

    def kept(self) -> dict[str, Kept]:
        """Every file TARS keeps, by its path in the event log's folder: the recordings and the files it sent. A wake
        window training learns from stays on the machine, as does every sent file."""
        kept: dict[str, Kept] = {}

        def keep(rel: str, ts: float, stays: bool) -> None:
            """A file more than one row names (a request and its turn) stays as long as any of them needs it."""
            if (k := kept.get(rel)) is not None:
                k.ts, k.stays = max(k.ts, ts), k.stays or stays
            else:
                kept[rel] = Kept(ts, stays)

        events = self._store.rows(
            "SELECT id, ts, kind, label, outcome, follow, transcript, audio, utterance_audio FROM events "
            "WHERE audio IS NOT NULL OR utterance_audio IS NOT NULL"
        )
        for e in self._log.labeled(events):
            if e["audio"]:
                keep(e["audio"], e["ts"], stays=learning_label(e) is not None)
            if e["utterance_audio"]:
                keep(e["utterance_audio"], e["ts"], stays=False)
        for t in self._store.rows("SELECT ts, audio FROM turns WHERE audio IS NOT NULL"):
            keep(t["audio"], t["ts"], stays=False)
        for i in self._store.rows("SELECT ts, file FROM items WHERE file IS NOT NULL"):
            keep(i["file"], i["ts"], stays=True)
        return kept

    def _remote(self) -> dict[str, dict]:
        return {r["path"]: r for r in self._store.rows("SELECT * FROM remote_files")}

    def _upload(self, status: Status, kept: dict[str, Kept]) -> None:
        status.uploaded = 0
        for rel in sorted(set(kept) - set(self._remote())):
            try:
                data = (self._store.folder / rel).read_bytes()
            except FileNotFoundError:
                continue  # deleted in the web UI since it was listed
            self._s3.put(self.key(rel), data)
            self._store.write(
                "INSERT OR REPLACE INTO remote_files (path, uploaded, size) VALUES (?, ?, ?)",
                (rel, self._clock(), len(data)),
            )
            status.uploaded += 1

    def _offload(self, status: Status, kept: dict[str, Kept]) -> None:
        """Removes from the machine what's old enough, may go, and the bucket is checked to have whole."""
        status.offloaded = 0
        if self._cfg.keep_local_days <= 0:
            return
        cutoff = self._clock() - self._cfg.keep_local_days * DAY_S
        for rel, row in self._remote().items():
            k = kept.get(rel)
            if row["offloaded"] is not None or k is None or k.stays or k.ts > cutoff:
                continue
            path = self._store.folder / rel
            if not path.exists():
                continue
            if self._s3.size(self.key(rel)) != path.stat().st_size:
                status.notes.append(f"{rel} wasn't whole in the bucket: it's uploaded again")
                self._store.write("DELETE FROM remote_files WHERE path=?", (rel,))
                continue
            path.unlink()
            self._store.write("UPDATE remote_files SET offloaded=? WHERE path=?", (self._clock(), rel))
            status.offloaded += 1

    def _bring_back(self, status: Status, kept: dict[str, Kept]) -> None:
        """A recording that's only in the bucket, and must be on the machine now (a wake labeled in Review since,
        which training learns from), comes back."""
        status.brought_back = 0
        for rel, row in self._remote().items():
            k = kept.get(rel)
            if row["offloaded"] is None or k is None or not k.stays:
                continue
            if self._get_back(rel):
                status.brought_back += 1
            else:
                status.notes.append(f"{rel} is needed on the machine but archived: --fetch-audio --restore-archived")

    def _get_back(self, rel: str) -> bool:
        """The file, from the bucket onto the machine; False if it's archived (and not restored)."""
        try:
            data = self._s3.get(self.key(rel))
        except BackupError as e:
            if e.code == "InvalidObjectState":
                return False
            raise
        path = self._store.folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, data)
        self._store.write("UPDATE remote_files SET offloaded=NULL WHERE path=?", (rel,))
        return True

    def _forget(self, status: Status, kept: dict[str, Kept]) -> None:
        remote = self._remote()
        gone = [rel for rel in remote if rel not in kept]
        status.deleted = 0
        if not gone:
            return
        if len(gone) > MAX_DELETE_ANYWAY and len(gone) > MAX_DELETE_SHARE * len(remote):
            raise BackupError(
                f"it would delete {len(gone)} of the {len(remote)} files in the bucket, which looks like a mistake: "
                "nothing was deleted. If TARS really no longer keeps them, delete them in the bucket by hand"
            )
        self._s3.delete([self.key(rel) for rel in gone])
        with self._store.transaction() as db:
            db.executemany("DELETE FROM remote_files WHERE path=?", [(rel,) for rel in gone])
        status.deleted = len(gone)

    def _snapshot(self, status: Status) -> None:
        now = self._clock()
        if now - status.last_snapshot < SNAPSHOT_EVERY_S:
            return
        self._s3.put(f"{self.snapshots}{_stamp(now)}.tar.gz", self.snapshot_bytes())
        status.last_snapshot = now

    def snapshot_bytes(self) -> bytes:
        """The database, consistent while TARS writes to it, and the snapshot's other files, as one .tar.gz."""
        out = io.BytesIO()
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "events.db"
            source = sqlite3.connect(f"{(self._store.folder / 'events.db').resolve().as_uri()}?mode=ro", uri=True)
            target = sqlite3.connect(copy)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            with tarfile.open(fileobj=out, mode="w:gz") as tar:
                tar.add(copy, arcname=self._db_as)
                for name, path in self._extra.items():
                    if path.exists():
                        tar.add(path, arcname=name)
        return out.getvalue()

    def _lifecycle(self, status: Status) -> None:
        """Puts the bucket's rules for TARS's prefix in step with [backup], leaving any other rule as it is. Keys
        that may not change them only leave a note: the backup works without them."""
        wanted = [
            {
                "ID": SNAPSHOTS_RULE,
                "Filter": {"Prefix": self.snapshots},
                "Status": "Enabled",
                "Expiration": {"Days": self._cfg.keep_snapshots_days},
            }
        ]
        if self._cfg.archive_after_days > 0:
            transition = {"Days": self._cfg.archive_after_days, "StorageClass": DEEP_ARCHIVE}
            wanted.append(
                {
                    "ID": ARCHIVE_RULE,
                    "Filter": {"Prefix": f"{self._prefix}/audio/"},
                    "Status": "Enabled",
                    "Transitions": [transition],
                }
            )
        ours = {ARCHIVE_RULE, SNAPSHOTS_RULE}
        try:
            rules = self._s3.lifecycle()
            if _same([r for r in rules if r.get("ID") in ours], wanted):
                status.lifecycle = _said(self._cfg)
                return
            self._s3.set_lifecycle([r for r in rules if r.get("ID") not in ours] + wanted)
        except BackupError as e:
            status.lifecycle = f"not set: {e}"
            status.notes.append("the keys may not set the bucket's lifecycle rules: set them by hand (docs/backup.md)")
            return
        status.lifecycle = _said(self._cfg)

    # Bringing files back.

    def fetch(self, restore: bool = False) -> dict[str, int]:
        """Brings every file that's only in the bucket back onto the machine. One that's archived is asked to be
        restored (`restore`), to be fetched by a later run once it's back (up to 48 hours). Returns the counts."""
        counts = {"fetched": 0, "archived": 0, "restoring": 0}
        for rel, row in self._remote().items():
            if row["offloaded"] is None or (self._store.folder / rel).exists():
                continue
            if self._get_back(rel):
                counts["fetched"] += 1
            elif restore:
                self._s3.restore(self.key(rel))
                counts["restoring"] += 1
            else:
                counts["archived"] += 1
        return counts

    def read(self, rel: str) -> bytes | None:
        """A file that's only in the bucket, for the web UI to play; None if it's archived, or not there."""
        if not self._store.rows("SELECT 1 FROM remote_files WHERE path=?", (rel,)):
            return None
        try:
            return self._s3.get(self.key(rel))
        except BackupError as e:
            if e.code in ("InvalidObjectState", "NoSuchKey"):
                return None
            raise


def restore_snapshot(s3: S3, prefix: str, root: Path, db_as: str) -> str:
    """Puts the bucket's latest snapshot in place under the project's folder `root` (a new machine). Refuses to write
    over an event log that's there already (`db_as`, as Backup's). Returns the snapshot's key."""
    if (root / db_as).exists():
        raise ValueError(f"{root / db_as} is there already: move it away to restore over it")
    key = s3.latest(f"{prefix.strip('/')}/snapshots/")
    if key is None:
        raise ValueError("there's no snapshot in the bucket yet")
    with tarfile.open(fileobj=io.BytesIO(s3.get(key)), mode="r:gz") as tar:
        tar.extractall(root, filter="data")
    return key


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _same(a: list[dict], b: list[dict]) -> bool:
    by_id = itemgetter("ID")
    return json.dumps(sorted(a, key=by_id), sort_keys=True) == json.dumps(sorted(b, key=by_id), sort_keys=True)


def _said(cfg: BackupConfig) -> str:
    archive = f"audio to Deep Archive after {cfg.archive_after_days} days" if cfg.archive_after_days else "no archive"
    return f"{archive}; snapshots kept {cfg.keep_snapshots_days} days"
