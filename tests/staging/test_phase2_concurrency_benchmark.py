import os
from pathlib import Path

import pytest

from queue_load_test.config import get_settings
from queue_load_test.harness.concurrency_tuning import generate_tuning_matrix
from queue_load_test.harness.phase2_tuning import run_phase2_concurrency_benchmark

pytestmark = [
    pytest.mark.staging,
    pytest.mark.skipif(
        os.environ.get("RUN_STAGING_TESTS") != "1"
        or os.environ.get("RUN_PHASE2_CONCURRENCY_BENCHMARK") != "1",
        reason="set both staging gates for the authorised Phase 2 concurrency matrix",
    ),
]


async def test_authorised_phase2_concurrency_matrix(tmp_path: Path) -> None:
    settings = get_settings()
    active_contexts = tuple(
        int(value)
        for value in os.environ.get("PHASE2_ACTIVE_CONTEXTS", "5,10,15,20,25").split(",")
    )
    chrome_processes = tuple(
        int(value)
        for value in os.environ.get(
            "PHASE2_CHROME_PROCESS_COUNTS",
            str(settings.chrome_process_count),
        ).split(",")
    )
    cases = generate_tuning_matrix(
        active_context_levels=active_contexts,
        chrome_process_counts=chrome_processes,
    )

    report = await run_phase2_concurrency_benchmark(
        settings,
        cases=cases,
        report_path=tmp_path / "phase2-concurrency-benchmark.json",
        work_directory=tmp_path / "matrix-work",
        monitoring_seconds=float(os.environ.get("PHASE2_MONITORING_SECONDS", "600")),
        sample_interval_seconds=float(os.environ.get("PHASE2_SAMPLE_INTERVAL_SECONDS", "5")),
        creation_timeout_seconds=float(
            os.environ.get("PHASE2_CREATION_TIMEOUT_SECONDS", "3600")
        ),
        restore_sample_size=int(os.environ.get("PHASE2_RESTORE_SAMPLE_SIZE", "10")),
    )

    assert (tmp_path / "phase2-concurrency-benchmark.json").is_file()
    assert len(report.cases) == len(cases)
