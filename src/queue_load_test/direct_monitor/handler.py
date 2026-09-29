"""Direct Monitoring Strategy handler with the browser monitor as its fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from queue_load_test.direct_monitor.checker import DirectStatusChecker
from queue_load_test.direct_monitor.harvest import DiscoveryRecipeHarvester
from queue_load_test.direct_monitor.models import DirectAttempt, DirectFallbackReason
from queue_load_test.direct_monitor.store import DirectMonitorStateStore
from queue_load_test.metrics.logging import log_event
from queue_load_test.models import QueueSession, QueueStatus
from queue_load_test.scheduler import MonitoringOutcome, QueueSessionMonitor

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({QueueStatus.ADMITTED, QueueStatus.EXPIRED, QueueStatus.FAILED})


@dataclass(slots=True)
class DirectMonitoringMetrics:
    checks: int = 0
    direct_requests: int = 0
    direct_successes: int = 0
    browser_fallbacks: int = 0
    recipes_adopted: int = 0
    recipe_refresh_failures: int = 0
    fallback_reasons: Counter[str] = field(default_factory=Counter)


class DirectMonitoringHandler:
    """claim -> direct request -> validate -> persist -> release, else browser.

    The scheduler owns the claim, lease, and release around ``check``; this class
    only chooses how the leased session is observed. A fallback runs the unchanged
    browser monitor (restore, live inspect, persist, park). Neither path acquires
    a Queue ID, replaces the expected identity, or changes the run's strategy.
    """

    def __init__(
        self,
        *,
        browser_monitor: QueueSessionMonitor,
        checker: DirectStatusChecker,
        store: DirectMonitorStateStore,
        harvester: DiscoveryRecipeHarvester | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._browser_monitor = browser_monitor
        self._checker = checker
        self._store = store
        self._harvester = harvester
        self._wall_clock = wall_clock
        self.metrics = DirectMonitoringMetrics()

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        self.metrics.checks += 1
        attempt = await self._attempt(session)
        if attempt.attempted_request:
            self.metrics.direct_requests += 1
        if attempt.observation is not None:
            self.metrics.direct_successes += 1
            return await self._browser_monitor.apply_direct_observation(
                session, attempt.observation
            )
        reason = attempt.fallback_reason
        assert reason is not None
        self.metrics.browser_fallbacks += 1
        self.metrics.fallback_reasons[reason.value] += 1
        log_event(
            logger,
            logging.INFO if not attempt.attempted_request else logging.WARNING,
            "direct_check_fallback",
            session_id=session.session_id,
            queue_id=session.queue_id,
            status=session.status.value,
            worker_id=session.worker_id,
            error_type=reason.value,
        )
        observed_since = self._wall_clock()
        outcome = await self._browser_monitor.check(session)
        await self._refresh_recipe(session, outcome, observed_since)
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
                "direct_check_uncertain",
                session_id=session.session_id,
                worker_id=session.worker_id,
                error_type=type(exc).__name__,
            )
            return DirectAttempt(fallback_reason=DirectFallbackReason.UNCERTAIN)

    async def _refresh_recipe(
        self,
        session: QueueSession,
        outcome: MonitoringOutcome,
        observed_since: float,
    ) -> None:
        harvester = self._harvester
        queue_id = session.queue_id
        if harvester is None or queue_id is None or outcome.observed_status in _TERMINAL:
            return
        try:
            recipe = await harvester.harvest(
                session_id=session.session_id,
                expected_queue_id=queue_id,
                observed_since=observed_since,
            )
            if recipe is None:
                return
            await self._store.adopt_recipe(
                session_id=session.session_id,
                expected_queue_id=queue_id,
                recipe=recipe,
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
            return
        self.metrics.recipes_adopted += 1
        log_event(
            logger,
            logging.INFO,
            "direct_capability_established",
            session_id=session.session_id,
            worker_id=session.worker_id,
            count=1,
        )
