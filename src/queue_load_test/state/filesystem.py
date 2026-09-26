"""Atomic local-filesystem browser-state storage."""

import asyncio
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from queue_load_test.state.base import (
    BrowserState,
    StateCorruptError,
    StateSessionMismatchError,
    StateUnreadableError,
)

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

STATE_DOCUMENT_FORMAT = "queue-load-test.browser-state"
STATE_DOCUMENT_VERSION = 1


@dataclass(frozen=True, slots=True)
class StateDocument:
    """A validated browser-state document read from storage.

    ``legacy`` documents predate the self-describing envelope: they are plain
    ``storage_state`` JSON with no embedded session ID or digest, so only the
    deterministic file name associates them with a session.
    """

    state: BrowserState
    legacy: bool = False


def _state_text(state: BrowserState) -> str:
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"))


def _state_digest(state_text: str) -> str:
    return hashlib.sha256(state_text.encode("utf-8")).hexdigest()


def encode_state_document(session_id: str, state: BrowserState) -> str:
    """Serialize ``state`` with its owning session ID and a SHA-256 digest."""

    envelope = {
        "format": STATE_DOCUMENT_FORMAT,
        "version": STATE_DOCUMENT_VERSION,
        "session_id": session_id,
        "sha256": _state_digest(_state_text(state)),
        "state": state,
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n"


def read_state_document(path: Path, session_id: str) -> StateDocument:
    """Read and validate one state file for ``session_id``.

    Raises ``FileNotFoundError`` for a missing file and a ``StateStoreError``
    subclass for an unreadable, corrupt, or other-session document. Error
    messages name only the session ID, never document contents.
    """

    try:
        with path.open(encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise StateUnreadableError(f"Could not read browser state for {session_id!r}") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateCorruptError(f"Browser state for {session_id!r} is not valid JSON") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise StateCorruptError(f"Browser state for {session_id!r} is not a JSON object")
    if "format" not in value:
        return StateDocument(state=value, legacy=True)
    if value.get("format") != STATE_DOCUMENT_FORMAT:
        raise StateCorruptError(f"Browser state for {session_id!r} has an unknown format")
    if value.get("version") != STATE_DOCUMENT_VERSION:
        raise StateCorruptError(f"Browser state for {session_id!r} has an unsupported version")
    if value.get("session_id") != session_id:
        raise StateSessionMismatchError(
            f"Browser state at the path for {session_id!r} belongs to another session"
        )
    state = value.get("state")
    if not isinstance(state, dict) or not all(isinstance(key, str) for key in state):
        raise StateCorruptError(f"Browser state for {session_id!r} has no state object")
    if value.get("sha256") != _state_digest(_state_text(state)):
        raise StateCorruptError(f"Browser state for {session_id!r} failed its integrity check")
    return StateDocument(state=state)


class FileSystemStateStore:
    """Store one private JSON document per session using atomic replacement.

    Each document embeds its session ID and a digest of the state, so a load never
    returns another session's state and in-place corruption is detected. ``save`` is
    idempotent: a failed save leaves the previous complete document and may be retried.
    """

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self._save_locks: dict[str, asyncio.Lock] = {}

    def path_for(self, session_id: str) -> Path:
        if not _SAFE_SESSION_ID.fullmatch(session_id) or session_id in {".", ".."}:
            raise ValueError("session_id contains unsafe path characters")
        return self.directory / f"{session_id}.json"

    async def save(self, session_id: str, state: BrowserState) -> Path:
        path = self.path_for(session_id)
        document = encode_state_document(session_id, state)
        # Windows cannot atomically replace a destination while another thread is
        # replacing the same path. Serialize only same-session writes; independent
        # sessions retain full concurrency.
        lock = self._save_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            await asyncio.to_thread(self._save, path, document)
        return path

    @staticmethod
    def _save(path: Path, document: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        _fsync_directory(path.parent)

    async def load(self, session_id: str) -> BrowserState | None:
        path = self.path_for(session_id)
        return await asyncio.to_thread(self._load, path, session_id)

    @staticmethod
    def _load(path: Path, session_id: str) -> BrowserState | None:
        try:
            return read_state_document(path, session_id).state
        except FileNotFoundError:
            return None

    async def delete(self, session_id: str) -> bool:
        path = self.path_for(session_id)
        return await asyncio.to_thread(self._delete, path)

    @staticmethod
    def _delete(path: Path) -> bool:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True


def _fsync_directory(directory: Path) -> None:
    """Persist the replaced directory entry where directory handles support fsync."""

    # Windows does not permit opening directory paths with ``os.open``. The replaced
    # file itself has already been flushed before this best-effort POSIX durability
    # step.
    if os.name == "nt":
        return

    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
