"""Explicit direct-then-browser shadow comparison orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from queue_load_test.models import MonitoringObservation, QueueSession
from queue_load_test.observation_equivalence.compare import (
    EquivalenceReport,
    EquivalenceTolerances,
    compare_observations,
)

type ObservationProvider = Callable[[QueueSession], Awaitable[MonitoringObservation]]


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    report: EquivalenceReport
    direct_observation: MonitoringObservation
    browser_observation: MonitoringObservation


class ShadowEquivalenceRunner:
    """Observe one persisted identity directly, then through the browser fallback."""

    def __init__(
        self,
        *,
        direct_provider: ObservationProvider,
        browser_provider: ObservationProvider,
        tolerances: EquivalenceTolerances | None = None,
    ) -> None:
        self._direct_provider = direct_provider
        self._browser_provider = browser_provider
        self._tolerances = tolerances or EquivalenceTolerances()

    async def compare(self, session: QueueSession) -> ShadowComparison:
        direct = await self._direct_provider(session)
        browser = await self._browser_provider(session)
        return ShadowComparison(
            report=compare_observations(direct, browser, tolerances=self._tolerances),
            direct_observation=direct,
            browser_observation=browser,
        )
