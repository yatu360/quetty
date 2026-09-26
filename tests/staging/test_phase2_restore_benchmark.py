import os
from pathlib import Path

import pytest

from queue_load_test.config import get_settings
from queue_load_test.harness.phase2_restore import run_phase2_restore_benchmark

pytestmark = [
    pytest.mark.staging,
    pytest.mark.skipif(
        os.environ.get("RUN_STAGING_TESTS") != "1"
        or os.environ.get("RUN_PHASE2_RESTORE_BENCHMARK") != "1",
        reason="set both staging gates for the authorised Phase 2 restore benchmark",
    ),
]


async def test_authorised_phase2_restore_reliability(tmp_path: Path) -> None:
    sample_size = int(os.environ.get("PHASE2_RESTORE_SAMPLE_SIZE", "100"))
    report = await run_phase2_restore_benchmark(
        get_settings(),
        report_path=tmp_path / "phase2-restore-benchmark.json",
        sample_size=sample_size,
    )

    assert (tmp_path / "phase2-restore-benchmark.json").is_file()
    assert report.sample_size == sample_size
    assert len(report.attempts) == sample_size * 3
