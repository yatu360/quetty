import os
from pathlib import Path

import pytest

from queue_load_test.state import FileSystemStateStore


async def test_state_save_load_and_delete(tmp_path: Path) -> None:
    store = FileSystemStateStore(tmp_path / "browser-state")
    state = {
        "cookies": [{"name": "QueueITAccepted", "value": "sensitive-token"}],
        "origins": [],
    }

    path = await store.save("session-1", state)

    assert path == tmp_path / "browser-state" / "session-1.json"
    assert await store.load("session-1") == state
    assert await store.delete("session-1")
    assert await store.load("session-1") is None
    assert not await store.delete("session-1")


async def test_state_save_atomically_replaces_existing_file(tmp_path: Path) -> None:
    store = FileSystemStateStore(tmp_path / "browser-state")
    await store.save("session-1", {"version": 1})

    await store.save("session-1", {"version": 2, "complete": True})

    assert await store.load("session-1") == {"version": 2, "complete": True}
    assert list(store.directory.glob("*.tmp")) == []


async def test_failed_replace_preserves_previous_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSystemStateStore(tmp_path / "browser-state")
    await store.save("session-1", {"version": 1})

    def fail_replace(source: Path | str, destination: Path | str) -> None:
        del source, destination
        raise OSError("simulated interrupted replacement")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted"):
        await store.save("session-1", {"version": 2})

    assert await store.load("session-1") == {"version": 1}
    assert list(store.directory.glob("*.tmp")) == []


@pytest.mark.parametrize("session_id", ["../escape", "nested/path", "", "."])
async def test_state_store_rejects_unsafe_session_ids(
    tmp_path: Path,
    session_id: str,
) -> None:
    store = FileSystemStateStore(tmp_path)

    with pytest.raises(ValueError, match="unsafe"):
        await store.save(session_id, {})
