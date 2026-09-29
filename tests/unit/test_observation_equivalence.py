from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from queue_load_test.harness.phase8_observation_equivalence import (
    _gates,
    _write_protected_report,
    load_manifest,
)
from queue_load_test.models import (
    MonitoringObservation,
    ObservationSource,
    QueuePageSignals,
    QueueProgress,
    QueueSession,
    QueueStatus,
    SessionMode,
    evaluate_monitoring_observation,
)
from queue_load_test.observation_equivalence import (
    ComparisonKind,
    DirectField,
    DirectObservationError,
    DirectObservationFailure,
    DirectResponseParser,
    DirectResponseSchema,
    EquivalenceReport,
    EquivalenceTolerances,
    FieldComparison,
    ShadowEquivalenceRunner,
    compare_observations,
)
from queue_load_test.scheduler import browser_observation_from_restore_result
from queue_load_test.transfer import RestoreMethod, SessionRestoreResult

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
QUEUE_ID = "authoritative-queue-id"


def _schema() -> DirectResponseSchema:
    return DirectResponseSchema(
        {
            DirectField.QUEUE_ID: ("identity", "value"),
            DirectField.PROGRESS_PERCENTAGE: ("state", "percent"),
            DirectField.QUEUE_NUMBER: ("state", "position"),
            DirectField.USERS_AHEAD: ("state", "ahead"),
            DirectField.LAST_UPDATED_AT: ("state", "updated"),
            DirectField.QUEUE_PAUSED: ("state", "hold"),
            DirectField.EXPECTED_SERVICE_TIME: ("state", "serviceAt"),
            DirectField.ESTIMATED_WAIT_TEXT: ("state", "waitText"),
            DirectField.FIRST_IN_LINE: ("lifecycle", "first"),
            DirectField.SERVICED_SOON: ("lifecycle", "soon"),
            DirectField.TURN_STARTED: ("lifecycle", "turn"),
            DirectField.PRE_QUEUE: ("lifecycle", "before"),
            DirectField.ACTIVE_QUEUE: ("lifecycle", "active"),
            DirectField.EXPIRED: ("lifecycle", "expired"),
            DirectField.REDIRECT_URL: ("navigation", "destination"),
            DirectField.POLL_AFTER_SECONDS: ("timing", "next"),
        },
        "local_simulator",
    )


def _payload() -> dict[str, object]:
    return {
        "identity": {"value": QUEUE_ID},
        "state": {
            "percent": 42.0,
            "position": "17",
            "ahead": 16,
            "updated": NOW.isoformat(),
            "hold": False,
            "serviceAt": (NOW + timedelta(minutes=10)).isoformat(),
            "waitText": "about ten minutes",
        },
        "lifecycle": {
            "first": False,
            "soon": False,
            "turn": False,
            "before": False,
            "active": True,
            "expired": False,
        },
        "navigation": {"destination": None},
        "timing": {"next": 30.0},
    }


def _parser() -> DirectResponseParser:
    return DirectResponseParser(
        _schema(), admission_matcher=lambda value: value.startswith("https://protected.test/")
    )


def _direct(payload: object | None = None) -> MonitoringObservation:
    return _parser().parse(
        _payload() if payload is None else payload,
        session_id="session-1",
        expected_queue_id=QUEUE_ID,
        observed_at=NOW,
    )


def _browser(**updates: object) -> MonitoringObservation:
    values: dict[str, object] = {
        "queue_number": "17",
        "users_ahead": 16,
        "progress_percentage": 42.0,
        "estimated_wait_text": "about ten minutes",
        "expected_service_time": NOW + timedelta(minutes=10),
        "last_updated_at": NOW,
        "queue_paused": False,
        "first_in_line": False,
        "serviced_soon": False,
        "turn_started": False,
        "pre_queue": False,
        "active_queue": True,
    }
    progress_updates = updates.pop("progress", {})
    assert isinstance(progress_updates, dict)
    values.update(progress_updates)
    page = updates.pop("page", QueuePageSignals(active_queue=True))
    return MonitoringObservation(
        source=ObservationSource.BROWSER_DOM,
        session_id="session-1",
        observed_at=NOW + timedelta(seconds=2),
        expected_queue_id=QUEUE_ID,
        observed_queue_id=QUEUE_ID,
        identity_match=True,
        progress=QueueProgress(session_id="session-1", **values),
        page=page,  # type: ignore[arg-type]
        redirect_present=updates.pop("redirect_present", None),  # type: ignore[arg-type]
        **updates,  # type: ignore[arg-type]
    )


