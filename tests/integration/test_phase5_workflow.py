"""The completed Phase 5/6 backend workflow matrix (local simulator only)."""

from pathlib import Path

import pytest

from queue_load_test.harness.phase5_workflow import run_workflow
from queue_load_test.models import BrowserBackendName

pytest.importorskip("psutil")


@pytest.mark.parametrize(
    "backend",
    [BrowserBackendName.CHROME, BrowserBackendName.CAMOUFOX],
)
async def test_phase5_operator_workflow_passes_every_check(
    tmp_path: Path,
    backend: BrowserBackendName,
) -> None:
    result = await run_workflow(tmp_path, headed=False, backend=backend)
    failed = [check["name"] for check in result["checks"] if not check["passed"]]
    assert failed == []
    assert result["passed"] >= 55
