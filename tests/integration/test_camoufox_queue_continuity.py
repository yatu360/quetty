"""Camoufox Queue-ID continuity using only the local Queue simulator."""

from pathlib import Path

import pytest

from queue_load_test.browser import BrowserBackendSetupError, BrowserManager, CamoufoxBackend
from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.models import BrowserBackendName, SessionMode
from queue_load_test.queue_monitor import AdmissionDetector
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler.creation import (
    CreationOutcomeKind,
    CreationWorkItem,
    QueueSessionCreator,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import QueueSessionRestorer, RestoreFailure


async def _manager() -> BrowserManager:
    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=5,
        max_active_contexts=5,
        backend=CamoufoxBackend(),
    )
    try:
        await manager.start()
    except BrowserBackendSetupError as exc:
        pytest.skip(str(exc))
    return manager


async def test_camoufox_queue_id_survives_twenty_park_reopen_cycles_and_restart(
    tmp_path: Path,
) -> None:
    simulator = LocalQueueSimulator(new_identity_prefix="sim-camoufox")
    await simulator.start()
    database = tmp_path / "sessions.sqlite3"
    state_directory = tmp_path / "state"
    repository = SQLiteSessionRepository(database)
    state_store = FileSystemStateStore(state_directory)
    manager = await _manager()

    try:
        creator = QueueSessionCreator(
            browser_manager=manager,
            repository=repository,
            state_store=state_store,
            staging_url=simulator.entry_url,
            state_directory=state_directory,
            mode=SessionMode.HYBRID,
            browser_backend=BrowserBackendName.CAMOUFOX,
        )
        created = await creator.create(CreationWorkItem(sequence=1, session_id="camoufox-1"))
        assert created.kind is CreationOutcomeKind.SUCCESS
        assert created.session is not None
        expected_queue_id = created.session.queue_id
        assert expected_queue_id is not None
        assert created.session.browser_backend is BrowserBackendName.CAMOUFOX
        assert manager.active_context_count == 0

        for cycle in range(20):
            if cycle == 10:
                await manager.shutdown()
                await repository.close()
                repository = SQLiteSessionRepository(database)
                state_store = FileSystemStateStore(state_directory)
                manager = await _manager()

            session = await repository.get("camoufox-1")
            assert session is not None
            restorer = QueueSessionRestorer(
                browser_manager=manager,
                repository=repository,
                state_store=state_store,
                admission_detector=AdmissionDetector.from_urls(simulator.entry_url),
                storage_navigation_url=simulator.entry_url,
                admission_wait_timeout_ms=0,
                browser_backend=BrowserBackendName.CAMOUFOX,
            )
            restored = await restorer.restore(session)

            assert restored.success, (cycle, restored.failure, restored.attempts)
            assert restored.identity_match is True
            assert restored.observed_queue_id == expected_queue_id
            assert restored.state_refreshed
            assert manager.active_context_count == 0
            persisted = await repository.get("camoufox-1")
            assert persisted is not None
            assert persisted.queue_id == expected_queue_id
            assert persisted.browser_backend is BrowserBackendName.CAMOUFOX

        simulator.mismatch_ids.add(expected_queue_id)
        session = await repository.get("camoufox-1")
        assert session is not None
        mismatch = await restorer.restore(session)
        assert not mismatch.success
        assert mismatch.failure is RestoreFailure.IDENTITY_MISMATCH
        assert mismatch.identity_match is False
        persisted = await repository.get("camoufox-1")
        assert persisted is not None
        assert persisted.queue_id == expected_queue_id
        assert manager.active_context_count == 0
    finally:
        await manager.shutdown()
        await repository.close()
        await simulator.close()


async def test_camoufox_transfer_only_restores_without_persisted_storage_state(
    tmp_path: Path,
) -> None:
    simulator = LocalQueueSimulator(new_identity_prefix="sim-transfer-only")
    await simulator.start()
    repository = SQLiteSessionRepository(tmp_path / "transfer-only.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    manager = await _manager()
    try:
        creator = QueueSessionCreator(
            browser_manager=manager,
            repository=repository,
            state_store=state_store,
            staging_url=simulator.entry_url,
            state_directory=state_store.directory,
            mode=SessionMode.TRANSFER_ONLY,
            browser_backend=BrowserBackendName.CAMOUFOX,
        )
        created = await creator.create(
            CreationWorkItem(sequence=1, session_id="transfer-only")
        )
        assert created.kind is CreationOutcomeKind.SUCCESS
        assert created.session is not None
        assert not state_store.path_for(created.session.session_id).exists()

        restorer = QueueSessionRestorer(
            browser_manager=manager,
            repository=repository,
            state_store=state_store,
            admission_detector=AdmissionDetector.from_urls(simulator.entry_url),
            storage_navigation_url=simulator.entry_url,
            admission_wait_timeout_ms=0,
            browser_backend=BrowserBackendName.CAMOUFOX,
        )
        restored = await restorer.restore(created.session)

        assert restored.success
        assert restored.identity_match is True
        assert restored.observed_queue_id == created.session.queue_id
        assert not restored.state_refreshed
        assert not state_store.path_for(created.session.session_id).exists()
        assert manager.active_context_count == 0
    finally:
        await manager.shutdown()
        await repository.close()
        await simulator.close()