def _field(report: EquivalenceReport, name: str) -> FieldComparison:
    return next(item for item in report.fields if item.field == name)


def test_matching_direct_and_browser_observations_share_lifecycle_semantics() -> None:
    report = compare_observations(_direct(), _browser())
    assert report.direct_status is QueueStatus.ACTIVE_QUEUE
    assert report.browser_status is QueueStatus.ACTIVE_QUEUE
    assert not report.hard_failure
    assert _field(report, "progress_percentage").kind is ComparisonKind.EXACT
    assert _field(report, "queue_id").kind is ComparisonKind.EXACT


def test_field_absence_and_schema_removal_are_reported_without_fabrication() -> None:
    payload = _payload()
    state = payload["state"]
    assert isinstance(state, dict)
    state.pop("position")
    direct = _direct(payload)
    report = compare_observations(direct, _browser())
    assert "queue_number" in direct.missing_fields
    assert _field(report, "queue_number").kind is ComparisonKind.MISSING_DIRECT


@pytest.mark.parametrize(
    ("direct_value", "expected"),
    [(42.5, ComparisonKind.ACCEPTABLE_DRIFT), (50.0, ComparisonKind.MISMATCH)],
)
def test_progress_drift(direct_value: float, expected: ComparisonKind) -> None:
    payload = _payload()
    assert isinstance(payload["state"], dict)
    payload["state"]["percent"] = direct_value
    report = compare_observations(
        _direct(payload),
        _browser(),
        tolerances=EquivalenceTolerances(progress_percentage=1.0),
    )
    assert _field(report, "progress_percentage").kind is expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(3, ComparisonKind.ACCEPTABLE_DRIFT), (30, ComparisonKind.MISMATCH)],
)
def test_timestamp_drift(seconds: int, expected: ComparisonKind) -> None:
    payload = _payload()
    assert isinstance(payload["state"], dict)
    payload["state"]["updated"] = (NOW + timedelta(seconds=seconds)).isoformat()
    report = compare_observations(_direct(payload), _browser())
    assert _field(report, "last_updated_at").kind is expected


@pytest.mark.parametrize("identity", [None, "different-queue-id"])
def test_identity_ambiguity_or_mismatch_is_a_hard_parser_failure(identity: str | None) -> None:
    payload = _payload()
    assert isinstance(payload["identity"], dict)
    if identity is None:
        payload["identity"].pop("value")
        expected = DirectObservationFailure.IDENTITY_AMBIGUITY
    else:
        payload["identity"]["value"] = identity
        expected = DirectObservationFailure.IDENTITY_MISMATCH
    with pytest.raises(DirectObservationError) as raised:
        _direct(payload)
    assert raised.value.failure is expected


def test_lifecycle_disagreement_is_a_hard_comparison_failure() -> None:
    payload = _payload()
    assert isinstance(payload["state"], dict)
    payload["state"]["hold"] = True
    report = compare_observations(_direct(payload), _browser())
    assert report.direct_status is QueueStatus.PAUSED
    assert report.browser_status is QueueStatus.ACTIVE_QUEUE
    assert report.hard_failure
    assert _field(report, "lifecycle").kind is ComparisonKind.HARD_FAILURE


def test_unknown_direct_field_is_recorded_as_schema_addition() -> None:
    payload = _payload()
    payload["newServerField"] = {"nested": 1}
    direct = _direct(payload)
    report = compare_observations(direct, _browser())
    assert direct.unknown_fields == ("newServerField.nested",)
    assert _field(report, "schema_additions").kind is ComparisonKind.UNEXPECTED_SCHEMA


def test_verified_redirect_normalizes_to_admission_and_compares_exactly() -> None:
    payload = _payload()
    assert isinstance(payload["navigation"], dict)
    payload["navigation"]["destination"] = "https://protected.test/tickets"
    direct = _direct(payload)
    browser = _browser(
        page=QueuePageSignals(admitted=True),
        redirect_present=True,
        progress={"active_queue": None},
    )
    report = compare_observations(direct, browser)
    assert evaluate_monitoring_observation(direct) is QueueStatus.ADMITTED
    assert report.direct_status is QueueStatus.ADMITTED
    assert report.browser_status is QueueStatus.ADMITTED
    assert _field(report, "admitted").kind is ComparisonKind.EXACT
    assert not report.hard_failure


