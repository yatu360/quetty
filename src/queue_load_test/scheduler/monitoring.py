"""Bounded scheduling and adaptive polling for persisted Queue-it sessions."""

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, Self
from uuid import uuid4

from queue_load_test.config import Settings
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, evaluate_queue_status
from queue_load_test.repository import SessionRepository
from queue_load_test.transfer import SessionRestoreResult

type Clock = Callable[[], datetime]
type Jitter = Callable[[float, float], float]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PollingPolicy:
    """Configuration-driven adaptive monitoring intervals."""

    default_seconds: float = 30.0
    jitter_seconds: float = 5.0
    pre_queue_min_seconds: float = 60.0
    pre_queue_max_seconds: float = 300.0
    active_early_min_seconds: float = 60.0
    active_early_max_seconds: float = 120.0
    active_mid_min_seconds: float = 30.0
    active_mid_max_seconds: float = 60.0
    serviced_soon_min_seconds: float = 10.0
    serviced_soon_max_seconds: float = 30.0
    active_mid_progress_percentage: float = 50.0
    turn_started_seconds: float = 0.0
    stale_update_seconds: float = 180.0

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            default_seconds=settings.queue_poll_seconds,
            jitter_seconds=settings.poll_jitter_seconds,
            pre_queue_min_seconds=settings.pre_queue_poll_min_seconds,
            pre_queue_max_seconds=settings.pre_queue_poll_max_seconds,
            active_early_min_seconds=settings.active_early_poll_min_seconds,
            active_early_max_seconds=settings.active_early_poll_max_seconds,
            active_mid_min_seconds=settings.active_mid_poll_min_seconds,
            active_mid_max_seconds=settings.active_mid_poll_max_seconds,
            serviced_soon_min_seconds=settings.serviced_soon_poll_min_seconds,
            serviced_soon_max_seconds=settings.serviced_soon_poll_max_seconds,
            active_mid_progress_percentage=settings.active_mid_progress_percentage,
            turn_started_seconds=settings.turn_started_poll_seconds,
            stale_update_seconds=settings.stale_update_seconds,
        )

    def interval_seconds(
        self,
        status: QueueStatus,
        progress: QueueProgress | None,
        *,
        jitter: Jitter = random.uniform,
    ) -> float:
        """Choose a useful cadence and spread checks within configured bounds."""

        status = QueueStatus.parse(status)
        if status in {QueueStatus.TURN_STARTED, QueueStatus.READY}:
            return self.turn_started_seconds
        if status is QueueStatus.PRE_QUEUE:
            return self._bounded_interval(
                self.pre_queue_min_seconds,
                self.pre_queue_max_seconds,
                jitter,
            )
        if status is QueueStatus.SERVICED_SOON:
            return self._bounded_interval(
                self.serviced_soon_min_seconds,
                self.serviced_soon_max_seconds,
                jitter,
            )
        if status is QueueStatus.ACTIVE_QUEUE:
            percentage = progress.progress_percentage if progress is not None else None
            if percentage is not None and percentage >= self.active_mid_progress_percentage:
                return self._bounded_interval(
                    self.active_mid_min_seconds,
                    self.active_mid_max_seconds,
                    jitter,
                )
            return self._bounded_interval(
                self.active_early_min_seconds,
                self.active_early_max_seconds,
                jitter,
            )
        if self.jitter_seconds == 0:
            return self.default_seconds
        return max(
            0.0,
            self.default_seconds + jitter(-self.jitter_seconds, self.jitter_seconds),
        )

    def _bounded_interval(self, minimum: float, maximum: float, jitter: Jitter) -> float:
        midpoint = (minimum + maximum) / 2
        if self.jitter_seconds == 0:
            return midpoint
        return min(
            maximum,
            max(minimum, midpoint + jitter(-self.jitter_seconds, self.jitter_seconds)),
        )


