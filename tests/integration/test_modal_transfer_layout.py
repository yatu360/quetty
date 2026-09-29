"""Acquire and restore Queue IDs through the current Queue-it transfer-dialog layout.

The layout mirrors the Glastonbury 2025 and Queue-it demo pages (closed "Continue my
journey" dialog, hidden footer Queue ID, ``#expectedServiceTime``). Pages come from
``LocalQueueSimulator`` on 127.0.0.1 with synthetic identities; this is not Queue-it.
"""

from pathlib import Path

import pytest

from queue_load_test.browser import BrowserBackendSetupError, BrowserManager, create_browser_backend
from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.models import BrowserBackendName, QueueStatus, SessionMode
from queue_load_test.queue_monitor import AdmissionDetector
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler.creation import (
    CreationOutcomeKind,
    CreationWorkItem,
    QueueSessionCreator,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import QueueSessionRestorer, RestoreFailure


async def _manager(backend: BrowserBackendName) -> BrowserManager:
    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=3,
        max_active_contexts=3,
        backend=create_browser_backend(backend),
    )
    try:
        await manager.start()
    except BrowserBackendSetupError as exc:
        pytest.skip(str(exc))
    return manager


@pytest.mark.parametrize("backend", [BrowserBackendName.CHROME, BrowserBackendName.PATCHRIGHT])
@pytest.mark.parametrize("mode", [SessionMode.HYBRID, SessionMode.TRANSFER_ONLY])
async def test_dialog_layout_acquires_and_restores_the_same_queue_id(
    tmp_path: Path, backend: BrowserBackendName, mode: SessionMode
) -> None:
    simulator = LocalQueueSimulator(new_identity_prefix="sim-modal", layout="modal")
    await simulator.start()
    repository = SQLiteSessionRepository(tmp_path / "modal.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    manager = await _manager(backend)
    try:
        creator = QueueSessionCreator(
            browser_manager=manager,
            repository=repository,
            state_store=state_store,
            staging_url=simulator.entry_url,
            state_directory=state_store.directory,
            mode=mode,
            browser_backend=backend,
        )
        created = await creator.create(CreationWorkItem(sequence=1, session_id="modal-1"))
        assert created.kind is CreationOutcomeKind.SUCCESS, created.failure_code
        assert created.session is not None
        queue_id = created.session.queue_id
        assert queue_id is not None and queue_id.startswith("sim-modal-")
        assert created.session.status is QueueStatus.PARKED  # parked once the ID is persisted
        assert created.session.transfer_url == simulator.transfer_url(queue_id)

        restorer = QueueSessionRestorer(
            browser_manager=manager,
            repository=repository,
            state_store=state_store,
            admission_detector=AdmissionDetector.from_urls(simulator.entry_url),
            storage_navigation_url=simulator.entry_url,
            admission_wait_timeout_ms=0,
            browser_backend=backend,
        )
        for _ in range(3):
            session = await repository.get("modal-1")
            assert session is not None
            restored = await restorer.restore(session)
            assert restored.success, (restored.failure, restored.attempts)
            assert restored.identity_match is True
            assert restored.observed_queue_id == queue_id
            assert restored.progress is not None and restored.progress.active_queue is True

        simulator.mismatch_ids.add(queue_id)
        session = await repository.get("modal-1")
        assert session is not None
        mismatch = await restorer.restore(session)
        assert not mismatch.success
        assert mismatch.failure is RestoreFailure.IDENTITY_MISMATCH
        persisted = await repository.get("modal-1")
        assert persisted is not None and persisted.queue_id == queue_id
        assert simulator.new_identities == 1
        assert manager.active_context_count == 0
    finally:
        await manager.shutdown()
        await repository.close()
        await simulator.close()


async def test_contradicting_footer_queue_id_fails_creation_closed(tmp_path: Path) -> None:
    simulator = LocalQueueSimulator(
        new_identity_prefix="sim-conflict",
        layout="modal",
        forced_new_ids=["sim-conflict-1"],
        crosscheck_conflict_ids={"sim-conflict-1"},
    )
    await simulator.start()
    repository = SQLiteSessionRepository(tmp_path / "conflict.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    manager = await _manager(BrowserBackendName.CHROME)
    try:
        creator = QueueSessionCreator(
            browser_manager=manager,
            repository=repository,
            state_store=state_store,
            staging_url=simulator.entry_url,
            state_directory=state_store.directory,
            mode=SessionMode.HYBRID,
            browser_backend=BrowserBackendName.CHROME,
        )
        outcome = await creator.create(CreationWorkItem(sequence=1, session_id="conflict-1"))

        assert outcome.kind is not CreationOutcomeKind.SUCCESS
        assert outcome.failure_code == "invalid_transfer_identity"
        assert await repository.count_successful_queue_ids() == 0
    finally:
        await manager.shutdown()
        await repository.close()
        await simulator.close()
