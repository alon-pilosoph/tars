"""Backing up what TARS keeps to an Amazon S3 bucket, and keeping the machine's card from filling with it.

`voice-assistant --backup` runs one pass, every 15 minutes from its systemd timer (docs/backup.md):

1. Every recording and every file TARS sent that isn't in the bucket yet is uploaded, under [backup] prefix.
2. A recording older than [backup] keep_local_days is removed from the machine, once the bucket is checked to have it
   whole. What something reads from the machine later stays there too: wake windows training learns from, each named
   voice's newest requests (its voiceprint is rebuilt from them), and the files TARS sent. A file that becomes one of
   those (a wake labeled in Review) comes back.
3. Whatever TARS no longer keeps (a conversation deleted in the web UI, old audio dropped by [learning]
   keep_audio_days) is deleted from the bucket too: the bucket holds what TARS keeps, no more.
4. Once a day, a snapshot: the database (through sqlite's backup API, so it's consistent while TARS writes), the
   voiceprints, the wake models trained on the household and config.local.toml, as one .tar.gz. Never .env.
5. The bucket's lifecycle rules, kept in step with [backup], move the audio to Glacier Deep Archive after
   archive_after_days, and delete snapshots after keep_snapshots_days.

What's in the bucket is kept in the database (remote_files), so a pass looks only at what's new. The web UI plays a
recording that's no longer on the machine straight from the bucket, until it's archived. `--fetch-audio` brings
recordings back (asking S3 to restore archived ones first), and `--restore-backup` puts the latest snapshot in place
on a new machine.
"""

import fcntl
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from operator import itemgetter
from pathlib import Path

from .config import BackupConfig
from .conversations import ConversationLog
from .events import DAY_S, EventLog, KeptFile
from .files import atomic_write
from .store import DB_FILE

SNAPSHOT_EVERY_S = DAY_S
STATUS_FILE = "backup.json"  # in the event log's folder: how the last pass went, for --check
LOCK_FILE = "backup.lock"  # in the event log's folder: held by a pass or a fetch, so two never run at once
DEEP_ARCHIVE = "DEEP_ARCHIVE"  # S3's name for Glacier Deep Archive
# The bucket's lifecycle rules this keeps in step with [backup], by their ids; any other rule there is left alone.
ARCHIVE_RULE, SNAPSHOTS_RULE = "tars-audio-to-deep-archive", "tars-snapshots-expire"
DELETE_BATCH = 1000  # S3 deletes at most this many objects a request
# A pass that would delete more than this share of the bucket, and more than a few files, stops instead: a database
# that suddenly keeps nothing is far likelier a mistake than a household deleting its whole history.
MAX_DELETE_SHARE, MAX_DELETE_ANYWAY = 0.5, 20
# Asking S3 to bring an archived recording back: the bulk tier (up to 48 hours, the cheapest), for this many days.
RESTORE_TIER, RESTORE_DAYS = "Bulk", 7
ARCHIVED, MISSING = "InvalidObjectState", ("404", "NoSuchKey", "NotFound")  # S3's error codes


class BackupError(Exception):
    """S3 refused or didn't answer: `code` is S3's error code ("AccessDenied", "NoSuchKey") when it gave one."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class Busy(Exception):
    """Another pass, or a fetch, is running."""


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
            if e.code in MISSING:
                return None
            raise

    def get(self, key: str) -> bytes:
        """BackupError with code ARCHIVED for one that's archived and not restored."""
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

    def keys(self, prefix: str) -> Iterator[str]:
        """Every key under `prefix`, a listing (of a thousand at most) at a time."""
        more = {}
        while True:
            page = self._call("list_objects_v2", Prefix=prefix, **more)
            yield from (o["Key"] for o in page.get("Contents") or [])
            if not page.get("IsTruncated"):
                return
            more = {"ContinuationToken": page["NextContinuationToken"]}

    def lifecycle(self) -> list[dict]:
        try:
            return self._call("get_bucket_lifecycle_configuration").get("Rules", [])
        except BackupError as e:
            if e.code == "NoSuchLifecycleConfiguration":
                return []
            raise

    def set_lifecycle(self, rules: list[dict]) -> None:
        self._call("put_bucket_lifecycle_configuration", LifecycleConfiguration={"Rules": rules})


