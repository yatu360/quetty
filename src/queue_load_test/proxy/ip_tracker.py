"""Record one post-check proxy-exit observation for a QueueSession.

Called once after a successful acquisition or queue-status check. It resolves the
session's *existing* sticky session through the run's resolver, asks the observer for
the exit IP, and compare-and-records it. A failure or an IP change is diagnostic only:
it never fails the queue check, changes the Queue ID, rotates the provider session, or
triggers Replace. Nothing here raises (except cancellation).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from queue_load_test.metrics.logging import log_event
from queue_load_test.models import QueueSession
from queue_load_test.proxy.ip_observer import ProxyIpFailure, ProxyIpObserver
from queue_load_test.proxy.resolver import (
    ProxyPurpose,
    ProxyResolutionError,
    SessionProxyResolver,
)

logger = logging.getLogger(__name__)

type Clock = Callable[[], datetime]

PROXY_IP_CHANGED = "PROXY_IP_CHANGED"


class ProxyIpOutcome(StrEnum):
    SKIPPED = "skipped"
    BASELINE = "baseline"
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    FAILED = "failed"


class ProxyIpRecordLike(Protocol):
    @property
    def baseline(self) -> bool: ...

    @property
    def changed(self) -> bool: ...


class ProxyIpStore(Protocol):
    async def record_proxy_ip(
        self, session_id: str, *, ip: str, observed_at: datetime
    ) -> ProxyIpRecordLike | None: ...


class ProxyIpMetrics(Protocol):
    def record_proxy_ip_observation(self, result: str) -> None: ...


class ProxyIpTracker:
    def __init__(
        self,
        *,
        resolver: SessionProxyResolver,
        observer: ProxyIpObserver,
        store: ProxyIpStore,
        observability: ProxyIpMetrics | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._resolver = resolver
        self._observer = observer
        self._store = store
        self._observability = observability
        self._clock = clock or (lambda: datetime.now(UTC))

    async def observe_after_check(self, session: QueueSession) -> ProxyIpOutcome:
        if not self._resolver.enabled:
            return ProxyIpOutcome.SKIPPED
        try:
            proxy = self._resolver.resolve(session, purpose=ProxyPurpose.IP_OBSERVATION)
        except ProxyResolutionError:
            # Fail closed: without the session's own proxy, no lookup is sent at all.
            return self._failed(session, ProxyIpFailure.PROXY_IP_UNRESOLVED)
        if proxy is None:
            return ProxyIpOutcome.SKIPPED
        self._resolver.record_attempt(purpose=ProxyPurpose.IP_OBSERVATION)
        observation = await self._observer.observe(proxy)
        if observation.ip is None:
            return self._failed(
                session, observation.failure or ProxyIpFailure.PROXY_IP_NETWORK
            )
        try:
            record = await self._store.record_proxy_ip(
                session.session_id, ip=observation.ip, observed_at=self._clock()
            )
        except Exception:  # noqa: BLE001 - diagnostic metadata never fails the check
            return self._failed(session, ProxyIpFailure.PROXY_IP_STORE_FAILED)
        if record is None:
            return ProxyIpOutcome.SKIPPED  # the row was deleted while the check ran
        if record.changed:
            outcome = ProxyIpOutcome.CHANGED
            # The provider session ID and Queue ID stay exactly as they are; the
            # change is continuity evidence only. The IP itself is not logged.
            log_event(
                logger,
                logging.WARNING,
                "proxy_ip_changed",
                session_id=session.session_id,
                error_type=PROXY_IP_CHANGED,
            )
        elif record.baseline:
            outcome = ProxyIpOutcome.BASELINE
        else:
            outcome = ProxyIpOutcome.UNCHANGED
        self._count(outcome.value)
        return outcome

    def _failed(self, session: QueueSession, failure: ProxyIpFailure) -> ProxyIpOutcome:
        log_event(
            logger,
            logging.WARNING,
            "proxy_ip_observation_failed",
            session_id=session.session_id,
            error_type=failure.value,
        )
        self._count(failure.value)
        return ProxyIpOutcome.FAILED

    def _count(self, result: str) -> None:
        if self._observability is not None:
            self._observability.record_proxy_ip_observation(result)
