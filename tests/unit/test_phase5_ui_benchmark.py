from pathlib import Path

from queue_load_test.harness.phase5_ui_benchmark import PHASE5_POPULATION, run_benchmark


async def test_ten_thousand_row_dashboard_reads_are_bounded_and_read_only(
    tmp_path: Path,
) -> None:
    result = await run_benchmark(tmp_path / "ui.sqlite3", iterations=3)

    assert result["population"] == PHASE5_POPULATION
    for name, measurement in result["repository"].items():
        # One COUNT plus one LIMIT/OFFSET page (or the fixed summary aggregates):
        # no list-all, no per-row progress query, no writes.
        assert measurement["writes_per_call"] == 0, name
        limit = 4 if name == "summary_aggregate" else 2
        assert measurement["statements_per_call"] <= limit, name
    for name, measurement in result["http"].items():
        assert measurement["writes_per_call"] == 0, name
        assert measurement["statements_per_call"] <= 5, name
    # Deep pages walk the display-order index instead of sorting the population.
    assert not any("TEMP B-TREE" in detail for detail in result["deep_page_query_plan"])
    assert result["polling"]["rows_written_during_polling"] == 0
    assert result["polling"]["asyncio_tasks_after"] <= result["polling"]["asyncio_tasks_before"]
    assert result["pause_rows_written"] == 1
    assert result["resume_rows_written"] == 1
    assert result["browser_calls"] == 0