def make_s3(cfg: BackupConfig, key_id: str, secret: str) -> S3:
    """S3 with the given key, for [backup]'s bucket and region. ValueError if boto3 isn't installed."""
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError:
        raise ValueError('it needs the optional extra "backup": uv sync --extra backup') from None
    client = boto3.session.Session().client(
        "s3",
        region_name=cfg.region,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        config=BotoConfig(connect_timeout=10, read_timeout=60, retries={"max_attempts": 5, "mode": "standard"}),
    )
    return S3(client, cfg.bucket)


def snapshots_prefix(cfg: BackupConfig) -> str:
    return f"{cfg.prefix}/snapshots/"


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
    lifecycle_ok: bool = True
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


@dataclass
class Fetched:
    """What --fetch-audio did: brought back, archived (or now being restored), and gone from the bucket."""

    fetched: int = 0
    archived: int = 0
    restoring: int = 0
    missing: int = 0


class Backup:
    """The backup of one event log to one bucket: a pass (run), bringing files back (fetch), and the web UI's reads
    of what's only in the bucket (read)."""

    def __init__(
        self,
        log: EventLog,
        cfg: BackupConfig,
        s3: S3,
        db_as: str,
        snapshot_files: dict[str, Path] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        """`db_as`: where a snapshot keeps the database, as [learning] folder puts it ("voice_data/events/events.db"),
        so restore_snapshot puts it back there. `snapshot_files`: what else goes in, by the path it has there, like
        {"voice_data/voiceprints.npz": ...}."""
        self._log, self._store, self._cfg, self._s3, self._clock = log, log.store, cfg, s3, clock
        self._conversations = ConversationLog(log)
        self._db_as, self._snapshot_files = db_as, snapshot_files or {}
        self.status_path = log.folder / STATUS_FILE

    def check(self) -> None:
        """The bucket answers, and the keys may use it: BackupError if not."""
        self._s3.check()

    def key(self, rel: str) -> str:
        """Where a file in the event log's folder is in the bucket."""
        return f"{self._cfg.prefix}/{rel}"

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Holds LOCK_FILE for a pass or a fetch: Busy if another holds it. The lock goes with the process."""
        with open(self._log.folder / LOCK_FILE, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy("another backup pass, or a fetch, is running") from None
            yield

    def run(self, delete_anyway: bool = False) -> Status:
        """One pass. Each step goes on if another fails; the first failure is in the status, which is saved.
        `delete_anyway`: delete from the bucket what TARS no longer keeps even if it's most of it. Busy if another
        pass is running."""
        with self._exclusive():
            status = Status.load(self.status_path)
            status.last_run, status.error, status.notes = self._clock(), "", []
            steps: list[tuple[str, Callable[[], None]]] = [
                ("uploading", lambda: self._upload(status)),
                ("making room", lambda: self._offload(status)),
                ("bringing back what's needed on the machine", lambda: self._bring_back(status)),
                ("deleting what TARS no longer keeps", lambda: self._forget(status, delete_anyway)),
                ("the snapshot", lambda: self._snapshot(status)),
                ("the lifecycle rules", lambda: self._lifecycle(status)),
            ]
            for what, step in steps:
                try:
                    step()
                except Exception as e:  # noqa: BLE001 - one step that fails never stops the others, and is kept
                    print(f"(backup: {what} failed: {e})", flush=True)
                    status.error = status.error or f"{what}: {e}"
            if not status.error:
                status.last_ok = status.last_run
            counts = self._store.rows("SELECT count(*) AS n, count(offloaded) AS away FROM remote_files")[0]
            status.in_bucket, status.only_in_bucket = counts["n"], counts["away"]
            status.save(self.status_path)
            return status

    def kept(self) -> dict[str, KeptFile]:
        """Every file TARS keeps, by its path in the event log's folder. One that more than one row names (a request
        and its turn) stays on the machine if any of them needs it, and counts as made when the newest was."""
        kept: dict[str, KeptFile] = {}
        for f in self._log.kept_files() + self._conversations.kept_files():
            if (k := kept.get(f.path)) is not None:
                f = KeptFile(f.path, max(k.ts, f.ts), k.stays or f.stays)
            kept[f.path] = f
        return kept

    def _remote(self) -> dict[str, dict]:
        return {r["path"]: r for r in self._store.rows("SELECT * FROM remote_files")}

    def _upload(self, status: Status) -> None:
        status.uploaded = 0
        for rel in sorted(set(self.kept()) - set(self._remote())):
            try:
                data = (self._store.folder / rel).read_bytes()
            except FileNotFoundError:
                continue  # deleted in the web UI since it was listed
            self._s3.put(self.key(rel), data)
            self._store.write(
                "INSERT INTO remote_files (path, uploaded, size) VALUES (?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET uploaded=excluded.uploaded, size=excluded.size",
                (rel, self._clock(), len(data)),
            )
            status.uploaded += 1

    def _offload(self, status: Status) -> None:
        """Removes from the machine what's old enough (or was fetched back long enough ago), may go, and the bucket
        is checked to have whole."""
        status.offloaded = 0
        if self._cfg.keep_local_days <= 0:
            return
        cutoff, kept = self._clock() - self._cfg.keep_local_days * DAY_S, self.kept()
        for rel, row in self._remote().items():
            k = kept.get(rel)
            if row["offloaded"] is not None or k is None or k.stays or max(k.ts, row["fetched"] or 0) > cutoff:
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

    def _bring_back(self, status: Status) -> None:
        """A file that's only in the bucket, and must be on the machine now (a wake labeled in Review since, which
        training learns from), comes back."""
        status.brought_back = 0
        kept = self.kept()
        for rel, row in self._remote().items():
            k = kept.get(rel)
            if row["offloaded"] is None or k is None or not k.stays:
                continue
            try:
                self._download(rel, row["size"])
                status.brought_back += 1
            except BackupError as e:
                if e.code != ARCHIVED:
                    raise
                status.notes.append(f"{rel} is needed on the machine but archived: --fetch-audio --restore-archived")

    def _download(self, rel: str, size: int) -> None:
        """The file, from the bucket onto the machine. BackupError if it's archived (ARCHIVED), gone, or cut short."""
        data = self._s3.get(self.key(rel))
        if len(data) != size:
            raise BackupError(f"{rel} came back {len(data)} bytes long, not {size}")
        path = self._store.folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, data)
        self._store.write("UPDATE remote_files SET offloaded=NULL, fetched=? WHERE path=?", (self._clock(), rel))

    def _forget(self, status: Status, delete_anyway: bool) -> None:
        remote, kept = self._remote(), self.kept()
        gone = [rel for rel in remote if rel not in kept]
        status.deleted = 0
        if not gone:
            return
        if not delete_anyway and len(gone) > MAX_DELETE_ANYWAY and len(gone) > MAX_DELETE_SHARE * len(remote):
            raise BackupError(
                f"it would delete {len(gone)} of the {len(remote)} files in the bucket, which looks like a mistake, so "
                "nothing was deleted. If TARS really no longer keeps them: voice-assistant --backup --delete-anyway"
            )
        self._s3.delete([self.key(rel) for rel in gone])
        with self._store.transaction() as db:
            db.executemany("DELETE FROM remote_files WHERE path=?", [(rel,) for rel in gone])
        status.deleted = len(gone)

    def _snapshot(self, status: Status) -> None:
        """Once a day, and never of a database with nothing in it yet: on a new machine, that would become the
        latest snapshot, ahead of the one to restore."""
        now = self._clock()
        if now - status.last_snapshot < SNAPSHOT_EVERY_S:
            return
        if not _has_history(self._store.folder / DB_FILE):
            status.notes.append("no snapshot yet: the database has nothing in it")
            return
        self._s3.put(f"{snapshots_prefix(self._cfg)}{_stamp(now)}.tar.gz", self.snapshot_bytes())
        status.last_snapshot = now

    def snapshot_bytes(self) -> bytes:
        """The database, consistent while TARS writes to it, and the snapshot's other files, as one .tar.gz."""
        out = io.BytesIO()
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / DB_FILE
            source = sqlite3.connect(f"{(self._store.folder / DB_FILE).resolve().as_uri()}?mode=ro", uri=True)
            target = sqlite3.connect(copy)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            with tarfile.open(fileobj=out, mode="w:gz") as tar:
                tar.add(copy, arcname=self._db_as)
                for name, path in self._snapshot_files.items():
                    if path.exists():
                        tar.add(path, arcname=name)
        return out.getvalue()

    def _lifecycle(self, status: Status) -> None:
        """Puts the bucket's rules for TARS's prefix in step with [backup], leaving any other rule as it is. A key
        that may not change them only leaves a note: the backup works without them."""
        wanted = [
            {
                "ID": SNAPSHOTS_RULE,
                "Filter": {"Prefix": snapshots_prefix(self._cfg)},
                "Status": "Enabled",
                "Expiration": {"Days": self._cfg.keep_snapshots_days},
            }
        ]
        if self._cfg.archive_after_days > 0:
            transition = {"Days": self._cfg.archive_after_days, "StorageClass": DEEP_ARCHIVE}
            wanted.append(
                {
                    "ID": ARCHIVE_RULE,
                    "Filter": {"Prefix": f"{self._cfg.prefix}/audio/"},
                    "Status": "Enabled",
                    "Transitions": [transition],
                }
            )
        ours = {ARCHIVE_RULE, SNAPSHOTS_RULE}
        try:
            rules = self._s3.lifecycle()
            if not _same([r for r in rules if r.get("ID") in ours], wanted):
                self._s3.set_lifecycle([r for r in rules if r.get("ID") not in ours] + wanted)
        except BackupError as e:
            status.lifecycle, status.lifecycle_ok = f"not set: {e}", False
            if e.code != "AccessDenied":
                raise
            status.notes.append("the key may not set the bucket's lifecycle rules: set them by hand (docs/backup.md)")
            return
        status.lifecycle, status.lifecycle_ok = _lifecycle_summary(self._cfg), True

    def fetch(self, restore: bool = False) -> Fetched:
        """Brings every file the bucket has and the machine doesn't back onto the machine, to stay there for
        keep_local_days. One that's archived is asked to be restored (`restore`), to be fetched by a later run once
        it's back (up to 48 hours). Busy if a pass is running."""
        with self._exclusive():
            done = Fetched()
            for rel, row in self._remote().items():
                if (self._store.folder / rel).exists():
                    continue
                try:
                    self._download(rel, row["size"])
                    done.fetched += 1
                except BackupError as e:
                    if e.code in MISSING:
                        done.missing += 1
                    elif e.code != ARCHIVED:
                        raise
                    elif restore:
                        self._s3.restore(self.key(rel))
                        done.restoring += 1
                    else:
                        done.archived += 1
            return done

    def read(self, rel: str) -> bytes | None:
        """A file that's only in the bucket, for the web UI to play; None if it's archived, or not there."""
        if not self._store.rows("SELECT 1 FROM remote_files WHERE path=?", (rel,)):
            return None
        try:
            return self._s3.get(self.key(rel))
        except BackupError as e:
            if e.code == ARCHIVED or e.code in MISSING:
                return None
            raise


