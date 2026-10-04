"""Persisted operator run and dashboard-only view models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from queue_load_test.models.browser import BrowserBackendName
from queue_load_test.models.session import QueueStatus


class RunStatus(StrEnum):
    """Lifecycle of the single current operator run."""

    ACTIVE = "ACTIVE"


class BrowserRuntimeState(StrEnum):
    """Browser ownership, deliberately separate from Queue-it lifecycle status."""

    PARKED = "PARKED"
    CHECKING = "CHECKING"
    OPEN_IN_CHROME = "OPEN_IN_CHROME"


class MonitoringStrategy(StrEnum):
    """Immutable run-level automatic-monitoring strategy."""

    HEADED_WINDOW = "headed_window"
    DIRECT = "direct"
    # Operator-paced acquisition: one visible window at a time, advanced only when the
    # operator closes it. Automatic monitoring afterwards reuses the browser monitor.
    MANUAL = "manual"

    @classmethod
    def parse(cls, value: str | MonitoringStrategy) -> MonitoringStrategy:
        if isinstance(value, cls):
            return value
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            supported = ", ".join(strategy.value for strategy in cls)
            raise ValueError(
                f"Unknown monitoring strategy {value!r}; expected one of: {supported}"
            ) from exc

    @property
    def label(self) -> str:
        if self is MonitoringStrategy.HEADED_WINDOW:
            return "Headed Window Strategy"
        if self is MonitoringStrategy.MANUAL:
            return "Manual Strategy"
        return "Direct Monitoring Strategy"


class ProxyProvider(StrEnum):
    """Immutable run-level proxy provenance."""

    NONE = "none"
    IPROYAL = "iproyal"

    @classmethod
    def parse(cls, value: str | ProxyProvider) -> ProxyProvider:
        if isinstance(value, cls):
            return value
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            raise ValueError("Unknown proxy provider; expected one of: none, iproyal") from exc

    @property
    def label(self) -> str:
        return "None" if self is ProxyProvider.NONE else "IPRoyal Residential"


@dataclass(frozen=True, slots=True)
class RunConfig:
    run_id: str
    target_url: str = field(repr=False)
    requested_sessions: int
    created_at: datetime
    browser_backend: BrowserBackendName = BrowserBackendName.CHROME
    monitoring_strategy: MonitoringStrategy = MonitoringStrategy.HEADED_WINDOW
    # Browser build provenance at run creation: pinned for Camoufox, observed installed
    # Chrome for Patchright, and NULL for standard Chrome and legacy runs.
    browser_build: str | None = None
    proxy_provider: ProxyProvider = ProxyProvider.NONE
    proxy_country: str | None = None
    proxy_lifetime: str | None = None
    status: RunStatus = RunStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Safe, bounded dashboard projection; sensitive session fields are excluded."""

    session_id: str
    queue_id: str | None
    status: QueueStatus
    progress_percentage: float | None
    last_queue_update: datetime | None
    last_checked_at: datetime | None
    next_check_at: datetime | None
    runtime_state: BrowserRuntimeState
    # Latest successful proxy-exit observation; local operator display only.
    proxy_ip: str | None = None
    proxy_ip_checked_at: datetime | None = None
    proxy_ip_changed_count: int = 0
    # Availability marker for the explicit Copy URL action; never the URL itself.
    has_transfer_url: bool = False


@dataclass(frozen=True, slots=True)
class SessionSummaryPage:
    items: tuple[SessionSummary, ...]
    total: int
    page: int
    page_size: int

    @property
    def page_count(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)
