from queue_load_test.harness.phase7_patchright_identity import RestoreMeasurements
from queue_load_test.transfer import (
    RestoreAttempt,
    RestoreFailure,
    RestoreMethod,
    SessionRestoreResult,
)


def test_restore_measurements_aggregate_attempts_without_identity_values() -> None:
    measurements = RestoreMeasurements()
    result = SessionRestoreResult(
        method=RestoreMethod.STORAGE_STATE,
        success=True,
        expected_queue_id="secret-expected",
        observed_queue_id="secret-observed",
        identity_match=True,
        state_refreshed=True,
        attempts=(
            RestoreAttempt(
                method=RestoreMethod.TRANSFER,
                success=False,
                failure=RestoreFailure.HTTP_FAILURE,
            ),
            RestoreAttempt(
                method=RestoreMethod.STORAGE_STATE,
                success=True,
                identity_match=True,
                state_refreshed=True,
            ),
        ),
    )

    measurements.record(result, 0.25)
    aggregate = measurements.aggregate()

    assert aggregate["attempts_by_mechanism"] == {"STORAGE_STATE": 1, "TRANSFER": 1}
    assert aggregate["successes_by_mechanism"] == {"STORAGE_STATE": 1}
    assert aggregate["failures_by_mechanism_and_reason"] == {"TRANSFER:HTTP_FAILURE": 1}
    assert aggregate["state_refresh_successes"] == 1
    assert "secret" not in repr(aggregate)