def restore_snapshot(s3: S3, cfg: BackupConfig, root: Path, db_as: str) -> str:
    """Puts the bucket's latest snapshot in place under the project's folder `root`, on a new machine; returns its
    key. Refuses (ValueError) over a database with anything in it, or one that's open: TARS has to be stopped. Every
    file is unpacked beside where it goes, then moved into place, so a failure leaves nothing half-written."""
    db = root / db_as
    for open_file in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        if open_file.exists():
            raise ValueError(f"{db} is open ({open_file.name} is there): stop TARS's services first")
    if _has_history(db):
        raise ValueError(f"{db} has a history of its own: move it away to restore over it")
    key = max(s3.keys(snapshots_prefix(cfg)), default=None)
    if key is None:
        raise ValueError("there's no snapshot in the bucket yet")
    unpacked = Path(tempfile.mkdtemp(prefix=".restore-", dir=root))
    try:
        with tarfile.open(fileobj=io.BytesIO(s3.get(key)), mode="r:gz") as tar:
            tar.extractall(unpacked, filter="data")
        for source in sorted(p for p in unpacked.rglob("*") if p.is_file()):
            target = root / source.relative_to(unpacked)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
    finally:
        shutil.rmtree(unpacked, ignore_errors=True)
    return key


def _has_history(db: Path) -> bool:
    """Whether a database has anything in it: an event or a conversation. One that isn't there has none."""
    if not db.exists():
        return False
    with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as found:
        try:
            events = found.execute("SELECT count(*) FROM events").fetchone()[0]
            conversations = found.execute("SELECT count(*) FROM conversations").fetchone()[0]
        except sqlite3.OperationalError:
            return False  # no tables yet
    return events + conversations > 0


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _same(a: list[dict], b: list[dict]) -> bool:
    by_id = itemgetter("ID")
    return json.dumps(sorted(a, key=by_id), sort_keys=True) == json.dumps(sorted(b, key=by_id), sort_keys=True)


def _lifecycle_summary(cfg: BackupConfig) -> str:
    archive = f"audio to Deep Archive after {cfg.archive_after_days} days" if cfg.archive_after_days else "no archive"
    return f"{archive}; snapshots kept {cfg.keep_snapshots_days} days"
