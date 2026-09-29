"""Admission when the operator only knows the queue page (target = queue page URL).

Uses ``LocalQueueSimulator`` on 127.0.0.1 with synthetic identities; not Queue-it.
"""

from pathlib import Path

import pytest

from queue_load_test.browser import BrowserManager
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
from queue_load_test.transfer import QueueSessionRestorer


@pytest.mark.parametrize("layout", ["classic", "modal"])
@pytest.mark.parametrize("mode", [SessionMode.HYBRID, SessionMode.TRANSFER_ONLY])
async def test_queue_page_target_is_monitored_until_the_visitor_leaves_the_queue(
    tmp_path: Path, layout: str, mode: SessionMode
) -> None:
    simulator = LocalQueueSimulator(new_identity_prefix="sim-unknown", layout=layout)
    await simulator.start()
    target = simulator.queue_url  # the only URL the operator knows
    repository = SQLiteSessionRepository(tmp_path / "unknown.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    manager = BrowserManager(chrome_process_count=1, max_contexts_per_browser=2,
                             max_active_contexts=2)
    await manager.start()
    try:
        creator = QueueSessionCreator(
            browser_manager=manager,
            repository=repository,
            state_store=state_store,
            staging_url=target,
            state_directory=state_store.directory,
            mode=mode,
            browser_backend=BrowserBackendName.CHROME,
        )
        created = await creator.create(CreationWorkItem(sequence=1, session_id="unknown-1"))
        assert created.kind is CreationOutcomeKind.SUCCESS, created.failure_code
        assert created.session is not None and created.session.queue_id is not None
        queue_id = created.session.queue_id
        restorer = QueueSessionRestorer(
            browser_manager=manager,
            repository=repository,
            state_store=state_store,
            admission_detector=AdmissionDetector.from_urls(target),
            storage_navigation_url=target,
            admission_wait_timeout_ms=0,
            browser_backend=BrowserBackendName.CHROME,
        )

        for _ in range(2):
            session = await repository.get("unknown-1")
            assert session is not None
            queuing = await restorer.restore(session)
            assert queuing.success, (queuing.failure, queuing.attempts)
            assert queuing.admitted is False  # the queue page itself is not admission
            assert queuing.observed_queue_id == queue_id
            assert queuing.progress is not None and queuing.progress.active_queue is True

        simulator.admitted_ids.add(queue_id)
        session = await repository.get("unknown-1")
        assert session is not None
        admitted = await restorer.restore(session)
        assert admitted.success and admitted.admitted is True
        persisted = await repository.get("unknown-1")
        assert persisted is not None and persisted.queue_id == queue_id
        assert simulator.new_identities == 1
    finally:
        await manager.shutdown()
        await repository.close()
        await simulator.close()
