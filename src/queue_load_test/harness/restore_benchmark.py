"""Repeatable, sensitive-URL-safe Phase 2 restoration benchmark reporting."""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from queue_load_test.models import QueueSession, QueueStatus, SessionMode, evaluate_queue_status
from queue_load_test.transfer import RestoreFailure, RestoreMethod, SessionRestoreResult


class RestoreBenchmarkMode(StrEnum):
    TRANSFER_ONLY = "TRANSFER_ONLY"
    STORAGE_STATE_ONLY = "STORAGE_STATE_ONLY"
    HYBRID = "HYBRID"

    @classmethod
    def parse(cls, value: str | RestoreBenchmarkMode) -> RestoreBenchmarkMode:
        if isinstance(value, cls):
            return value
        return cls(value.strip().upper())


class BenchmarkRestorer(Protocol):
    async def restore(self, session: QueueSession) -> SessionRestoreResult: ...

    async def restore_with_method(
        self,
        session: QueueSession,
        method: RestoreMethod,
    ) -> SessionRestoreResult: ...


@dataclass(frozen=True, slots=True)
class MechanismAttemptRecord:
    method: RestoreMethod
    success: bool
    observed_queue_id: str | None
    identity_match: bool | None
    error_category: str | None
    browser_context_failure: bool


@dataclass(frozen=True, slots=True)
class RestoreBenchmarkAttempt:
    session_id: str
    expected_queue_id: str | None
    observed_queue_id: str | None
    benchmark_mode: RestoreBenchmarkMode
    restore_method: RestoreMethod | None
    started_at: datetime
    duration_seconds: float
    success: bool
    identity_match: bool | None
    resulting_status: QueueStatus
    error_category: str | None
    browser_context_failure: bool
    fallback_used: bool
    mechanism_attempts: tuple[MechanismAttemptRecord, ...]


@dataclass(frozen=True, slots=True)
class RestoreAggregate:
    attempts: int
    successes: int
    failures: int
    success_rate: float | None


@dataclass(frozen=True, slots=True)
class RestoreBenchmarkReport:
    generated_at: datetime
    sample_size: int
    modes: tuple[RestoreBenchmarkMode, ...]
    attempts: tuple[RestoreBenchmarkAttempt, ...]
    summary: dict[str, object]
    scope_warning: str = (
        "Results apply only to this authorised Phase 2 run and configuration; "
        "they do not generalize to larger populations."
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "sample_size": self.sample_size,
            "modes": [mode.value for mode in self.modes],
            "scope_warning": self.scope_warning,
            "summary": self.summary,
            "attempts": [asdict(attempt) for attempt in self.attempts],
        }

    def write_json(self, path: Path) -> None:
        """Atomically replace a machine-readable report containing no transfer URLs."""

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def render_text(self) -> str:
        overall = self.summary["overall"]
        assert isinstance(overall, dict)
        lines = [
            "Phase 2 HYBRID restoration reliability benchmark",
            "",
            self.scope_warning,
            "",
            f"Sample sessions: {self.sample_size}",
            f"Restore invocations: {overall['attempts']}",
            f"Successes: {overall['successes']}",
            f"Failures: {overall['failures']}",
            f"Success rate: {_format_rate(overall['success_rate'])}",
            f"Identity mismatches: {self.summary['identity_mismatches']}",
            f"Identity mismatch rate: {_format_rate(self.summary['identity_mismatch_rate'])}",
            f"Average duration: {_format_seconds(self.summary['average_duration_seconds'])}",
            f"p50 duration: {_format_seconds(self.summary['p50_duration_seconds'])}",
            f"p95 duration: {_format_seconds(self.summary['p95_duration_seconds'])}",
            f"Fallback uses: {self.summary['fallback_usage']}",
            "",
            "Mechanism reliability",
        ]
        mechanism_summary = self.summary["mechanisms"]
        assert isinstance(mechanism_summary, dict)
        for name, value in mechanism_summary.items():
            assert isinstance(value, dict)
            lines.append(
                f"- {name}: {value['successes']}/{value['attempts']} "
                f"({_format_rate(value['success_rate'])})"
            )
        lines.extend(("", "Error categories"))
        errors = self.summary["error_categories"]
        assert isinstance(errors, dict)
        lines.extend(f"- {name}: {count}" for name, count in errors.items())
        if not errors:
            lines.append("- none")
        return "\n".join(lines) + "\n"


