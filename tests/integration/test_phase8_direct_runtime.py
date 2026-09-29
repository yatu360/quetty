"""The complete Phase 8 Direct Monitoring Strategy workflow (local simulator only)."""

from pathlib import Path

import pytest

from queue_load_test.harness.phase8_direct_runtime import run_direct_workflow
from queue_load_test.models import BrowserBackendName

pytest.importorskip("psutil")


async def test_phase8_direct_monitoring_workflow_passes_every_check(tmp_path: Path) -> None:
    result = await run_direct_workflow(tmp_path, backend=BrowserBackendName.CHROME)

    failed = [check["name"] for check in result["checks"] if not check["passed"]]
    assert failed == []
    assert result["passed"] >= 30
    assert result["queue_it_contacted"] is False
    metrics = result["evidence"]["direct_metrics"]
    assert metrics["direct_successes"] > 0
    assert metrics["browser_fallbacks"] > 0
