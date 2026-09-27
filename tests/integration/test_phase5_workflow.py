"""The Phase 5 operator workflow end to end on installed Chrome (local simulator only)."""

from pathlib import Path

import pytest

from queue_load_test.harness.phase5_workflow import run_workflow

pytest.importorskip("psutil")


async def test_phase5_operator_workflow_passes_every_check(tmp_path: Path) -> None:
    result = await run_workflow(tmp_path, headed=False)
    failed = [check["name"] for check in result["checks"] if not check["passed"]]
    assert failed == []
    assert result["passed"] >= 55