def is_queue_update_stale(
    last_queue_update: datetime | None,
    *,
    now: datetime,
    stale_after_seconds: float,
) -> bool:
    """Report staleness only when Queue-it supplied a last-update timestamp."""

    if stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be positive")
    if last_queue_update is None:
        return False
    return now - last_queue_update > timedelta(seconds=stale_after_seconds)


class SessionRestorer(Protocol):
    async def restore(self, session: QueueSession) -> SessionRestoreResult: ...


@dataclass(frozen=True, slots=True)
class MonitoringOutcome:
    session_id: str
    success: bool
    observed_status: QueueStatus
    next_check_at: datetime
    queue_update_stale: bool
    progress_changed: bool


class QueueSessionMonitor:
    """Restore, assess, persist, and re-schedule one leased session."""

    def __init__(
        self,
        *,
        repository: SessionRepository,
        restorer: SessionRestorer,
        polling_policy: PollingPolicy,
        clock: Clock | None = None,
        jitter: Jitter = random.uniform,
    ) -> None:
        self._repository = repository
        self._restorer = restorer
        self._polling_policy = polling_policy
        self._clock = clock or (lambda: datetime.now(UTC))
        self._jitter = jitter

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        previous_progress = await self._repository.get_progress(session.session_id)
        result = await self._restorer.restore(session)
        observed_at = self._clock()
        progress_changed = False

        if result.success and result.progress is not None:
            observed_status = evaluate_queue_status(result.progress)
            session.status = observed_status
            progress_changed = _progress_signature(previous_progress) != _progress_signature(
                result.progress
            )
            if progress_changed or session.last_progress_change_at is None:
                session.last_progress_change_at = observed_at
            if result.progress.last_updated_at is not None and (
                session.last_queue_update is None
                or result.progress.last_updated_at > session.last_queue_update
            ):
                session.last_queue_update = result.progress.last_updated_at
        else:
            observed_status = session.status

        interval = self._polling_policy.interval_seconds(
            observed_status,
            result.progress,
            jitter=self._jitter,
        )
        session.next_check_at = observed_at + timedelta(seconds=interval)
        await self._repository.update(session, result.progress)
        stale = is_queue_update_stale(
            session.last_queue_update,
            now=observed_at,
            stale_after_seconds=self._polling_policy.stale_update_seconds,
        )
        return MonitoringOutcome(
            session_id=session.session_id,
            success=result.success,
            observed_status=observed_status,
            next_check_at=session.next_check_at,
            queue_update_stale=stale,
            progress_changed=progress_changed,
        )


def _progress_signature(progress: QueueProgress | None) -> tuple[object, ...] | None:
    if progress is None:
        return None
    return (
        progress.queue_number,
        progress.users_ahead,
        progress.progress_percentage,
        progress.estimated_wait_text,
        progress.expected_service_time,
        progress.queue_paused,
        progress.first_in_line,
        progress.serviced_soon,
        progress.turn_started,
        progress.connection_lost,
        progress.pre_queue,
        progress.active_queue,
    )


class MonitoringHandler(Protocol):
    async def check(self, session: QueueSession) -> MonitoringOutcome: ...


@dataclass(slots=True)
class MonitoringMetrics:
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    currently_checking: int = 0
    maximum_queue_depth: int = 0


class _StopWorker:
    pass


_STOP = _StopWorker()