class RestoreBenchmarkRunner:
    """Run sequential restore probes over a bounded, explicitly selected sample."""

    def __init__(self, restorer: BenchmarkRestorer) -> None:
        self._restorer = restorer

    async def run(
        self,
        sessions: Sequence[QueueSession],
        *,
        sample_size: int,
        modes: Iterable[RestoreBenchmarkMode] = (
            RestoreBenchmarkMode.TRANSFER_ONLY,
            RestoreBenchmarkMode.STORAGE_STATE_ONLY,
            RestoreBenchmarkMode.HYBRID,
        ),
    ) -> RestoreBenchmarkReport:
        if sample_size < 1:
            raise ValueError("sample_size must be at least 1")
        parsed_modes = tuple(RestoreBenchmarkMode.parse(mode) for mode in modes)
        if not parsed_modes:
            raise ValueError("at least one benchmark mode is required")
        eligible = [
            session
            for session in sessions
            if session.mode is SessionMode.HYBRID and session.queue_id is not None
        ]
        if len(eligible) < sample_size:
            raise ValueError(
                f"Requested {sample_size} sessions but only {len(eligible)} HYBRID sessions exist"
            )

        records: list[RestoreBenchmarkAttempt] = []
        for session in eligible[:sample_size]:
            for mode in parsed_modes:
                records.append(await self._run_one(session, mode))
        attempts = tuple(records)
        return RestoreBenchmarkReport(
            generated_at=datetime.now(UTC),
            sample_size=sample_size,
            modes=parsed_modes,
            attempts=attempts,
            summary=summarize_restore_attempts(attempts),
        )

    async def _run_one(
        self,
        session: QueueSession,
        mode: RestoreBenchmarkMode,
    ) -> RestoreBenchmarkAttempt:
        expected_queue_id = session.queue_id
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        try:
            if mode is RestoreBenchmarkMode.HYBRID:
                result = await self._restorer.restore(session)
            else:
                method = (
                    RestoreMethod.TRANSFER
                    if mode is RestoreBenchmarkMode.TRANSFER_ONLY
                    else RestoreMethod.STORAGE_STATE
                )
                result = await self._restorer.restore_with_method(session, method)
        except Exception:  # noqa: BLE001 - benchmark records and continues safely
            return RestoreBenchmarkAttempt(
                session_id=session.session_id,
                expected_queue_id=expected_queue_id,
                observed_queue_id=None,
                benchmark_mode=mode,
                restore_method=None,
                started_at=started_at,
                duration_seconds=time.perf_counter() - started,
                success=False,
                identity_match=None,
                resulting_status=QueueStatus.CONNECTION_LOST,
                error_category="UNEXPECTED_EXCEPTION",
                browser_context_failure=True,
                fallback_used=False,
                mechanism_attempts=(),
            )
        if session.queue_id != expected_queue_id:
            raise RuntimeError("Restoration changed the persisted expected Queue ID")
        return _record_from_result(
            session,
            mode,
            result,
            started_at=started_at,
            duration_seconds=time.perf_counter() - started,
        )


def summarize_restore_attempts(
    attempts: Sequence[RestoreBenchmarkAttempt],
) -> dict[str, object]:
    durations = [attempt.duration_seconds for attempt in attempts]
    successes = sum(attempt.success for attempt in attempts)
    mismatches = sum(attempt.identity_match is False for attempt in attempts)
    mechanisms: dict[str, dict[str, int | float | None]] = {}
    for method in RestoreMethod:
        values = [
            mechanism
            for attempt in attempts
            for mechanism in attempt.mechanism_attempts
            if mechanism.method is method
        ]
        mechanism_successes = sum(value.success for value in values)
        mechanisms[method.value] = asdict(
            _aggregate(len(values), mechanism_successes)
        )
    errors = Counter(
        mechanism.error_category
        for attempt in attempts
        for mechanism in attempt.mechanism_attempts
        if mechanism.error_category is not None
    )
    errors.update(
        attempt.error_category
        for attempt in attempts
        if not attempt.mechanism_attempts and attempt.error_category is not None
    )
    return {
        "overall": asdict(_aggregate(len(attempts), successes)),
        "identity_mismatches": mismatches,
        "identity_mismatch_rate": mismatches / len(attempts) if attempts else None,
        "average_duration_seconds": statistics.fmean(durations) if durations else None,
        "p50_duration_seconds": percentile(durations, 50),
        "p95_duration_seconds": percentile(durations, 95),
        "mechanisms": mechanisms,
        "fallback_usage": sum(attempt.fallback_used for attempt in attempts),
        "error_categories": dict(sorted(errors.items())),
    }


