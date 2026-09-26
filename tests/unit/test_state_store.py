import asyncio
import json
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from queue_load_test.state import (
    FileSystemStateStore,
    StateCorruptError,
    StateSessionMismatchError,
    StateStoreError,
    StateUnreadableError,
    filesystem,
)
from queue_load_test.state.filesystem import STATE_DOCUMENT_FORMAT, STATE_DOCUMENT_VERSION


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


async def test_saved_document_embeds_session_identity_digest_and_private_mode(
    tmp_path: Path,
) -> None:
    store = FileSystemStateStore(tmp_path / "browser-state")
    state = {"cookies": [{"name": "c", "value": "v"}], "origins": []}

    path = await store.save("session-1", state)
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["format"] == STATE_DOCUMENT_FORMAT
    assert document["version"] == STATE_DOCUMENT_VERSION
    assert document["session_id"] == "session-1"
    assert len(document["sha256"]) == 64
    assert document["state"] == state
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_legacy_plain_storage_state_remains_loadable(tmp_path: Path) -> None:
    store = FileSystemStateStore(tmp_path / "browser-state")
    store.directory.mkdir()
    legacy = {"cookies": [], "origins": []}
    store.path_for("session-1").write_text(json.dumps(legacy), encoding="utf-8")

    assert await store.load("session-1") == legacy


async def test_load_rejects_another_sessions_state_copied_into_place(tmp_path: Path) -> None:
    store = FileSystemStateStore(tmp_path / "browser-state")
    await store.save("session-a", {"owner": "a"})
    shutil.copyfile(store.path_for("session-a"), store.path_for("session-b"))

    with pytest.raises(StateSessionMismatchError):
        await store.load("session-b")
    assert await store.load("session-a") == {"owner": "a"}


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda text: text[: len(text) // 2],
        lambda text: text.replace('"owner":"a"', '"owner":"z"'),
        lambda text: text.replace('"version":1', '"version":99'),
        lambda text: text.replace(STATE_DOCUMENT_FORMAT, "other-format"),
        lambda text: "[]",
    ],
    ids=["truncated", "tampered", "unknown-version", "unknown-format", "not-object"],
)
async def test_load_detects_corrupt_documents(
    tmp_path: Path,
    corrupt: Callable[[str], str],
) -> None:
    store = FileSystemStateStore(tmp_path / "browser-state")
    path = await store.save("session-a", {"owner": "a"})
    path.write_text(corrupt(path.read_text(encoding="utf-8")), encoding="utf-8")

    with pytest.raises(StateCorruptError):
        await store.load("session-a")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
async def test_load_reports_unreadable_state_without_leaking_contents(tmp_path: Path) -> None:
    store = FileSystemStateStore(tmp_path / "browser-state")
    path = await store.save("session-a", {"cookies": [{"value": "secret-token"}]})
    path.chmod(0o000)
    try:
        with pytest.raises(StateUnreadableError) as raised:
            await store.load("session-a")
    finally:
        path.chmod(0o600)

    assert "secret-token" not in str(raised.value)


async def test_corruption_errors_do_not_include_state_contents(tmp_path: Path) -> None:
    store = FileSystemStateStore(tmp_path / "browser-state")
    path = await store.save("session-a", {"cookies": [{"value": "secret-token"}]})
    path.write_text(path.read_text(encoding="utf-8").replace("secret", "altered"))

    with pytest.raises(StateStoreError) as raised:
        await store.load("session-a")

    assert "secret" not in str(raised.value)
    assert "altered" not in str(raised.value)


async def test_failed_save_can_be_retried_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSystemStateStore(tmp_path / "browser-state")
    await store.save("session-1", {"version": 1})
    original_replace = os.replace
    failures = iter([OSError("simulated transient failure")])

    def flaky_replace(source: Path | str, destination: Path | str) -> None:
        error = next(failures, None)
        if error is not None:
            raise error
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError, match="transient"):
        await store.save("session-1", {"version": 2})
    assert await store.load("session-1") == {"version": 1}

    await store.save("session-1", {"version": 2})

    assert await store.load("session-1") == {"version": 2}
    assert list(store.directory.glob("*.tmp")) == []


async def test_save_fsyncs_the_state_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSystemStateStore(tmp_path / "browser-state")
    synced: list[Path] = []
    monkeypatch.setattr(filesystem, "_fsync_directory", synced.append)

    await store.save("session-1", {})

    assert synced == [store.directory]


async def test_concurrent_independent_sessions_keep_their_own_state(tmp_path: Path) -> None:
    store = FileSystemStateStore(tmp_path / "browser-state")
    session_ids = [f"session-{index:03d}" for index in range(200)]

    await asyncio.gather(
        *(store.save(session_id, {"owner": session_id}) for session_id in session_ids)
    )
    loaded = await asyncio.gather(*(store.load(session_id) for session_id in session_ids))

    assert loaded == [{"owner": session_id} for session_id in session_ids]
    assert list(store.directory.glob("*.tmp")) == []


async def test_concurrent_saves_of_one_session_leave_one_complete_version(
    tmp_path: Path,
) -> None:
    store = FileSystemStateStore(tmp_path / "browser-state")
    versions = [{"version": index, "payload": "x" * 4096} for index in range(50)]

    await asyncio.gather(*(store.save("session-1", version) for version in versions))

    assert await store.load("session-1") in versions
    assert list(store.directory.glob("*.tmp")) == []
