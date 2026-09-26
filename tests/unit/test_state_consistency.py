import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from queue_load_test.models import QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.state import FileSystemStateStore, StateConsistencyChecker


def _session(
    session_id: str,
    state_path: Path,
    *,
    mode: SessionMode = SessionMode.HYBRID,
    queue_id: str | None = None,
) -> QueueSession:
    return QueueSession(
        session_id=session_id,
        queue_id=queue_id or f"queue-{session_id}",
        transfer_url=f"https://queue.test/{session_id}",
        mode=mode,
        status=QueueStatus.PARKED,
        state_path=state_path,
        created_at=datetime(2026, 9, 26, tzinfo=UTC),
    )


async def test_consistency_checker_reports_missing_orphan_corrupt_and_conflicting_paths(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(":memory:")
    await repository.initialize()
    store = FileSystemStateStore(tmp_path / "state")
    shared_path = store.directory / "shared.json"
    try:
        await repository.create(_session("missing", store.path_for("missing")))
        await repository.create(_session("corrupt", store.path_for("corrupt")))
        await repository.create(_session("duplicate-a", shared_path))
        await repository.create(_session("duplicate-b", shared_path))
        await store.save("valid", {"cookies": [], "origins": []})
        await repository.create(_session("valid", store.path_for("valid")))

        store.directory.mkdir(parents=True, exist_ok=True)
        store.path_for("corrupt").write_text("{not-json", encoding="utf-8")
        shared_path.write_text("{}\n", encoding="utf-8")
        (store.directory / "orphan.json").write_text("{}\n", encoding="utf-8")
        (store.directory / ".interrupted.json.abc.tmp").write_text("partial", encoding="utf-8")

        report = await StateConsistencyChecker(repository, store).check()
        recovery = await StateConsistencyChecker(repository, store).recovery_summary(
            now=datetime(2026, 9, 26, 12, tzinfo=UTC)
        )

        assert not report.is_consistent
        assert report.count("missing_state_file") == 1
        assert report.count("orphaned_state_file") == 1
        assert report.count("corrupt_state_file") == 1
        assert report.count("duplicate_state_path") == 1
        assert report.count("conflicting_state_path") == 2
        assert report.count("stale_temporary_file") == 1
        assert report.temporary_files == 1
        assert report.to_dict()["database_sessions"] == 5
        assert recovery.missing_state_files == 1
        assert recovery.corrupt_state_files == 1
        assert recovery.state_scan_performed
        assert (store.directory / "orphan.json").exists()
        assert (store.directory / ".interrupted.json.abc.tmp").exists()
    finally:
        await repository.close()


async def test_consistency_checker_does_not_require_state_for_transfer_only_or_failed(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(":memory:")
    await repository.initialize()
    store = FileSystemStateStore(tmp_path / "state")
    try:
        await repository.create(
            _session(
                "transfer-only",
                store.path_for("transfer-only"),
                mode=SessionMode.TRANSFER_ONLY,
            )
        )
        failed = _session("failed", store.path_for("failed"), queue_id=None)
        failed.queue_id = None
        failed.status = QueueStatus.FAILED
        await repository.create(failed)

        report = await StateConsistencyChecker(repository, store).check()

        assert report.is_consistent
        assert report.hybrid_sessions_requiring_state == 0
    finally:
        await repository.close()


async def test_state_store_slow_write_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSystemStateStore(tmp_path / "state")
    original_save = store._save

    def slow_save(path: Path, state: dict[str, object]) -> None:
        time.sleep(0.05)
        original_save(path, state)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_save", slow_save)
    save_task = asyncio.create_task(store.save("session", {}))
    started = time.perf_counter()
    await asyncio.sleep(0.005)
    elapsed = time.perf_counter() - started
    await save_task

    assert elapsed < 0.03
