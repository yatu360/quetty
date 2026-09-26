import os
from pathlib import Path

import pytest

from queue_load_test.config import get_settings
from queue_load_test.harness.phase2_resources import run_phase2_resource_benchmark

pytestmark = [
    pytest.mark.staging,
    pytest.mark.skipif(
        os.environ.get("RUN_STAGING_TESTS") != "1"
        or os.environ.get("RUN_PHASE2_RESOURCE_BENCHMARK") != "1",
        reason="set both staging gates for the authorised Phase 2 resource benchmark",
    ),
]


async def test_authorised_phase2_resource_and_stability_run(tmp_path: Path) -> None:
    report = await run_phase2_resource_benchmark(
        get_settings(),
        report_path=tmp_path / "phase2-resource-benchmark.json",
        monitoring_seconds=float(os.environ.get("PHASE2_MONITORING_SECONDS", "600")),
        sample_interval_seconds=float(os.environ.get("PHASE2_SAMPLE_INTERVAL_SECONDS", "5")),
        creation_timeout_seconds=float(
            os.environ.get("PHASE2_CREATION_TIMEOUT_SECONDS", "3600")
        ),
    )

    assert (tmp_path / "phase2-resource-benchmark.json").is_file()
    assert report.configuration.target_queue_ids == 100
    assert report.configuration.chrome_process_count in {1, 2}
    assert report.configuration.max_active_contexts == 25
    assert report.samples
