"""Field and lifecycle comparison for near-in-time browser/direct observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from queue_load_test.models import (
    MonitoringObservation,
    ObservationSource,
    QueueStatus,
    evaluate_monitoring_observation,
)


class ComparisonKind(StrEnum):
    EXACT = "exact_agreement"
    ACCEPTABLE_DRIFT = "acceptable_timing_drift"
    UNAVAILABLE = "unavailable_in_both"
    MISSING_DIRECT = "missing_direct"
    MISSING_BROWSER = "missing_browser"
    MISMATCH = "mismatch"
    UNEXPECTED_SCHEMA = "unexpected_schema"
    HARD_FAILURE = "hard_failure"


@dataclass(frozen=True, slots=True)
class FieldComparison:
    field: str
    kind: ComparisonKind
    direct_present: bool
    browser_present: bool
    absolute_difference: float | None = None


@dataclass(frozen=True, slots=True)
class EquivalenceReport:
    direct_status: QueueStatus
    browser_status: QueueStatus
    observation_gap_seconds: float
    fields: tuple[FieldComparison, ...]
    hard_failure: bool

    @property
    def equivalent(self) -> bool:
        return not self.hard_failure and all(
            field.kind
            in {
                ComparisonKind.EXACT,
                ComparisonKind.ACCEPTABLE_DRIFT,
                ComparisonKind.UNAVAILABLE,
                ComparisonKind.MISSING_DIRECT,
                ComparisonKind.MISSING_BROWSER,
                ComparisonKind.UNEXPECTED_SCHEMA,
            }
            for field in self.fields
        )


@dataclass(frozen=True, slots=True)
class EquivalenceTolerances:
    progress_percentage: float = 1.0
    users_ahead: int = 1
    timestamp_seconds: float = 5.0

    def __post_init__(self) -> None:
        if (
            self.progress_percentage < 0
            or self.users_ahead < 0
            or self.timestamp_seconds < 0
        ):
            raise ValueError("equivalence tolerances cannot be negative")


def compare_observations(
    direct: MonitoringObservation,
    browser: MonitoringObservation,
    *,
    tolerances: EquivalenceTolerances | None = None,
) -> EquivalenceReport:
    tolerances = tolerances or EquivalenceTolerances()
    if direct.source is not ObservationSource.DIRECT_RESPONSE:
        raise ValueError("direct observation has the wrong source")
    if browser.source is not ObservationSource.BROWSER_DOM:
        raise ValueError("browser observation has the wrong source")
    if direct.session_id != browser.session_id:
        raise ValueError("observations belong to different sessions")
    fields: list[FieldComparison] = []
    identity_ok = (
        direct.identity_match is True
        and browser.identity_match is True
        and direct.expected_queue_id is not None
        and direct.expected_queue_id == browser.expected_queue_id
        and direct.observed_queue_id == direct.expected_queue_id
        and (
            browser.observed_queue_id is None
            or browser.observed_queue_id == direct.expected_queue_id
        )
    )
    fields.append(
        FieldComparison(
            "queue_id",
            ComparisonKind.EXACT if identity_ok else ComparisonKind.HARD_FAILURE,
            direct.observed_queue_id is not None,
            browser.identity_match is not None,
        )
    )
    direct_status = evaluate_monitoring_observation(direct)
    browser_status = evaluate_monitoring_observation(browser)
    fields.append(
        FieldComparison(
            "lifecycle",
            ComparisonKind.EXACT
            if direct_status is browser_status
            else ComparisonKind.HARD_FAILURE,
            True,
            True,
        )
    )
    direct_progress, browser_progress = direct.progress, browser.progress
    fields.extend(
        (
            _number(
                "progress_percentage",
                direct_progress.progress_percentage,
                browser_progress.progress_percentage,
                tolerances.progress_percentage,
            ),
            _exact("queue_number", direct_progress.queue_number, browser_progress.queue_number),
            _number(
                "users_ahead",
                direct_progress.users_ahead,
                browser_progress.users_ahead,
                float(tolerances.users_ahead),
            ),
            _datetime(
                "last_updated_at",
                direct_progress.last_updated_at,
                browser_progress.last_updated_at,
                tolerances.timestamp_seconds,
            ),
            _exact("queue_paused", direct_progress.queue_paused, browser_progress.queue_paused),
            _datetime(
                "expected_service_time",
                direct_progress.expected_service_time,
                browser_progress.expected_service_time,
                tolerances.timestamp_seconds,
            ),
            _exact(
                "estimated_wait_text",
                direct_progress.estimated_wait_text,
                browser_progress.estimated_wait_text,
            ),
            _exact(
                "first_in_line", direct_progress.first_in_line, browser_progress.first_in_line
            ),
            _exact(
                "serviced_soon", direct_progress.serviced_soon, browser_progress.serviced_soon
            ),
            _exact(
                "turn_started", direct_progress.turn_started, browser_progress.turn_started
            ),
            _exact("pre_queue", direct_progress.pre_queue, browser_progress.pre_queue),
            _exact("active_queue", direct_progress.active_queue, browser_progress.active_queue),
            _exact("redirect_present", direct.redirect_present, browser.redirect_present),
            _exact("admitted", direct.page.admitted, browser.page.admitted),
            _exact("expired", direct.page.expired, browser.page.expired),
            _exact("poll_after_seconds", direct.poll_after_seconds, browser.poll_after_seconds),
        )
    )
    if direct.unknown_fields:
        fields.append(
            FieldComparison(
                "schema_additions",
                ComparisonKind.UNEXPECTED_SCHEMA,
                True,
                False,
            )
        )
    gap = abs((browser.observed_at - direct.observed_at).total_seconds())
    hard = any(field.kind is ComparisonKind.HARD_FAILURE for field in fields)
    return EquivalenceReport(
        direct_status=direct_status,
        browser_status=browser_status,
        observation_gap_seconds=gap,
        fields=tuple(fields),
        hard_failure=hard,
    )


def _exact[T](field: str, direct: T | None, browser: T | None) -> FieldComparison:
    if direct is None and browser is None:
        kind = ComparisonKind.UNAVAILABLE
    elif direct is None:
        kind = ComparisonKind.MISSING_DIRECT
    elif browser is None:
        kind = ComparisonKind.MISSING_BROWSER
    else:
        kind = ComparisonKind.EXACT if direct == browser else ComparisonKind.MISMATCH
    return FieldComparison(field, kind, direct is not None, browser is not None)


def _number(
    field: str,
    direct: float | None,
    browser: float | None,
    tolerance: float,
) -> FieldComparison:
    if direct is None or browser is None:
        return _exact(field, direct, browser)
    difference = abs(float(direct) - float(browser))
    if difference == 0:
        kind = ComparisonKind.EXACT
    elif difference <= tolerance:
        kind = ComparisonKind.ACCEPTABLE_DRIFT
    else:
        kind = ComparisonKind.MISMATCH
    return FieldComparison(field, kind, True, True, difference)


def _datetime(
    field: str,
    direct: datetime | None,
    browser: datetime | None,
    tolerance: float,
) -> FieldComparison:
    if direct is None or browser is None:
        return _exact(field, direct, browser)
    difference = abs((direct - browser).total_seconds())
    if difference == 0:
        kind = ComparisonKind.EXACT
    elif difference <= tolerance:
        kind = ComparisonKind.ACCEPTABLE_DRIFT
    else:
        kind = ComparisonKind.MISMATCH
    return FieldComparison(field, kind, True, True, difference)