class ParkedSessionScheduler:
    """Feed leased due sessions through a bounded queue to fixed workers."""

    def __init__(
        self,
        *,
        repository: SessionRepository,
        handler: MonitoringHandler,
        worker_count: int,
        queue_capacity: int,
        claim_batch_size: int,
        lease_seconds: float,
        failure_delay_seconds: float,
        scheduler_tick_seconds: float = 1.0,
        clock: Clock | None = None,
        scheduler_id: str | None = None,
    ) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be at least 1")
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be at least 1")
        if claim_batch_size < 1 or claim_batch_size > queue_capacity:
            raise ValueError("claim_batch_size must be between 1 and queue_capacity")
        if lease_seconds <= 0 or failure_delay_seconds <= 0 or scheduler_tick_seconds <= 0:
            raise ValueError("lease, failure, and scheduler tick delays must be positive")
        self._repository = repository
        self._handler = handler
        self._worker_count = worker_count
        self._claim_batch_size = claim_batch_size
        self._lease_seconds = lease_seconds
        self._failure_delay_seconds = failure_delay_seconds
        self._scheduler_tick_seconds = scheduler_tick_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._scheduler_id = scheduler_id or f"monitor-{uuid4()}"
        self._queue: asyncio.Queue[QueueSession | _StopWorker] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._workers: list[asyncio.Task[None]] = []
        self._schedule_lock = asyncio.Lock()
        self.metrics = MonitoringMetrics()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        repository: SessionRepository,
        handler: MonitoringHandler,
        clock: Clock | None = None,
        scheduler_id: str | None = None,
    ) -> Self:
        return cls(
            repository=repository,
            handler=handler,
            worker_count=settings.monitor_workers,
            queue_capacity=settings.monitor_queue_capacity,
            claim_batch_size=settings.monitor_claim_batch_size,
            lease_seconds=settings.monitor_lease_seconds,
            failure_delay_seconds=settings.queue_poll_seconds,
            scheduler_tick_seconds=settings.monitor_scheduler_tick_seconds,
            clock=clock,
            scheduler_id=scheduler_id,
        )

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def queue_capacity(self) -> int:
        return self._queue.maxsize

    @property
    def worker_task_count(self) -> int:
        return len(self._workers)

    async def start(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"queue-monitor-{index}")
            for index in range(self._worker_count)
        ]

    async def run(self, stop_event: asyncio.Event) -> MonitoringMetrics:
        """Continuously feed due work until asked to drain and stop."""

        await self.start()
        try:
            while not stop_event.is_set():
                await self.schedule_due()
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self._scheduler_tick_seconds,
                    )
                except TimeoutError:
                    pass
        finally:
            await self.wait_until_idle()
            await self.shutdown()
        return self.metrics

    async def schedule_due(self) -> int:
        """Claim no more sessions than the bounded queue can accept."""

        async with self._schedule_lock:
            available = self._queue.maxsize - self._queue.qsize()
            limit = min(available, self._claim_batch_size)
            if limit < 1:
                return 0
            now = self._clock()
            sessions = await self._repository.claim_due_sessions(
                worker_id=self._scheduler_id,
                now=now,
                lease_until=now + timedelta(seconds=self._lease_seconds),
                limit=limit,
            )
            for session in sessions:
                self._queue.put_nowait(session)
            self.metrics.claimed += len(sessions)
            self.metrics.maximum_queue_depth = max(
                self.metrics.maximum_queue_depth,
                self._queue.qsize(),
            )
            return len(sessions)

    async def wait_until_idle(self) -> None:
        await self._queue.join()

    async def shutdown(self) -> None:
        if not self._workers:
            return
        for _ in self._workers:
            await self._queue.put(_STOP)
        await asyncio.gather(*self._workers)
        self._workers.clear()

    async def _worker(self, _: int) -> None:
        while True:
            item = await self._queue.get()
            try:
                if isinstance(item, _StopWorker):
                    return
                self.metrics.currently_checking += 1
                try:
                    outcome = await self._handler.check(item)
                    self.metrics.completed += 1
                    if not outcome.success:
                        self.metrics.failed += 1
                except Exception:  # noqa: BLE001
                    self.metrics.failed += 1
                    try:
                        await self._repark_after_failure(item)
                    except Exception:
                        logger.exception("Could not re-park failed monitoring work")
                finally:
                    self.metrics.currently_checking -= 1
                    try:
                        await self._repository.release_lease(
                            item.session_id,
                            worker_id=self._scheduler_id,
                        )
                    except Exception:
                        logger.exception("Could not release monitoring lease")
            finally:
                self._queue.task_done()

    async def _repark_after_failure(self, session: QueueSession) -> None:
        session.last_error = "monitor:worker_failure"
        session.next_check_at = self._clock() + timedelta(seconds=self._failure_delay_seconds)
        await self._repository.update(session)
