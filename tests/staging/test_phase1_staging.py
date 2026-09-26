import os
from pathlib import Path

import pytest

from queue_load_test.config import get_settings
from queue_load_test.harness import AcceptanceStatus
from queue_load_test.harness.staging import run_staging_acceptance

pytestmark = [
    pytest.mark.staging,
    pytest.mark.skipif(
        os.environ.get("RUN_STAGING_TESTS") != "1",
        reason="set RUN_STAGING_TESTS=1 for authorised staging traffic",
    ),
]


async def test_authorised_phase1_ten_session_profile(tmp_path: Path) -> None:
    report = await run_staging_acceptance(
        get_settings(),
        report_path=tmp_path / "phase1-acceptance.json",
        observe_seconds=float(os.environ.get("PHASE1_OBSERVE_SECONDS", "0")),
    )

    assert (tmp_path / "phase1-acceptance.json").is_file()
    assert all(result.status is not AcceptanceStatus.FAIL for result in report.results)
