"""Phase 9 acceptance workflow on the real operator stack (local stand-ins only)."""

from pathlib import Path

import pytest

from queue_load_test.harness.phase9_acceptance import run_backend
from queue_load_test.models import BrowserBackendName

pytest.importorskip("psutil")


@pytest.mark.parametrize(
    "backend", [BrowserBackendName.PATCHRIGHT, BrowserBackendName.CHROME], ids=lambda b: b.value
)
async def test_phase9_acceptance_passes_every_gate(
    tmp_path: Path, backend: BrowserBackendName
) -> None:
    result = await run_backend(backend, tmp_path)

    failed = [check["name"] for check in result["checks"] if not check["passed"]]
    assert failed == []
    assert result["ip_lookups_without_sticky_session"] == 0
    assert len(result["checks"]) >= 30