def percentile(values: Sequence[float], percentage: float) -> float | None:
    """Return a linearly interpolated percentile for small or large samples."""

    if not 0 <= percentage <= 100:
        raise ValueError("percentage must be between 0 and 100")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentage / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _record_from_result(
    session: QueueSession,
    mode: RestoreBenchmarkMode,
    result: SessionRestoreResult,
    *,
    started_at: datetime,
    duration_seconds: float,
) -> RestoreBenchmarkAttempt:
    mechanism_attempts = tuple(
        MechanismAttemptRecord(
            method=attempt.method,
            success=attempt.success,
            observed_queue_id=attempt.observed_queue_id,
            identity_match=attempt.identity_match,
            error_category=attempt.failure.value if attempt.failure is not None else None,
            browser_context_failure=_is_browser_failure(attempt.failure),
        )
        for attempt in result.attempts
    )
    mismatch_attempt = next(
        (attempt for attempt in mechanism_attempts if attempt.identity_match is False),
        None,
    )
    identity_match = False if mismatch_attempt is not None else result.identity_match
    observed_queue_id = (
        mismatch_attempt.observed_queue_id
        if mismatch_attempt is not None
        else result.observed_queue_id
    )
    return RestoreBenchmarkAttempt(
        session_id=session.session_id,
        expected_queue_id=result.expected_queue_id,
        observed_queue_id=observed_queue_id,
        benchmark_mode=mode,
        restore_method=result.method,
        started_at=started_at,
        duration_seconds=duration_seconds,
        success=result.success and identity_match is not False,
        identity_match=identity_match,
        resulting_status=_resulting_status(session, result),
        error_category=result.failure.value if result.failure is not None else None,
        browser_context_failure=any(
            attempt.browser_context_failure for attempt in mechanism_attempts
        ),
        fallback_used=(mode is RestoreBenchmarkMode.HYBRID and len(mechanism_attempts) > 1),
        mechanism_attempts=mechanism_attempts,
    )


def _resulting_status(session: QueueSession, result: SessionRestoreResult) -> QueueStatus:
    if result.admitted:
        return QueueStatus.ADMITTED
    if result.expired:
        return QueueStatus.EXPIRED
    if result.progress is not None:
        return evaluate_queue_status(result.progress)
    if result.success:
        return session.status
    if result.failure in {
        RestoreFailure.EXPECTED_IDENTITY_MISSING,
        RestoreFailure.IDENTITY_MISMATCH,
        RestoreFailure.INVALID_TRANSFER_URL,
        RestoreFailure.STATE_CORRUPT,
        RestoreFailure.SESSION_EXPIRED,
        RestoreFailure.EVENT_CLOSED,
    }:
        return QueueStatus.FAILED
    return QueueStatus.CONNECTION_LOST


def _is_browser_failure(failure: RestoreFailure | None) -> bool:
    return failure in {
        RestoreFailure.NAVIGATION_FAILED,
        RestoreFailure.STATE_CONTEXT_FAILED,
        RestoreFailure.STATE_REFRESH_FAILED,
    }


def _aggregate(attempts: int, successes: int) -> RestoreAggregate:
    return RestoreAggregate(
        attempts=attempts,
        successes=successes,
        failures=attempts - successes,
        success_rate=successes / attempts if attempts else None,
    )


def _format_rate(value: object) -> str:
    return f"{value * 100:.2f}%" if isinstance(value, (int, float)) else "UNKNOWN"


def _format_seconds(value: object) -> str:
    return f"{value:.3f}s" if isinstance(value, (int, float)) else "UNKNOWN"


def _json_default(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
