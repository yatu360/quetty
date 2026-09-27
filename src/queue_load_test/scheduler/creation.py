"""Bounded Queue-it session creation and target acquisition."""

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, Self, cast
from uuid import uuid4

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from queue_load_test.browser import BrowserManager, BrowserManagerError
from queue_load_test.config import Settings
from queue_load_test.metrics.logging import log_event
from queue_load_test.metrics.prometheus import PrometheusMetrics
from queue_load_test.models import (
    BrowserBackendName,
    QueueProgress,
    QueueSession,
    QueueStatus,
    SessionMode,
)
from queue_load_test.queue_monitor import QueueItLiveStateExtractor
from queue_load_test.repository import QueueIdConflictError, SessionRepository
from queue_load_test.state import BrowserState, StateStore, StateStoreError
from queue_load_test.transfer import (
    QueueItTransferExtractor,
    TransferExtractionResult,
    TransferFailure,
)

type Sleep = Callable[[float], Awaitable[None]]
type Jitter = Callable[[float, float], float]

logger = logging.getLogger(__name__)


class CreationOutcomeKind(StrEnum):
    SUCCESS = "SUCCESS"
    DUPLICATE = "DUPLICATE"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


class TransientCreationError(RuntimeError):
    """A sanitized failure that can reasonably succeed on retry."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PermanentCreationError(RuntimeError):
    """A sanitized failure that should not be retried."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CreationRetryPolicy:
    max_attempts: int = 3
    initial_backoff_seconds: float = 0.25
    maximum_backoff_seconds: float = 5.0
    jitter_seconds: float = 0.1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.initial_backoff_seconds < 0 or self.maximum_backoff_seconds < 0:
            raise ValueError("backoff values cannot be negative")
        if self.maximum_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("maximum_backoff_seconds cannot be less than initial backoff")
        if self.jitter_seconds < 0:
            raise ValueError("jitter_seconds cannot be negative")

    def delay(self, failed_attempt: int, jitter: Jitter) -> float:
        exponential: float = min(
            self.initial_backoff_seconds * (2.0 ** max(0, failed_attempt - 1)),
            self.maximum_backoff_seconds,
        )
        return exponential + jitter(0.0, self.jitter_seconds)


@dataclass(frozen=True, slots=True)
class CreationWorkItem:
    sequence: int
    session_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True, slots=True)
class CreationOutcome:
    kind: CreationOutcomeKind
    attempts: int
    temporary_failures: int
    duration_seconds: float
    session: QueueSession | None = None
    progress: QueueProgress | None = None
    failure_code: str | None = None


class SessionCreationHandler(Protocol):
    async def create(self, work_item: CreationWorkItem) -> CreationOutcome: ...


class TransferExtractor(Protocol):
    async def extract(
        self,
        page: Page,
        *,
        expected_queue_id: str | None = None,
    ) -> TransferExtractionResult: ...


type TransferExtractorFactory = Callable[[str], TransferExtractor]


