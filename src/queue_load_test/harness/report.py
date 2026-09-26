"""Sensitive-data-safe measurements and acceptance reporting for Phase 1."""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class AcceptanceStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


_QUESTIONS = (
    "Does each fresh BrowserContext receive an independent Queue ID?",
    "Does a Queue ID already exist during PRE_QUEUE?",
    "Does PRE_QUEUE transition to ACTIVE_QUEUE while keeping the same Queue ID?",
    "Can the official Queue-it transfer URL be extracted reliably?",
    "Can the transfer URL restore the same Queue-it journey?",
    "Can Playwright storage_state restore the same journey?",
    "Can progress percentage be read reliably?",
    "Does Queue-it lastUpdated continue changing appropriately?",
    "Can SERVICED_SOON be detected?",
    "Can TURN_STARTED be detected?",
    "Can final ADMITTED state be detected?",
    "Does restart/recovery preserve existing sessions?",
)


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    number: int
    question: str
    status: AcceptanceStatus
    evidence: str


@dataclass(slots=True)
class RunMeasurements:
    session_creation_seconds: list[float] = field(default_factory=list)
    context_creation_seconds: list[float] = field(default_factory=list)
    navigation_seconds: list[float] = field(default_factory=list)
    restore_seconds: list[float] = field(default_factory=list)
    monitoring_seconds: list[float] = field(default_factory=list)
    transfer_restore_attempts: int = 0
    transfer_restore_successes: int = 0
    storage_state_restore_attempts: int = 0
    storage_state_restore_successes: int = 0
    storage_state_restore_failures: int = 0
    controller_cpu_seconds: float | None = None
    controller_ram_observation_bytes: int | None = None
    browser_crashes: int = 0
    navigation_failures: int = 0
    identity_mismatches: int = 0

    @staticmethod
    def _summary(values: list[float]) -> dict[str, float | int | None]:
        if not values:
            return {"count": 0, "average": None, "minimum": None, "maximum": None}
        return {
            "count": len(values),
            "average": statistics.fmean(values),
            "minimum": min(values),
            "maximum": max(values),
        }

    @staticmethod
    def _rate(successes: int, attempts: int) -> float | None:
        return successes / attempts if attempts else None

    def summarized(self) -> dict[str, object]:
        return {
            "session_creation_seconds": self._summary(self.session_creation_seconds),
            "context_creation_seconds": self._summary(self.context_creation_seconds),
            "navigation_seconds": self._summary(self.navigation_seconds),
            "restore_seconds": self._summary(self.restore_seconds),
            "monitoring_seconds": self._summary(self.monitoring_seconds),
            "transfer_restore_success_rate": self._rate(
                self.transfer_restore_successes, self.transfer_restore_attempts
            ),
            "storage_state_restore_success_rate": self._rate(
                self.storage_state_restore_successes,
                self.storage_state_restore_successes + self.storage_state_restore_failures,
            ),
            "storage_state_restore_attempts": self.storage_state_restore_attempts,
            "controller_cpu_seconds": self.controller_cpu_seconds,
            "controller_ram_observation_bytes": self.controller_ram_observation_bytes,
            "browser_crashes": self.browser_crashes,
            "navigation_failures": self.navigation_failures,
            "identity_mismatches": self.identity_mismatches,
        }


@dataclass(frozen=True, slots=True)
class Phase1AcceptanceReport:
    results: tuple[AcceptanceResult, ...]
    measurements: dict[str, object]
    assumptions: tuple[str, ...]
    scope_warning: str = (
        "These Phase 1 observations apply only to the 10-session controlled run; "
        "they do not generalize to 100, 1,000, or 10,000 sessions."
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "scope_warning": self.scope_warning,
            "results": [asdict(result) for result in self.results],
            "measurements": self.measurements,
            "assumptions": list(self.assumptions),
        }

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )

    def render_text(self) -> str:
        lines = ["Phase 1 acceptance report", "", self.scope_warning, ""]
        lines.extend(
            f"{result.number:>2}. {result.status.value:<7} {result.question} - {result.evidence}"
            for result in self.results
        )
        lines.extend(("", "Measurements", json.dumps(self.measurements, indent=2), ""))
        lines.append("Assumptions requiring validation before Phase 2")
        lines.extend(f"- {assumption}" for assumption in self.assumptions)
        return "\n".join(lines) + "\n"


