"""Direct Monitoring Strategy handler with the browser monitor as its fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from queue_load_test.direct_monitor.checker import DirectStatusChecker
from queue_load_test.direct_monitor.harvest import DiscoveryRecipeHarvester
from queue_load_test.direct_monitor.models import DirectAttempt, DirectFallbackReason
from queue_load_test.direct_monitor.store import (
    DirectMonitorRecord,
    DirectMonitorStateError,
    DirectMonitorStateStore,
    recipe_reference,
)
from queue_load_test.metrics.logging import log_event
from queue_load_test.metrics.prometheus import PrometheusMetrics
from queue_load_test.models import (
    DirectCapability,
    QueueSession,
    QueueStatus,
    evaluate_monitoring_observation,
)
from queue_load_test.repository import DirectMonitorMetadataRepository, DirectMonitorStatus
from queue_load_test.scheduler import MonitoringOutcome, QueueSessionMonitor

logger = logging.getLogger(__name__)

type Clock = Callable[[], datetime]

_TERMINAL = frozenset({QueueStatus.ADMITTED, QueueStatus.EXPIRED, QueueStatus.FAILED})
_IDENTITY_REASONS = frozenset(
    {DirectFallbackReason.IDENTITY_MISMATCH, DirectFallbackReason.IDENTITY_AMBIGUITY}
)


@dataclass(slots=True)
class DirectMonitoringMetrics:
    checks: int = 0
    direct_requests: int = 0
    direct_successes: int = 0
    browser_fallbacks: int = 0
    recipes_adopted: int = 0
    recipe_refresh_failures: int = 0
    recipe_refresh_deferred: int = 0
    disagreements: int = 0
    metadata_failures: int = 0
    artifacts_pruned: int = 0
    fallback_reasons: Counter[str] = field(default_factory=Counter)


class DirectMonitoringHandler:
    """claim -> direct request -> validate -> persist -> release, else browser.

    The scheduler owns the claim, lease, and release around ``check``; this class
    only chooses how the leased session is observed. A fallback runs the unchanged
    browser monitor (restore, live inspect, persist, park). Neither path acquires
    a Queue ID, replaces the expected identity, or changes the run's strategy.

    Everything this class emits (logs, metrics, SQLite metadata) is limited to
    session IDs, closed enum values, counts, durations, and one-way references.
    """

    def __init__(
        self,
        *,
        browser_monitor: QueueSessionMonitor,
        checker: DirectStatusChecker,
        store: DirectMonitorStateStore,
        harvester: DiscoveryRecipeHarvester | None = None,
        metadata: DirectMonitorMetadataRepository | None = None,
        observability: PrometheusMetrics | None = None,
        readopt_cooldown_seconds: float = 0.0,
        discovery_retention: int = 0,
        wall_clock: Callable[[], float] = time.time,
        clock: Clock | None = None,
    ) -> None:
        if readopt_cooldown_seconds < 0 or discovery_retention < 0:
            raise ValueError("cooldown and retention cannot be negative")
        self._browser_monitor = browser_monitor
        self._checker = checker
        self._store = store
        self._harvester = harvester
        self._metadata = metadata
        self._observability = observability
        self._readopt_cooldown = timedelta(seconds=readopt_cooldown_seconds)
        self._discovery_retention = discovery_retention
        self._wall_clock = wall_clock
        self._clock = clock or (lambda: datetime.now(UTC))
        self.metrics = DirectMonitoringMetrics()

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        self.metrics.checks += 1
        attempt = await self._attempt(session)
        if attempt.attempted_request:
            self.metrics.direct_requests += 1
            if self._observability is not None:
                self._observability.record_direct_attempt(
                    attempt.request_seconds or 0.0, success=attempt.observation is not None
                )
        if attempt.observation is not None:
            self.metrics.direct_successes += 1
            outcome = await self._browser_monitor.apply_direct_observation(
                session, attempt.observation
            )
            log_event(
                logger,
                logging.INFO,
                "direct_monitor_success",
                session_id=session.session_id,
                status=outcome.observed_status.value,
                worker_id=session.worker_id,
                duration=attempt.request_seconds,
            )
            await self._publish(session, success_at=self._clock())
            return outcome
        reason = attempt.fallback_reason
        assert reason is not None
        self._log_failure(session, attempt, reason)
        self.metrics.browser_fallbacks += 1
        self.metrics.fallback_reasons[reason.value] += 1
        observed_since = self._wall_clock()
        started = time.perf_counter()
        try:
            outcome = await self._browser_monitor.check(session)
        finally:
            fallback_seconds = time.perf_counter() - started
            if self._observability is not None:
                self._observability.record_direct_fallback(reason, fallback_seconds)
        log_event(
            logger,
            logging.INFO,
            "direct_monitor_fallback",
            session_id=session.session_id,
            status=outcome.observed_status.value,
            worker_id=session.worker_id,
            error_type=reason.value,
            duration=fallback_seconds,
        )
        self._count_disagreement(session, attempt, outcome)
        await self._refresh_recipe(session, outcome, observed_since)
        await self._publish(
            session, failure_at=self._clock() if attempt.attempted_request else None
        )
        return outcome

    async def _attempt(self, session: QueueSession) -> DirectAttempt:
        try:
            return await self._checker.attempt(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - uncertainty always means browser fallback
            log_event(
                logger,
                logging.WARNING,
                "direct_monitor_failure",
                session_id=session.session_id,
                worker_id=session.worker_id,
                error_type=DirectFallbackReason.UNCERTAIN.value,
                operation=type(exc).__name__,
            )
            return DirectAttempt(fallback_reason=DirectFallbackReason.UNCERTAIN)

    def _log_failure(
        self, session: QueueSession, attempt: DirectAttempt, reason: DirectFallbackReason
    ) -> None:
        if reason in _IDENTITY_REASONS:
            if self._observability is not None:
                self._observability.record_direct_identity_mismatch()
            # The persisted expected Queue ID is logged per the existing policy; the
            # contradicting value from the response never is.
            log_event(
                logger,
                logging.WARNING,
                "direct_identity_mismatch",
                session_id=session.session_id,
                queue_id=session.queue_id,
                worker_id=session.worker_id,
                error_type=reason.value,
            )
        if attempt.attempted_request:
            log_event(
                logger,
                logging.WARNING,
                "direct_monitor_failure",
                session_id=session.session_id,
                status=session.status.value,
                worker_id=session.worker_id,
                error_type=reason.value,
                duration=attempt.request_seconds,
            )

    def _count_disagreement(
        self, session: QueueSession, attempt: DirectAttempt, outcome: MonitoringOutcome
    ) -> None:
        refused = attempt.rejected_observation
        if refused is None or not outcome.success:
            return
        direct_status = evaluate_monitoring_observation(refused)
        if direct_status is outcome.observed_status:
            return
        self.metrics.disagreements += 1
        if self._observability is not None:
            self._observability.record_direct_disagreement()
        log_event(
            logger,
            logging.WARNING,
            "direct_browser_disagreement",
            session_id=session.session_id,
            status=outcome.observed_status.value,
            operation=direct_status.value,
            worker_id=session.worker_id,
        )

    async def _refresh_recipe(
        self,
        session: QueueSession,
        outcome: MonitoringOutcome,
        observed_since: float,
    ) -> None:
        harvester = self._harvester
        queue_id = session.queue_id
        if harvester is None or queue_id is None:
            return
        try:
            if outcome.observed_status not in _TERMINAL and not await self._cooling_down(
                session
            ):
                recipe = await harvester.harvest(
                    session_id=session.session_id,
                    expected_queue_id=queue_id,
                    observed_since=observed_since,
                )
                if recipe is not None:
                    await self._store.adopt_recipe(
                        session_id=session.session_id,
                        expected_queue_id=queue_id,
                        recipe=recipe,
                    )
                    self.metrics.recipes_adopted += 1
                    if self._observability is not None:
                        self._observability.record_direct_recipe_refreshed()
                    log_event(
                        logger,
                        logging.INFO,
                        "direct_recipe_refreshed",
                        session_id=session.session_id,
                        worker_id=session.worker_id,
                        count=1,
                    )
            if self._discovery_retention:
                self.metrics.artifacts_pruned += await harvester.prune(
                    session_id=session.session_id, keep=self._discovery_retention
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - refresh never fails a completed check
            self.metrics.recipe_refresh_failures += 1
            log_event(
                logger,
                logging.WARNING,
                "direct_recipe_refresh_failed",
                session_id=session.session_id,
                error_type=type(exc).__name__,
            )

    async def _cooling_down(self, session: QueueSession) -> bool:
        """Bound direct-then-fallback churn after a session became unavailable."""

        if not self._readopt_cooldown:
            return False
        try:
            record = await self._store.load(session.session_id)
        except DirectMonitorStateError:
            return False
        if (
            record is None
            or record.capability is not DirectCapability.DIRECT_UNAVAILABLE
            or record.unavailable_since is None
        ):
            return False
        if self._clock() - record.unavailable_since >= self._readopt_cooldown:
            return False
        self.metrics.recipe_refresh_deferred += 1
        return True

    async def _publish(
        self,
        session: QueueSession,
        *,
        success_at: datetime | None = None,
        failure_at: datetime | None = None,
    ) -> None:
        """Mirror non-secret capability metadata into SQLite (best effort)."""

        metadata = self._metadata
        if metadata is None:
            return
        try:
            record: DirectMonitorRecord | None = await self._store.load(session.session_id)
            corrupt = False
        except DirectMonitorStateError:
            record, corrupt = None, True
        if record is None and not corrupt:
            return
        if record is not None and record.expected_queue_id != session.queue_id:
            record, corrupt = None, True
        status = DirectMonitorStatus(
            session_id=session.session_id,
            capability=(
                record.capability.value
                if record is not None
                else DirectCapability.DIRECT_UNAVAILABLE.value
            ),
            last_reason=(
                record.last_reason.value
                if record is not None and record.last_reason is not None
                else (DirectFallbackReason.RECIPE_UNCERTAIN.value if corrupt else None)
            ),
            recipe_reference=recipe_reference(record.recipe) if record is not None else None,
            consecutive_failures=record.consecutive_failures if record is not None else 0,
            last_success_at=success_at,
            last_failure_at=failure_at,
            updated_at=self._clock(),
        )
        try:
            await metadata.record_direct_monitor_status(status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - metadata never fails a completed check
            self.metrics.metadata_failures += 1
            if self._observability is not None:
                self._observability.record_repository_error("other")
            log_event(
                logger,
                logging.WARNING,
                "direct_monitor_metadata_failed",
                session_id=session.session_id,
                error_type=type(exc).__name__,
            )
