import asyncio
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from queue_load_test.models import QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import SessionRepository, SQLiteSessionRepository
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


class _ListingRepository:
    """Minimal repository double: the checker only lists sessions."""

    def __init__(self, sessions: list[QueueSession]) -> None:
        self._sessions = sessions

    async def list(self) -> list[QueueSession]:
        return list(self._sessions)


def _checker(sessions: list[QueueSession], store: FileSystemStateStore) -> StateConsistencyChecker:
    return StateConsistencyChecker(cast(SessionRepository, _ListingRepository(sessions)), store)


def _snapshot(directory: Path) -> dict[str, tuple[int, int, bytes]]:
    snapshot: dict[str, tuple[int, int, bytes]] = {}
    for path in sorted(directory.iterdir()):
        details = path.stat()
        snapshot[path.name] = (details.st_mode, details.st_mtime_ns, path.read_bytes())
    return snapshot


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission mode is not enforced on Windows or when running as root",
)
async def test_consistency_checker_reports_mismatched_unreadable_insecure_and_legacy_state(
    tmp_path: Path,
) -> None:
    store = FileSystemStateStore(tmp_path / "state")
    sessions = [
        _session(session_id, store.path_for(session_id))
        for session_id in ("valid", "mismatched", "unreadable", "insecure", "legacy")
    ]
    for session_id in ("valid", "mismatched", "unreadable", "insecure"):
        await store.save(session_id, {"owner": session_id})
    shutil.copyfile(store.path_for("valid"), store.path_for("mismatched"))
    store.path_for("unreadable").chmod(0o000)
    store.path_for("insecure").chmod(0o644)
    store.path_for("legacy").write_text('{"cookies":[],"origins":[]}\n', encoding="utf-8")
    store.path_for("legacy").chmod(0o600)

    try:
        report = await _checker(sessions, store).check()
    finally:
        store.path_for("unreadable").chmod(0o600)

    kinds = {finding.kind: finding.session_ids for finding in report.findings}
    assert kinds == {
        "mismatched_state_file": ("mismatched",),
        "unreadable_state_file": ("unreadable",),
        "insecure_state_permissions": ("insecure",),
    }
    assert report.legacy_state_files == 1
    assert all("owner" not in (finding.detail or "") for finding in report.findings)


async def test_recovery_summary_counts_all_unusable_state_as_corrupt(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(":memory:")
    await repository.initialize()
    store = FileSystemStateStore(tmp_path / "state")
    try:
        for session_id in ("a", "b", "c"):
            await repository.create(_session(session_id, store.path_for(session_id)))
            await store.save(session_id, {"owner": session_id})
        shutil.copyfile(store.path_for("a"), store.path_for("b"))
        store.path_for("c").write_text("{truncated", encoding="utf-8")

        summary = await StateConsistencyChecker(repository, store).recovery_summary(
            now=datetime(2026, 9, 26, 12, tzinfo=UTC)
        )

        assert summary.missing_state_files == 0
        assert summary.corrupt_state_files == 2
    finally:
        await repository.close()


async def test_consistency_check_never_modifies_or_removes_state(tmp_path: Path) -> None:
    store = FileSystemStateStore(tmp_path / "state")
    await store.save("referenced", {"owner": "referenced"})
    await store.save("orphan", {"owner": "orphan"})
    (store.directory / ".interrupted.json.abc.tmp").write_text("partial", encoding="utf-8")
    store.path_for("corrupt").write_text("{not-json", encoding="utf-8")
    sessions = [
        _session(session_id, store.path_for(session_id))
        for session_id in ("referenced", "corrupt", "missing")
    ]
    before = _snapshot(store.directory)

    report = await _checker(sessions, store).check()

    assert {finding.kind for finding in report.findings} >= {
        "orphaned_state_file",
        "stale_temporary_file",
        "corrupt_state_file",
        "missing_state_file",
    }
    assert _snapshot(store.directory) == before


async def test_consistency_check_of_ten_thousand_states_finds_exactly_injected_faults(
    tmp_path: Path,
) -> None:
    store = FileSystemStateStore(tmp_path / "state")
    session_ids = [f"scale-{index:05d}" for index in range(10_000)]
    sessions = [_session(session_id, store.path_for(session_id)) for session_id in session_ids]
    for start in range(0, len(session_ids), 500):
        await asyncio.gather(
            *(
                store.save(session_id, {"owner": session_id})
                for session_id in session_ids[start : start + 500]
            )
        )

    clean = await _checker(sessions, store).check()
    assert clean.is_consistent
    assert clean.state_files == clean.hybrid_sessions_requiring_state == 10_000

    store.path_for("scale-00001").unlink()
    store.path_for("scale-00002").write_text("{truncated", encoding="utf-8")
    shutil.copyfile(store.path_for("scale-00003"), store.path_for("scale-00004"))
    await store.save("scale-orphan", {"owner": "scale-orphan"})

    faulty = await _checker(sessions, store).check()

    assert {finding.kind: finding.state_path for finding in faulty.findings} == {
        "missing_state_file": str(store.path_for("scale-00001")),
        "corrupt_state_file": str(store.path_for("scale-00002")),
        "mismatched_state_file": str(store.path_for("scale-00004")),
        "orphaned_state_file": str(store.path_for("scale-orphan")),
    }
    assert await store.load("scale-09999") == {"owner": "scale-09999"}
