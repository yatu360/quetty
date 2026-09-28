"""Controlled Phase 7 park/reopen identity evidence for Patchright and Chrome.

The harness uses the local Queue simulator, real browser contexts, SQLite, and browser
state files.  Its report is deliberately aggregate: Queue IDs, transfer URLs, session
IDs, and storage-state contents never leave the temporary work directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from queue_load_test.browser import BrowserManager, ChromeBackend, PatchrightBackend
from queue_load_test.browser.backend import BrowserBackend
from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.harness.phase6_camoufox_benchmark import main_pids, stats, until
from queue_load_test.models import BrowserBackendName, QueueSession, QueueStatus, SessionMode
from queue_load_test.queue_monitor import AdmissionDetector
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler.creation import (
    CreationOutcomeKind,
    CreationWorkItem,
    QueueSessionCreator,
)
from queue_load_test.state import BrowserState, FileSystemStateStore, StateUnreadableError
from queue_load_test.transfer import (
    QueueSessionRestorer,
    RestoreFailure,
    RestoreMethod,
    SessionRestoreResult,
)

DEFAULT_PATCHRIGHT_CYCLES = 20
DEFAULT_CHROME_CONTROL_CYCLES = 5


class UnreadableStateStore(FileSystemStateStore):
    """Deterministic unavailable-volume simulation without changing file permissions."""

    async def load(self, session_id: str) -> BrowserState | None:
        raise StateUnreadableError(f"Could not read browser state for {session_id!r}")


@dataclass(slots=True)
class RestoreMeasurements:
    durations: list[float] = field(default_factory=list)
    attempts: Counter[str] = field(default_factory=Counter)
    successes: Counter[str] = field(default_factory=Counter)
    failures: Counter[str] = field(default_factory=Counter)
    identity_mismatches: int = 0
    state_refresh_successes: int = 0

    def record(self, result: SessionRestoreResult, duration: float) -> None:
        self.durations.append(duration)
        for attempt in result.attempts:
            method = attempt.method.value
            self.attempts[method] += 1
            if attempt.success:
                self.successes[method] += 1
            else:
                failure = attempt.failure.value if attempt.failure is not None else "UNKNOWN"
                self.failures[f"{method}:{failure}"] += 1
            if attempt.identity_match is False:
                self.identity_mismatches += 1
            if attempt.state_refreshed:
                self.state_refresh_successes += 1

    def aggregate(self) -> dict[str, object]:
        return {
            "operations": len(self.durations),
            "attempts_by_mechanism": dict(sorted(self.attempts.items())),
            "successes_by_mechanism": dict(sorted(self.successes.items())),
            "failures_by_mechanism_and_reason": dict(sorted(self.failures.items())),
            "identity_mismatches": self.identity_mismatches,
            "state_refresh_successes": self.state_refresh_successes,
            "restore_duration_seconds": stats(self.durations),
        }


def _backend(backend: BrowserBackendName) -> BrowserBackend:
    if backend is BrowserBackendName.PATCHRIGHT:
        return PatchrightBackend()
    if backend is BrowserBackendName.CHROME:
        return ChromeBackend()
    raise ValueError("Phase 7 identity evidence is limited to Patchright and Chrome")


async def _manager(backend: BrowserBackendName) -> BrowserManager:
    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=3,
        max_active_contexts=3,
        backend=_backend(backend),
        operation_timeout_seconds=15,
        close_timeout_seconds=5,
    )
    await manager.start()
    return manager


def _restorer(
    manager: BrowserManager,
    repository: SQLiteSessionRepository,
    state_store: FileSystemStateStore,
    simulator: LocalQueueSimulator,
    backend: BrowserBackendName,
    *,
    navigation_timeout_ms: float = 5_000,
) -> QueueSessionRestorer:
    return QueueSessionRestorer(
        browser_manager=manager,
        repository=repository,
        state_store=state_store,
        expected_journey_url=simulator.queue_url,
        storage_navigation_url=simulator.queue_url,
        admission_detector=AdmissionDetector.from_urls(simulator.protected_url),
        navigation_timeout_ms=navigation_timeout_ms,
        admission_wait_timeout_ms=0,
        observation_timeout_seconds=1,
        observation_interval_seconds=0.05,
        browser_backend=backend,
    )


async def _measured_restore(
    measurements: RestoreMeasurements,
    restorer: QueueSessionRestorer,
    session: QueueSession,
    method: RestoreMethod | None = None,
) -> SessionRestoreResult:
    started = time.perf_counter()
    if method is None:
        result = await restorer.restore(session)
    else:
        result = await restorer.restore_with_method(session, method)
    measurements.record(result, time.perf_counter() - started)
    return result


async def _assert_identity(
    repository: SQLiteSessionRepository,
    session_id: str,
    expected_queue_id: str,
    *,
    expected_rows: int = 1,
) -> QueueSession:
    persisted = await repository.get(session_id)
    if persisted is None or persisted.queue_id != expected_queue_id:
        raise AssertionError("persisted Queue identity changed")
    if len(await repository.list()) != expected_rows:
        raise AssertionError("restore created or removed a persisted session")
    return persisted


async def run_backend_identity_workflow(
    backend: BrowserBackendName,
    *,
    cycles: int,
    include_failure_matrix: bool,
) -> dict[str, object]:
    """Run one production-shaped backend workflow and return sanitized evidence."""

    if cycles < 2:
        raise ValueError("cycles must be at least 2")
    backend = BrowserBackendName.parse(backend)
    baseline_pids = main_pids(backend)
    measurements = RestoreMeasurements()
    cleanup_checks = 0
    cleanup_failures = 0
    process_restart_success = False
    application_restart_success = False
    failure_results: dict[str, str] = {}
    replacements = 0
    simulator = LocalQueueSimulator(new_identity_prefix=f"phase7-{backend.value.lower()}")
    await simulator.start()

    with tempfile.TemporaryDirectory(prefix=f"phase7-{backend.value.lower()}-") as temp:
        root = Path(temp)
        database = root / "sessions.sqlite3"
        state_directory = root / "state"
        repository = SQLiteSessionRepository(database)
        state_store = FileSystemStateStore(state_directory)
        manager = await _manager(backend)
        try:
            creator = QueueSessionCreator(
                browser_manager=manager,
                repository=repository,
                state_store=state_store,
                staging_url=simulator.entry_url,
                state_directory=state_directory,
                mode=SessionMode.HYBRID,
                browser_backend=backend,
            )
            creation_started = time.perf_counter()
            outcome = await creator.create(
                CreationWorkItem(sequence=1, session_id="phase7-identity")
            )
            creation_duration = time.perf_counter() - creation_started
            if outcome.kind is not CreationOutcomeKind.SUCCESS or outcome.session is None:
                raise AssertionError(f"identity acquisition failed: {outcome.failure_code}")
            expected_queue_id = outcome.session.queue_id
            if expected_queue_id is None:
                raise AssertionError("creator persisted no Queue ID")
            if manager.active_context_count != 0:
                raise AssertionError("creation context was not returned")
            cleanup_checks += 1

            restorer = _restorer(manager, repository, state_store, simulator, backend)
            session = await _assert_identity(repository, outcome.session.session_id, expected_queue_id)

            transfer = await _measured_restore(
                measurements, restorer, session, RestoreMethod.TRANSFER
            )
            if not transfer.success or transfer.identity_match is not True:
                raise AssertionError("explicit transfer restoration failed")
            cleanup_checks += 1
            cleanup_failures += manager.active_context_count != 0

            session = await _assert_identity(repository, session.session_id, expected_queue_id)
            storage = await _measured_restore(
                measurements, restorer, session, RestoreMethod.STORAGE_STATE
            )
            if not storage.success or storage.identity_match is not True:
                raise AssertionError("explicit storage-state restoration failed")
            cleanup_checks += 1
            cleanup_failures += manager.active_context_count != 0

            process_restart_at = cycles // 3
            application_restart_at = (cycles * 2) // 3
            successful_cycles = 0
            for cycle in range(cycles):
                if cycle == process_restart_at:
                    await manager.shutdown()
                    if manager.active_context_count != 0 or manager.managed_process_count != 0:
                        raise AssertionError("browser process restart did not cleanly stop")
                    manager = await _manager(backend)
                    restorer = _restorer(manager, repository, state_store, simulator, backend)
                    process_restart_success = True
                if cycle == application_restart_at:
                    await repository.close()
                    repository = SQLiteSessionRepository(database)
                    state_store = FileSystemStateStore(state_directory)
                    restorer = _restorer(manager, repository, state_store, simulator, backend)
                    application_restart_success = True

                session = await _assert_identity(
                    repository, outcome.session.session_id, expected_queue_id
                )
                result = await _measured_restore(measurements, restorer, session)
                if (
                    not result.success
                    or result.identity_match is not True
                    or not result.state_refreshed
                ):
                    raise AssertionError(
                        f"park/reopen cycle {cycle + 1} failed: {result.failure}"
                    )
                successful_cycles += 1
                await _assert_identity(repository, session.session_id, expected_queue_id)
                cleanup_checks += 1
                cleanup_failures += manager.active_context_count != 0

            fallback_session = await _assert_identity(
                repository, outcome.session.session_id, expected_queue_id
            )
            simulator.transfer_down_ids.add(expected_queue_id)
            fallback = await _measured_restore(
                measurements, restorer, fallback_session
            )
            simulator.transfer_down_ids.discard(expected_queue_id)
            if (
                not fallback.success
                or fallback.method is not RestoreMethod.STORAGE_STATE
                or fallback.identity_match is not True
                or not fallback.state_refreshed
                or [attempt.method for attempt in fallback.attempts]
                != [RestoreMethod.TRANSFER, RestoreMethod.STORAGE_STATE]
            ):
                raise AssertionError("HYBRID transfer-first storage fallback failed")
            cleanup_checks += 1
            cleanup_failures += manager.active_context_count != 0

            if include_failure_matrix:
                good_state = await state_store.load(fallback_session.session_id)
                if good_state is None:
                    raise AssertionError("verified restore did not leave browser state")

                async def record_failure(
                    name: str,
                    result: SessionRestoreResult,
                    expected: RestoreFailure,
                    *,
                    rows: int = 1,
                ) -> None:
                    nonlocal cleanup_checks, cleanup_failures
                    if result.success or result.failure is not expected:
                        raise AssertionError(f"{name} returned {result.failure}, expected {expected}")
                    failure_results[name] = result.failure.value
                    await _assert_identity(
                        repository,
                        fallback_session.session_id,
                        expected_queue_id,
                        expected_rows=rows,
                    )
                    cleanup_checks += 1
                    cleanup_failures += manager.active_context_count != 0

                simulator.transfer_down_ids.add(expected_queue_id)
                await state_store.delete(fallback_session.session_id)
                current = await _assert_identity(
                    repository, fallback_session.session_id, expected_queue_id
                )
                result = await _measured_restore(measurements, restorer, current)
                await record_failure("missing_state", result, RestoreFailure.STATE_MISSING)
                await state_store.save(fallback_session.session_id, good_state)

                state_store.path_for(fallback_session.session_id).write_text(
                    "controlled-corrupt-state", encoding="utf-8"
                )
                current = await _assert_identity(
                    repository, fallback_session.session_id, expected_queue_id
                )
                result = await _measured_restore(measurements, restorer, current)
                await record_failure("corrupt_state", result, RestoreFailure.STATE_CORRUPT)
                await state_store.save(fallback_session.session_id, good_state)

                unreadable = UnreadableStateStore(state_directory)
                unavailable_restorer = _restorer(
                    manager, repository, unreadable, simulator, backend
                )
                current = await _assert_identity(
                    repository, fallback_session.session_id, expected_queue_id
                )
                result = await _measured_restore(
                    measurements, unavailable_restorer, current
                )
                await record_failure(
                    "unavailable_state", result, RestoreFailure.STATE_UNAVAILABLE
                )
                simulator.transfer_down_ids.discard(expected_queue_id)

                current = await _assert_identity(
                    repository, fallback_session.session_id, expected_queue_id
                )
                simulator.transfer_down_ids.add(expected_queue_id)
                result = await _measured_restore(
                    measurements, restorer, current, RestoreMethod.TRANSFER
                )
                await record_failure("transfer_failure", result, RestoreFailure.HTTP_FAILURE)
                simulator.transfer_down_ids.discard(expected_queue_id)

                current = await _assert_identity(
                    repository, fallback_session.session_id, expected_queue_id
                )
                simulator.mismatch_ids.add(expected_queue_id)
                result = await _measured_restore(
                    measurements, restorer, current, RestoreMethod.TRANSFER
                )
                await record_failure("identity_mismatch", result, RestoreFailure.IDENTITY_MISMATCH)
                simulator.mismatch_ids.discard(expected_queue_id)

                foreign = QueueSession(
                    session_id="phase7-foreign-provenance",
                    queue_id="phase7-foreign-identity",
                    transfer_url=simulator.transfer_url("phase7-foreign-identity"),
                    mode=SessionMode.HYBRID,
                    browser_backend=(
                        BrowserBackendName.CHROME
                        if backend is BrowserBackendName.PATCHRIGHT
                        else BrowserBackendName.PATCHRIGHT
                    ),
                    status=QueueStatus.PARKED,
                    state_path=state_directory / "phase7-foreign-provenance.json",
                )
                await repository.create(foreign)
                before_contexts = manager.active_context_count
                result = await _measured_restore(measurements, restorer, foreign)
                if result.failure is not RestoreFailure.BACKEND_MISMATCH:
                    raise AssertionError("backend provenance mismatch was not rejected")
                if manager.active_context_count != before_contexts:
                    raise AssertionError("provenance mismatch opened a context")
                persisted_foreign = await repository.get(foreign.session_id)
                if (
                    persisted_foreign is None
                    or persisted_foreign.queue_id != foreign.queue_id
                    or persisted_foreign.browser_backend is not foreign.browser_backend
                ):
                    raise AssertionError("provenance mismatch changed its persisted session")
                failure_results["backend_provenance_mismatch"] = result.failure.value
                cleanup_checks += 1

                await manager.shutdown()
                current = await _assert_identity(
                    repository,
                    fallback_session.session_id,
                    expected_queue_id,
                    expected_rows=2,
                )
                result = await _measured_restore(
                    measurements, restorer, current, RestoreMethod.TRANSFER
                )
                await record_failure(
                    "context_creation_failure",
                    result,
                    RestoreFailure.NAVIGATION_FAILED,
                    rows=2,
                )
                manager = await _manager(backend)
                restorer = _restorer(manager, repository, state_store, simulator, backend)

                simulator.slow_seconds = 0.5
                simulator.slow_ids.add(expected_queue_id)
                timeout_restorer = _restorer(
                    manager,
                    repository,
                    state_store,
                    simulator,
                    backend,
                    navigation_timeout_ms=50,
                )
                current = await _assert_identity(
                    repository,
                    fallback_session.session_id,
                    expected_queue_id,
                    expected_rows=2,
                )
                result = await _measured_restore(
                    measurements, timeout_restorer, current, RestoreMethod.TRANSFER
                )
                await record_failure(
                    "navigation_timeout",
                    result,
                    RestoreFailure.NAVIGATION_FAILED,
                    rows=2,
                )
                simulator.slow_ids.discard(expected_queue_id)

                final_session = await _assert_identity(
                    repository,
                    fallback_session.session_id,
                    expected_queue_id,
                    expected_rows=2,
                )
                final_restore = await _measured_restore(
                    measurements, restorer, final_session
                )
                if not final_restore.success or not final_restore.state_refreshed:
                    raise AssertionError("session did not recover after injected failures")
                cleanup_checks += 1
                cleanup_failures += manager.active_context_count != 0

            if simulator.new_identities != 1:
                replacements = simulator.new_identities - 1
                raise AssertionError("a restore acquired a replacement Queue identity")

            package = manager.backend_diagnostics()
            report: dict[str, object] = {
                "backend": backend.value,
                "browser_package_version": package.package_version,
                "browser_build": package.browser_version,
                "fresh_context_creation": "PASS",
                "identity_acquisition": "PASS",
                "creation_duration_seconds": round(creation_duration, 4),
                "explicit_transfer_restore": "PASS",
                "explicit_storage_state_restore": "PASS",
                "hybrid_transfer_first_fallback": "PASS",
                "park_reopen": {
                    "attempted_cycles": cycles,
                    "successful_cycles": successful_cycles,
                },
                "process_restart": "PASS" if process_restart_success else "NOT_RUN",
                "application_repository_restart": (
                    "PASS" if application_restart_success else "NOT_RUN"
                ),
                "failure_scenarios": dict(sorted(failure_results.items())),
                "persisted_identity_changes": 0,
                "replacement_identities_created": replacements,
                "context_cleanup": {
                    "checks": cleanup_checks,
                    "failures": cleanup_failures,
                    "active_contexts_before_shutdown": manager.active_context_count,
                },
                "restoration": measurements.aggregate(),
            }
        finally:
            await manager.shutdown()
            await repository.close()
            await simulator.close()

    leaked_pids = main_pids(backend) - baseline_pids
    if leaked_pids:
        await until(lambda: not (main_pids(backend) - baseline_pids), timeout=5)
        leaked_pids = main_pids(backend) - baseline_pids
    cast(dict[str, Any], report)["resource_cleanup"] = {
        "active_contexts_after_shutdown": manager.active_context_count,
        "managed_processes_after_shutdown": manager.managed_process_count,
        "new_browser_processes_after_shutdown": len(leaked_pids),
    }
    if cleanup_failures or leaked_pids or manager.managed_process_count:
        raise AssertionError("browser contexts or processes leaked")
    return report


async def run_identity_evidence(
    *,
    patchright_cycles: int = DEFAULT_PATCHRIGHT_CYCLES,
    chrome_cycles: int = DEFAULT_CHROME_CONTROL_CYCLES,
) -> dict[str, object]:
    """Run required Patchright evidence followed by a smaller Chrome control."""

    started = time.perf_counter()
    patchright = await run_backend_identity_workflow(
        BrowserBackendName.PATCHRIGHT,
        cycles=patchright_cycles,
        include_failure_matrix=True,
    )
    chrome = await run_backend_identity_workflow(
        BrowserBackendName.CHROME,
        cycles=chrome_cycles,
        include_failure_matrix=False,
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "local_queue_simulator_only",
        "staging": "NOT_RUN_UNKNOWN",
        "decision": "TEMPORARY_CONTEXTS_PASS",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "patchright": patchright,
        "chrome_control": chrome,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patchright-cycles", type=int, default=DEFAULT_PATCHRIGHT_CYCLES)
    parser.add_argument("--chrome-cycles", type=int, default=DEFAULT_CHROME_CONTROL_CYCLES)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report = asyncio.run(
        run_identity_evidence(
            patchright_cycles=args.patchright_cycles,
            chrome_cycles=args.chrome_cycles,
        )
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":  # pragma: no cover
    main()