class Phase1AcceptanceRecorder:
    """Record aggregate evidence without retaining Queue IDs or transfer URLs."""

    def __init__(self, *, target_queue_ids: int = 10) -> None:
        self.target_queue_ids = target_queue_ids
        self.measurements = RunMeasurements()
        self.fresh_contexts = 0
        self._fresh_id_fingerprints: set[bytes] = set()
        self.pre_queue_with_identity = 0
        self.pre_queue_to_active_same_identity = 0
        self.pre_queue_to_active_mismatch = 0
        self.transfer_extraction_attempts = 0
        self.transfer_extraction_successes = 0
        self.progress_observations = 0
        self.last_updated_changes = 0
        self.serviced_soon_observed = False
        self.turn_started_observed = False
        self.admitted_observed = False
        self.restart_preserved: bool | None = None

    def record_fresh_identity(self, queue_id: str | None) -> None:
        self.fresh_contexts += 1
        if queue_id:
            self._fresh_id_fingerprints.add(hashlib.sha256(queue_id.encode()).digest())

    def record_transfer_extraction(self, *, success: bool) -> None:
        self.transfer_extraction_attempts += 1
        self.transfer_extraction_successes += int(success)

    def record_transfer_restore(self, *, success: bool, identity_match: bool | None) -> None:
        self.measurements.transfer_restore_attempts += 1
        self.measurements.transfer_restore_successes += int(success and identity_match is True)
        self.measurements.identity_mismatches += int(identity_match is False)

    def record_storage_restore(self, *, success: bool, identity_match: bool | None) -> None:
        self.measurements.storage_state_restore_attempts += 1
        self.measurements.storage_state_restore_successes += int(success and identity_match is True)
        self.measurements.storage_state_restore_failures += int(
            not success or identity_match is False
        )
        self.measurements.identity_mismatches += int(identity_match is False)

    def build_report(self) -> Phase1AcceptanceReport:
        unique_count = len(self._fresh_id_fingerprints)
        results = (
            self._result(
                1,
                self.fresh_contexts >= self.target_queue_ids
                and unique_count == self.fresh_contexts,
                self.fresh_contexts >= self.target_queue_ids,
                f"{unique_count}/{self.fresh_contexts} observed identities were unique",
            ),
            self._result(2, self.pre_queue_with_identity > 0, False, "PRE_QUEUE identity observed"),
            self._result(
                3,
                self.pre_queue_to_active_same_identity > 0
                and self.pre_queue_to_active_mismatch == 0,
                self.pre_queue_to_active_mismatch > 0,
                "same-identity transitions: "
                f"{self.pre_queue_to_active_same_identity}; mismatches: "
                f"{self.pre_queue_to_active_mismatch}",
            ),
            self._rate_result(
                4,
                self.transfer_extraction_successes,
                self.transfer_extraction_attempts,
                "transfer extractions",
            ),
            self._rate_result(
                5,
                self.measurements.transfer_restore_successes,
                self.measurements.transfer_restore_attempts,
                "same-identity transfer restores",
            ),
            self._storage_restore_result(),
            self._result(
                7,
                self.progress_observations > 0,
                False,
                f"{self.progress_observations} numeric progress observations",
            ),
            self._result(
                8,
                self.last_updated_changes > 0,
                False,
                f"{self.last_updated_changes} lastUpdated changes",
            ),
            self._result(9, self.serviced_soon_observed, False, "SERVICED_SOON observed"),
            self._result(10, self.turn_started_observed, False, "TURN_STARTED observed"),
            self._result(11, self.admitted_observed, False, "ADMITTED observed"),
            AcceptanceResult(
                12,
                _QUESTIONS[11],
                (
                    AcceptanceStatus.UNKNOWN
                    if self.restart_preserved is None
                    else AcceptanceStatus.PASS
                    if self.restart_preserved
                    else AcceptanceStatus.FAIL
                ),
                "restart persistence was "
                + ("not exercised" if self.restart_preserved is None else "verified"),
            ),
        )
        return Phase1AcceptanceReport(
            results=results,
            measurements=self.measurements.summarized(),
            assumptions=(
                "Run against the authorised staging event through every timed lifecycle state.",
                "Confirm the staging theme keeps exposing a supported official transfer control.",
                "Confirm Queue-it update cadence and admission destination under real event timing.",
                "Repeat on the intended Phase 2 host to establish CPU and RAM headroom.",
            ),
        )

    @staticmethod
    def _result(number: int, passed: bool, failed: bool, evidence: str) -> AcceptanceResult:
        status = (
            AcceptanceStatus.PASS
            if passed
            else AcceptanceStatus.FAIL
            if failed
            else AcceptanceStatus.UNKNOWN
        )
        return AcceptanceResult(number, _QUESTIONS[number - 1], status, evidence)

    @staticmethod
    def _rate_result(
        number: int,
        successes: int,
        attempts: int,
        label: str,
    ) -> AcceptanceResult:
        if attempts == 0:
            status = AcceptanceStatus.UNKNOWN
        elif successes == attempts:
            status = AcceptanceStatus.PASS
        else:
            status = AcceptanceStatus.FAIL
        return AcceptanceResult(
            number,
            _QUESTIONS[number - 1],
            status,
            f"{successes}/{attempts} {label}",
        )

    def _storage_restore_result(self) -> AcceptanceResult:
        successes = self.measurements.storage_state_restore_successes
        failures = self.measurements.storage_state_restore_failures
        verified = successes + failures
        if verified == 0:
            status = AcceptanceStatus.UNKNOWN
        elif failures == 0:
            status = AcceptanceStatus.PASS
        else:
            status = AcceptanceStatus.FAIL
        return AcceptanceResult(
            6,
            _QUESTIONS[5],
            status,
            f"{successes}/{verified} verified restores; "
            f"{self.measurements.storage_state_restore_attempts} attempts",
        )


def _json_default(value: Any) -> object:
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
