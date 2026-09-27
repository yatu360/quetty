"""Single-process ownership of one operator database.

The operator UI owns the only headed Chrome pool and the process-local operator
queue for its database. An OS advisory lock proves that no other UI process is
running against the same database, so startup can treat every persisted browser
owner as belonging to a dead process. The operating system releases the lock when
the process exits for any reason, including a crash or ``SIGKILL``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import BinaryIO


class InstanceLockError(RuntimeError):
    """Another live process already owns the database."""


class InstanceLock:
    """Non-blocking exclusive lock on ``<database>.lock``."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: BinaryIO | None = None

    @classmethod
    def for_database(cls, database: str | Path) -> InstanceLock | None:
        """Return a lock beside a file database; in-memory databases need none."""

        raw = str(database)
        if raw.startswith("sqlite:///"):
            raw = raw.removeprefix("sqlite:///")
        if raw in {"", ":memory:"}:
            return None
        path = Path(raw)
        return cls(path.with_name(f"{path.name}.lock"))

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a+b")  # noqa: SIM115 - held for the process lifetime
        try:
            _lock(handle)
        except OSError as exc:
            handle.close()
            raise InstanceLockError(
                "Another operator UI process is already using this database"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock(handle)
        finally:
            handle.close()


if sys.platform == "win32":
    import msvcrt

    def _lock(handle: BinaryIO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
