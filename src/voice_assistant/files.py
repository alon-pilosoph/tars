"""Whole-file replacement that a reader never sees half-done, even after a power cut."""

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    fsync_folder(path.parent)


def fsync_folder(folder: Path) -> None:
    """A rename reaches the disk only once its folder is fsynced too."""
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