def test_unrecognised_redirect_does_not_fabricate_admission() -> None:
    payload = _payload()
    assert isinstance(payload["navigation"], dict)
    payload["navigation"]["destination"] = "https://unrelated.invalid/landing"
    direct = _direct(payload)
    assert direct.redirect_present is True
    assert direct.page.admitted is False
    assert evaluate_monitoring_observation(direct) is QueueStatus.ACTIVE_QUEUE


def test_unexpected_direct_type_is_schema_failure() -> None:
    payload = _payload()
    assert isinstance(payload["state"], dict)
    payload["state"]["ahead"] = "sixteen"
    with pytest.raises(DirectObservationError) as raised:
        _direct(payload)
    assert raised.value.failure is DirectObservationFailure.SCHEMA


def test_out_of_range_direct_progress_is_schema_failure() -> None:
    payload = _payload()
    assert isinstance(payload["state"], dict)
    payload["state"]["percent"] = 101
    with pytest.raises(DirectObservationError) as raised:
        _direct(payload)
    assert raised.value.failure is DirectObservationFailure.SCHEMA


def test_existing_browser_restore_maps_to_common_observation() -> None:
    result = SessionRestoreResult(
        method=RestoreMethod.STORAGE_STATE,
        success=True,
        expected_queue_id=QUEUE_ID,
        observed_queue_id=QUEUE_ID,
        identity_match=True,
        progress=QueueProgress("session-1", users_ahead=16, queue_number="17"),
    )
    observation = browser_observation_from_restore_result(
        session_id="session-1", result=result, observed_at=NOW
    )
    assert observation.source is ObservationSource.BROWSER_DOM
    assert evaluate_monitoring_observation(observation) is QueueStatus.ACTIVE_QUEUE


def test_browser_normalization_preserves_historical_admitted_priority() -> None:
    result = SessionRestoreResult(
        method=RestoreMethod.STORAGE_STATE,
        success=True,
        expected_queue_id=QUEUE_ID,
        identity_match=True,
        admitted=True,
        expired=True,
    )
    observation = browser_observation_from_restore_result(
        session_id="session-1", result=result, observed_at=NOW
    )
    assert evaluate_monitoring_observation(observation) is QueueStatus.ADMITTED


@pytest.mark.asyncio
async def test_shadow_runner_performs_direct_then_browser_for_same_session() -> None:
    order: list[str] = []

    async def direct_provider(_session: QueueSession) -> MonitoringObservation:
        order.append("direct")
        return _direct()

    async def browser_provider(_session: QueueSession) -> MonitoringObservation:
        order.append("browser")
        return _browser()

    runner = ShadowEquivalenceRunner(
        direct_provider=direct_provider,
        browser_provider=browser_provider,
    )
    session = QueueSession(
        session_id="session-1",
        queue_id=QUEUE_ID,
        transfer_url="https://fixture.invalid/transfer",
        mode=SessionMode.HYBRID,
        state_path=Path("state/session-1.json"),
    )
    comparison = await runner.compare(session)
    assert order == ["direct", "browser"]
    assert comparison.report.equivalent


def test_shadow_manifest_is_authorized_bounded_and_session_unique(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scope": "authorized_queue_it_staging",
                "cases": [
                    {
                        "session_id": "session-1",
                        "artifact": "protected/capture.json",
                        "exchange_sequence": 3,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert load_manifest(path)[0].exchange_sequence == 3
    document = json.loads(path.read_text())
    document["cases"].append(document["cases"][0])
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_manifest(path)


def test_shadow_gate_requires_confirmation_and_two_environment_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit, match="confirm"):
        _gates(False)
    monkeypatch.setenv("RUN_STAGING_TESTS", "1")
    with pytest.raises(SystemExit, match="RUN_PHASE8_EQUIVALENCE"):
        _gates(True)
    monkeypatch.setenv("RUN_PHASE8_EQUIVALENCE", "1")
    _gates(True)


def test_shadow_report_is_written_only_to_protected_ignored_state(tmp_path: Path) -> None:
    directory = tmp_path / "protected"
    path = _write_protected_report(directory, {"sessions": 1, "hard_failures": 0})
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / ".gitignore").read_text() == "*\n"