@dataclass(slots=True)
class CreationMetrics:
    attempts: int = 0
    completed_work_items: int = 0
    initial_successful_unique_ids: int = 0
    successful_unique_ids: int = 0
    unique_ids_acquired: int = 0
    duplicates: int = 0
    temporary_failures: int = 0
    temporary_failure_outcomes: int = 0
    permanent_failures: int = 0
    retries: int = 0
    currently_creating: int = 0
    maximum_concurrent_creating: int = 0
    queue_depth: int = 0
    maximum_queue_depth: int = 0
    total_creation_duration_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    lost_queue_ids: int = 0
    effective_target: int = 0
    replacement_blocked: bool = False
    worker_completed: dict[int, int] = field(default_factory=dict)
    worker_successes: dict[int, int] = field(default_factory=dict)
    worker_failures: dict[int, int] = field(default_factory=dict)

    @property
    def sessions_created_per_second(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.unique_ids_acquired / self.elapsed_seconds


class QueueSessionCreator:
    """Create and persist one independent Queue-it session with bounded retries."""

    def __init__(
        self,
        *,
        browser_manager: BrowserManager,
        repository: SessionRepository,
        state_store: StateStore,
        staging_url: str,
        state_directory: Path,
        mode: SessionMode,
        browser_backend: BrowserBackendName = BrowserBackendName.CHROME,
        live_extractor: QueueItLiveStateExtractor | None = None,
        transfer_extractor_factory: TransferExtractorFactory = QueueItTransferExtractor,
        retry_policy: CreationRetryPolicy | None = None,
        navigation_timeout_ms: float = 30_000,
        live_page_timeout_seconds: float = 30.0,
        observation_interval_seconds: float = 0.25,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter = random.uniform,
        observability: PrometheusMetrics | None = None,
    ) -> None:
        self._browser_manager = browser_manager
        self._repository = repository
        self._state_store = state_store
        self._staging_url = staging_url
        self._state_directory = state_directory
        self._mode = SessionMode.parse(mode)
        self._browser_backend = BrowserBackendName.parse(browser_backend)
        self._live_extractor = live_extractor or QueueItLiveStateExtractor()
        self._transfer_extractor_factory = transfer_extractor_factory
        self._retry_policy = retry_policy or CreationRetryPolicy()
        self._navigation_timeout_ms = navigation_timeout_ms
        self._live_page_timeout_seconds = live_page_timeout_seconds
        self._observation_interval_seconds = observation_interval_seconds
        self._sleep = sleep
        self._jitter = jitter
        self._observability = observability
        self._attempt_timeout_seconds = (
            navigation_timeout_ms / 1_000 + live_page_timeout_seconds + 10.0
        )

    async def create(self, work_item: CreationWorkItem) -> CreationOutcome:
        outcome = await self._create(work_item)
        if self._observability is not None:
            if outcome.kind is CreationOutcomeKind.SUCCESS:
                self._observability.record_creation_success(outcome.duration_seconds)
            else:
                self._observability.record_creation_failure(outcome.duration_seconds)
        log_event(
            logger,
            logging.INFO if outcome.kind is CreationOutcomeKind.SUCCESS else logging.WARNING,
            "session_creation_completed",
            session_id=work_item.session_id,
            queue_id=outcome.session.queue_id if outcome.session is not None else None,
            status=outcome.session.status.value if outcome.session is not None else None,
            attempt=outcome.attempts,
            duration=outcome.duration_seconds,
            error_type=outcome.failure_code,
        )
        return outcome

    async def _create(self, work_item: CreationWorkItem) -> CreationOutcome:
        started = time.perf_counter()
        temporary_failures = 0
        last_failure = "creation_failed"
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            if self._observability is not None:
                self._observability.record_creation_attempt()
            log_event(
                logger,
                logging.INFO,
                "session_creation_attempt",
                session_id=work_item.session_id,
                attempt=attempt,
            )
            try:
                # A Playwright call can stay pending forever when Chrome dies mid-call;
                # the builtin TimeoutError is an OSError and is retried as transient.
                async with asyncio.timeout(self._attempt_timeout_seconds):
                    return await self._attempt(work_item, attempt, started, temporary_failures)
            except PermanentCreationError as exc:
                if self._observability is not None:
                    self._observability.record_creation_permanent_failure()
                await self._persist_failed(work_item, attempt, exc.code)
                return CreationOutcome(
                    kind=CreationOutcomeKind.PERMANENT_FAILURE,
                    attempts=attempt,
                    temporary_failures=temporary_failures,
                    duration_seconds=time.perf_counter() - started,
                    failure_code=exc.code,
                )
            except (
                TransientCreationError,
                PlaywrightTimeoutError,
                PlaywrightError,
                BrowserManagerError,
                OSError,
            ) as exc:
                temporary_failures += 1
                if self._observability is not None:
                    self._observability.record_creation_transient_failure()
                last_failure = (
                    exc.code
                    if isinstance(exc, TransientCreationError)
                    else "transient_browser_error"
                )
                if attempt < self._retry_policy.max_attempts:
                    if self._observability is not None:
                        self._observability.record_creation_retry()
                    await self._sleep(self._retry_policy.delay(attempt, self._jitter))

        await self._persist_failed(work_item, self._retry_policy.max_attempts, last_failure)
        return CreationOutcome(
            kind=CreationOutcomeKind.TEMPORARY_FAILURE,
            attempts=self._retry_policy.max_attempts,
            temporary_failures=temporary_failures,
            duration_seconds=time.perf_counter() - started,
            failure_code=last_failure,
        )

    async def _attempt(
        self,
        work_item: CreationWorkItem,
        attempt: int,
        started: float,
        temporary_failures: int,
    ) -> CreationOutcome:
        state_saved = False
        state_path = self._state_directory / f"{work_item.session_id}.json"
        async with self._browser_manager.context() as context:
            page = await context.new_page()
            navigation_started = time.perf_counter()
            try:
                response = await page.goto(
                    self._staging_url,
                    wait_until="domcontentloaded",
                    timeout=self._navigation_timeout_ms,
                )
                self._browser_manager.report_navigation(context, responsive=True)
            except PlaywrightTimeoutError:
                self._browser_manager.report_navigation(context, responsive=False)
                if self._observability is not None:
                    self._observability.record_navigation_failure(timed_out=True)
                raise
            except PlaywrightError:
                if self._observability is not None:
                    self._observability.record_navigation_failure()
                raise
            finally:
                if self._observability is not None:
                    self._observability.record_navigation_duration(
                        time.perf_counter() - navigation_started
                    )
            if response is not None:
                if response.status >= 400 and self._observability is not None:
                    self._observability.record_navigation_failure()
                if response.status >= 500 or response.status in {408, 429}:
                    raise TransientCreationError("temporary_http_response")
                if response.status >= 400:
                    raise PermanentCreationError("permanent_http_response")

            progress, transfer = await self._wait_for_live_queue(page, work_item.session_id)
            queue_id = transfer.queue_id
            if queue_id is None or transfer.transfer_url is None:
                raise PermanentCreationError("queue_identity_missing")

            if self._mode is SessionMode.HYBRID:
                storage_state = cast(BrowserState, await context.storage_state())
                try:
                    state_path = await self._state_store.save(
                        work_item.session_id,
                        storage_state,
                    )
                except (OSError, StateStoreError) as exc:
                    if self._observability is not None:
                        self._observability.record_state_persistence_failure()
                    raise TransientCreationError("state_persistence_failed") from exc
                except asyncio.CancelledError:
                    # The write completed before cancellation propagated; no session
                    # row references it yet, so remove it instead of orphaning it.
                    with contextlib.suppress(Exception):
                        await self._state_store.delete(work_item.session_id)
                    raise
                state_saved = True

            observed_at = datetime.now(UTC)
            session = QueueSession(
                session_id=work_item.session_id,
                queue_id=queue_id,
                transfer_url=transfer.transfer_url,
                mode=self._mode,
                browser_backend=self._browser_backend,
                status=QueueStatus.PARKED,
                state_path=state_path,
                created_at=observed_at,
                last_checked_at=observed_at,
                last_queue_update=progress.last_updated_at,
                last_progress_change_at=observed_at,
                next_check_at=observed_at,
                attempt_count=attempt,
            )
            try:
                await self._repository.create(session, progress)
            except QueueIdConflictError:
                if state_saved:
                    await self._state_store.delete(work_item.session_id)
                if self._observability is not None:
                    self._observability.record_creation_duplicate()
                await self._persist_failed(work_item, attempt, "duplicate_queue_id")
                return CreationOutcome(
                    kind=CreationOutcomeKind.DUPLICATE,
                    attempts=attempt,
                    temporary_failures=temporary_failures,
                    duration_seconds=time.perf_counter() - started,
                    failure_code="duplicate_queue_id",
                )
            except BaseException:
                # A cancelled create may still have committed in its worker thread.
                # Deleting that session's state would strand a valid identity.
                if state_saved and not await self._session_committed(work_item.session_id):
                    await self._state_store.delete(work_item.session_id)
                raise
            return CreationOutcome(
                kind=CreationOutcomeKind.SUCCESS,
                attempts=attempt,
                temporary_failures=temporary_failures,
                duration_seconds=time.perf_counter() - started,
                session=session,
                progress=progress,
            )

    async def _session_committed(self, session_id: str) -> bool:
        """Return whether the session row exists; assume it does when unknowable."""

        try:
            return await self._repository.get(session_id) is not None
        except Exception:  # noqa: BLE001 - keep state rather than risk stranding it
            return True

    async def _wait_for_live_queue(
        self,
        page: Page,
        session_id: str,
    ) -> tuple[QueueProgress, TransferExtractionResult]:
        deadline = asyncio.get_running_loop().time() + self._live_page_timeout_seconds
        last_transfer_failure: TransferFailure | None = None
        while True:
            progress = await self._live_extractor.extract(page, session_id=session_id)
            if progress.pre_queue is True or progress.active_queue is True:
                transfer = await self._transfer_extractor_factory(page.url).extract(page)
                if transfer.successful:
                    return progress, transfer
                last_transfer_failure = transfer.failure
                if last_transfer_failure in {
                    TransferFailure.MALFORMED_URL,
                    TransferFailure.UNEXPECTED_HOST,
                    TransferFailure.UNEXPECTED_JOURNEY,
                    TransferFailure.AMBIGUOUS_QUEUE_ID,
                    TransferFailure.IDENTITY_MISMATCH,
                }:
                    raise PermanentCreationError("invalid_transfer_identity")
            if asyncio.get_running_loop().time() >= deadline:
                if last_transfer_failure is TransferFailure.QUEUE_ID_MISSING:
                    raise PermanentCreationError("queue_identity_missing")
                raise TransientCreationError("queue_page_not_ready")
            await self._sleep(self._observation_interval_seconds)

    async def _persist_failed(
        self,
        work_item: CreationWorkItem,
        attempts: int,
        failure_code: str,
    ) -> None:
        session = QueueSession(
            session_id=work_item.session_id,
            queue_id=None,
            transfer_url="",
            mode=self._mode,
            browser_backend=self._browser_backend,
            status=QueueStatus.FAILED,
            state_path=self._state_directory / f"{work_item.session_id}.json",
            attempt_count=attempts,
            last_error=failure_code,
        )
        await self._repository.create(session)


class SessionCreationController:
    """Acquire unique Queue IDs using a bounded queue and fixed worker pool."""

    def __init__(
        self,
        *,
        repository: SessionRepository,
        handler: SessionCreationHandler,
        target_queue_ids: int,
        worker_count: int,
        queue_capacity: int | None = None,
        observability: PrometheusMetrics | None = None,
        identity_replacement_limit: int | None = None,
    ) -> None:
        if identity_replacement_limit is not None and identity_replacement_limit < 0:
            raise ValueError("identity_replacement_limit cannot be negative")
        if target_queue_ids < 0:
            raise ValueError("target_queue_ids cannot be negative")
        if worker_count < 1:
            raise ValueError("worker_count must be at least 1")
        if queue_capacity is not None and queue_capacity < 1:
            raise ValueError("queue_capacity must be at least 1")
        self._repository = repository
        self._handler = handler
        self._target = target_queue_ids
        self._worker_count = worker_count
        self._queue_capacity = queue_capacity or worker_count
        self._identity_replacement_limit = identity_replacement_limit
        self.metrics = CreationMetrics()
        self._observability = observability
        if observability is not None:
            observability.set_target(target_queue_ids)

    def adjust_target(self, delta: int) -> None:
        """Keep a still-running acquisition loop aligned with operator add/delete."""

        self._target = max(0, self._target + delta)
        if self._observability is not None:
            self._observability.set_target(self._target)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        repository: SessionRepository,
        handler: SessionCreationHandler,
        observability: PrometheusMetrics | None = None,
    ) -> Self:
        return cls(
            repository=repository,
            handler=handler,
            target_queue_ids=settings.target_queue_ids,
            worker_count=settings.creation_workers,
            queue_capacity=settings.creation_queue_capacity,
            observability=observability,
            identity_replacement_limit=settings.identity_replacement_limit,
        )

    async def run(self, stop_event: asyncio.Event | None = None) -> CreationMetrics:
        started = time.perf_counter()
        work_queue: asyncio.Queue[CreationWorkItem | None] = asyncio.Queue(
            maxsize=self._queue_capacity
        )
        result_queue: asyncio.Queue[tuple[int, CreationOutcome]] = asyncio.Queue(
            maxsize=self._worker_count
        )
        workers = [
            asyncio.create_task(
                self._worker(index, work_queue, result_queue),
                name=f"creation-worker-{index}",
            )
            for index in range(self._worker_count)
        ]
        sequence = 0
        in_flight = 0
        try:
            existing = await self._repository.count_successful_queue_ids()
            self.metrics.initial_successful_unique_ids = existing
            self.metrics.successful_unique_ids = existing
            target = await self._effective_target()
            self._set_creation_activity(work_queue)
            while not (stop_event is not None and stop_event.is_set()):
                while self.metrics.successful_unique_ids < target and not (
                    stop_event is not None and stop_event.is_set()
                ):
                    deficit = target - self.metrics.successful_unique_ids
                    desired_in_flight = min(self._worker_count, deficit)
                    while in_flight < desired_in_flight:
                        sequence += 1
                        await work_queue.put(CreationWorkItem(sequence=sequence))
                        in_flight += 1
                        self._set_creation_activity(work_queue)

                    worker_index, outcome = await result_queue.get()
                    in_flight -= 1
                    self._record(outcome, worker_index=worker_index)
                    if self._observability is not None:
                        elapsed = max(time.perf_counter() - started, 1e-9)
                        self._observability.set_creation_rate(
                            self.metrics.unique_ids_acquired / elapsed
                        )

                # Successful outcomes are persisted by the handler, so they can drive
                # the hot loop without an O(target) sequence of COUNT queries. Verify
                # against SQLite before declaring the target complete.
                self.metrics.successful_unique_ids = (
                    await self._repository.count_successful_queue_ids()
                )
                target = await self._effective_target()
                if self.metrics.successful_unique_ids >= target or (
                    stop_event is not None and stop_event.is_set()
                ):
                    break

            if in_flight:
                for _ in range(in_flight):
                    worker_index, outcome = await result_queue.get()
                    self._record(outcome, worker_index=worker_index)
                self.metrics.successful_unique_ids = (
                    await self._repository.count_successful_queue_ids()
                )
        except BaseException:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        else:
            for _ in workers:
                await work_queue.put(None)
            await asyncio.gather(*workers)
        finally:
            self.metrics.elapsed_seconds = time.perf_counter() - started
            self.metrics.queue_depth = 0
            if self._observability is not None:
                self._observability.set_creation_activity(in_flight=0, queue_depth=0)
                self._observability.set_creation_rate(
                    self.metrics.sessions_created_per_second
                )
        return self.metrics

    async def _effective_target(self) -> int:
        """Return how many valid IDs may be pursued without mass identity replacement.

        A Queue ID that later becomes FAILED (for example after an identity
        mismatch) no longer counts as valid. Without a limit, the deficit would be
        refilled with brand-new identities; a systemic fault could then replace a
        large part of the population. Replacements are capped by
        ``identity_replacement_limit``; ``None`` keeps the uncapped behavior.
        """

        if self._identity_replacement_limit is None:
            self.metrics.effective_target = self._target
            return self._target
        lost = await self._repository.count_lost_queue_ids()
        effective = min(self._target, self._target + self._identity_replacement_limit - lost)
        blocked = effective < self._target
        if blocked and not self.metrics.replacement_blocked:
            log_event(
                logger,
                logging.ERROR,
                "identity_replacement_limit_reached",
                lost_queue_ids=lost,
                count=self._identity_replacement_limit,
                valid_queue_ids=self.metrics.successful_unique_ids,
            )
        self.metrics.lost_queue_ids = lost
        self.metrics.effective_target = effective
        self.metrics.replacement_blocked = blocked
        if self._observability is not None:
            self._observability.set_identity_replacement_blocked(blocked)
        return effective

    async def _worker(
        self,
        worker_index: int,
        work_queue: asyncio.Queue[CreationWorkItem | None],
        result_queue: asyncio.Queue[tuple[int, CreationOutcome]],
    ) -> None:
        while True:
            work_item = await work_queue.get()
            try:
                if work_item is None:
                    return
                self._set_creation_activity(work_queue)
                self.metrics.currently_creating += 1
                self.metrics.maximum_concurrent_creating = max(
                    self.metrics.maximum_concurrent_creating,
                    self.metrics.currently_creating,
                )
                self._set_creation_activity(work_queue)
                try:
                    try:
                        outcome = await self._handler.create(work_item)
                    except Exception:  # noqa: BLE001 - isolate one failed worker job
                        if self._observability is not None:
                            self._observability.record_creation_transient_failure()
                            self._observability.record_creation_failure(0.0)
                        outcome = CreationOutcome(
                            kind=CreationOutcomeKind.TEMPORARY_FAILURE,
                            attempts=1,
                            temporary_failures=1,
                            duration_seconds=0.0,
                            failure_code="unexpected_creation_error",
                        )
                finally:
                    self.metrics.currently_creating -= 1
                    self._set_creation_activity(work_queue)
                await result_queue.put((worker_index, outcome))
            finally:
                work_queue.task_done()

    def _record(self, outcome: CreationOutcome, *, worker_index: int) -> None:
        self.metrics.completed_work_items += 1
        self.metrics.worker_completed[worker_index] = (
            self.metrics.worker_completed.get(worker_index, 0) + 1
        )
        self.metrics.attempts += outcome.attempts
        self.metrics.retries += max(0, outcome.attempts - 1)
        self.metrics.temporary_failures += outcome.temporary_failures
        self.metrics.total_creation_duration_seconds += outcome.duration_seconds
        if outcome.kind is CreationOutcomeKind.SUCCESS:
            self.metrics.unique_ids_acquired += 1
            self.metrics.successful_unique_ids += 1
            self.metrics.worker_successes[worker_index] = (
                self.metrics.worker_successes.get(worker_index, 0) + 1
            )
        elif outcome.kind is CreationOutcomeKind.DUPLICATE:
            self.metrics.duplicates += 1
        elif outcome.kind is CreationOutcomeKind.PERMANENT_FAILURE:
            self.metrics.permanent_failures += 1
        elif outcome.kind is CreationOutcomeKind.TEMPORARY_FAILURE:
            self.metrics.temporary_failure_outcomes += 1
        if outcome.failure_code == "unexpected_creation_error":
            self.metrics.worker_failures[worker_index] = (
                self.metrics.worker_failures.get(worker_index, 0) + 1
            )

    def _set_creation_activity(
        self,
        work_queue: asyncio.Queue[CreationWorkItem | None],
    ) -> None:
        queue_depth = work_queue.qsize()
        self.metrics.queue_depth = queue_depth
        self.metrics.maximum_queue_depth = max(
            self.metrics.maximum_queue_depth,
            queue_depth,
        )
        if self._observability is not None:
            self._observability.set_creation_activity(
                in_flight=self.metrics.currently_creating,
                queue_depth=queue_depth,
            )
