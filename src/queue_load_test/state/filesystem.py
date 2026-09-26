"""Atomic local-filesystem browser-state storage."""

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

from queue_load_test.state.base import BrowserState, StateStoreError

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class FileSystemStateStore:
    """Store one private JSON document per session using atomic replacement."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def path_for(self, session_id: str) -> Path:
        if not _SAFE_SESSION_ID.fullmatch(session_id) or session_id in {".", ".."}:
            raise ValueError("session_id contains unsafe path characters")
        return self.directory / f"{session_id}.json"

    async def save(self, session_id: str, state: BrowserState) -> Path:
        path = self.path_for(session_id)
        await asyncio.to_thread(self._save, path, state)
        return path

    def _save(self, path: Path, state: BrowserState) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    async def load(self, session_id: str) -> BrowserState | None:
        path = self.path_for(session_id)
        return await asyncio.to_thread(self._load, path)

    @staticmethod
    def _load(path: Path) -> BrowserState | None:
        try:
            with path.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise StateStoreError(f"Could not load browser state for {path.stem!r}") from exc
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise StateStoreError(f"Browser state for {path.stem!r} is not a JSON object")
        return value

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
