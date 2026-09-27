"""Phase 6 Camoufox recovery scenarios on a small population (local simulator only)."""

from pathlib import Path

import pytest

from queue_load_test.browser import BrowserBackendSetupError, BrowserManager, CamoufoxBackend
from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.harness.phase4_recovery import Outcome
from queue_load_test.harness.phase6_camoufox_benchmark import (
    PopulationContext,
    scenario_multi_slot_failure,
    scenario_park_reopen,
    scenario_process_failure,
    scenario_restoration_failures,
)
from queue_load_test.models import BrowserBackendName

pytest.importorskip("psutil")


async def _require_camoufox() -> None:
    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=1,
        max_active_contexts=1,
        backend=CamoufoxBackend(),
    )
    try:
        await manager.start()
    except BrowserBackendSetupError as exc:
        pytest.skip(str(exc))
    finally:
        await manager.shutdown()


async def test_camoufox_sessions_survive_browser_kills_and_restore_faults(
    tmp_path: Path,
) -> None:
    await _require_camoufox()
    simulator = LocalQueueSimulator(new_identity_prefix="sim-phase6-test")
    await simulator.start()
    backend = BrowserBackendName.CAMOUFOX
    population = PopulationContext(
        database=tmp_path / "population.sqlite3",
        state_directory=tmp_path / "state",
    )
    try:
        results = [
            await scenario_park_reopen(
                simulator, population, backend=backend, size=3, sweeps=2, processes=2, workers=2
            ),
            await scenario_process_failure(simulator, population, backend=backend),
            await scenario_multi_slot_failure(simulator, population, backend=backend),
            await scenario_restoration_failures(simulator, tmp_path, backend=backend),
        ]
    finally:
        await simulator.close()

    failed = {
        result.key: [name for name, passed in result.checks.items() if not passed]
        for result in results
        if result.outcome is not Outcome.PASS
    }
    assert failed == {}
