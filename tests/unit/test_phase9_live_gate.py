"""The live IPRoyal revalidation never sends traffic without both explicit gates."""

import json
from pathlib import Path

import pytest

from queue_load_test.harness import phase9_iproyal_live


@pytest.mark.parametrize(("gate", "flag"), [("0", True), ("1", False), (None, False)])
def test_live_harness_is_not_run_without_both_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str | None, flag: bool
) -> None:
    if gate is None:
        monkeypatch.delenv(phase9_iproyal_live.LIVE_GATE, raising=False)
    else:
        monkeypatch.setenv(phase9_iproyal_live.LIVE_GATE, gate)

    async def must_not_run(_: list[int]) -> dict[str, object]:
        raise AssertionError("live traffic attempted without both gates")

    monkeypatch.setattr(phase9_iproyal_live, "run_live", must_not_run)
    report = tmp_path / "live.json"
    argv = ["--report", str(report)] + (["--confirm-live-iproyal"] if flag else [])

    phase9_iproyal_live.main(argv)

    assert json.loads(report.read_text())["outcome"] == "NOT RUN"
