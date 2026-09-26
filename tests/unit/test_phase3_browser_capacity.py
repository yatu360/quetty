from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from playwright.async_api import BrowserContext

from queue_load_test.browser import BrowserCapacity, BrowserProcessCapacity
from queue_load_test.harness.phase3_browser_capacity import (
    BrowserCapacityBenchmarkReport,
    BrowserCapacityCase,
    BrowserResourceSample,
    ManagerFactory,
    NavigationMode,
    generate_phase3_browser_cases,
    run_browser_capacity_case,
    summarize_browser_resources,
)
from queue_load_test.harness.resource_benchmark import ProcessResourceSnapshot
from queue_load_test.metrics import PrometheusMetrics


class FakePage:
    async def goto(self, *_: object, **__: object) -> None:
        return None


class FakeContext:
    def __init__(self, *, fail_navigation: bool = False) -> None:
        self.closed = False
        self.fail_navigation = fail_navigation

    async def new_page(self) -> FakePage:
        if self.fail_navigation:
            raise RuntimeError("page creation failed")
        return FakePage()


class FakeOwnedContext:
    def __init__(self, manager: FakeManager, browser_id: int) -> None:
        self._manager = manager
        self._browser_id = browser_id
        self._context = FakeContext()
        self._closed = False

    @property
    def context(self) -> BrowserContext:
        return cast(BrowserContext, self._context)

    @property
    def browser_id(self) -> int:
        return self._browser_id

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def creation_duration_seconds(self) -> float:
        return 0.01

    @property
    def acquisition_wait_seconds(self) -> float:
        return 0.001

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._context.closed = True
        self._manager.active_by_browser[self._browser_id] -= 1


class FakeManager:
    def __init__(self, case: BrowserCapacityCase, *, fail_after: int | None = None) -> None:
        self.case = case
        self.fail_after = fail_after
        self.started = False
        self.shutdown_called = False
        self.created: list[FakeOwnedContext] = []
        self.active_by_browser = [0] * case.chrome_process_count

    async def start(self) -> None:
        self.started = True

    async def create_context(self) -> FakeOwnedContext:
        if self.fail_after is not None and len(self.created) >= self.fail_after:
            raise RuntimeError("synthetic allocation failure")
        browser_id = min(
            range(self.case.chrome_process_count),
            key=lambda index: (self.active_by_browser[index], index),
        )
        if self.active_by_browser[browser_id] >= self.case.max_contexts_per_browser:
            raise RuntimeError("synthetic capacity exhausted")
        owned = FakeOwnedContext(self, browser_id)
        self.created.append(owned)
        self.active_by_browser[browser_id] += 1
        return owned

    async def capacity(self) -> BrowserCapacity:
        active = sum(self.active_by_browser)
        process_capacity = tuple(
            BrowserProcessCapacity(
                index=index,
                connected=self.started,
                active_contexts=count,
                available_contexts=self.case.max_contexts_per_browser - count,
            )
            for index, count in enumerate(self.active_by_browser)
        )
        return BrowserCapacity(
            chrome_processes=self.case.chrome_process_count,
            connected_processes=self.case.chrome_process_count if self.started else 0,
            active_contexts=active,
            available_contexts=self.case.active_contexts - active,
            maximum_active_contexts=self.case.active_contexts,
            processes=process_capacity,
        )

    async def shutdown(self) -> None:
        self.shutdown_called = True
        self.started = False


class FakeProbe:
    def __init__(self) -> None:
        self.calls = 0

    def sample(self) -> ProcessResourceSnapshot:
        self.calls += 1
        return ProcessResourceSnapshot(
            application_cpu_percent=float(self.calls),
            application_ram_bytes=100 + self.calls,
            chrome_cpu_percent=float(self.calls * 2),
            chrome_ram_bytes=1_000 + self.calls,
            observed_chrome_processes=6,
        )


def test_phase3_case_generation_is_small_and_proportional() -> None:
    cases = generate_phase3_browser_cases()

    assert [case.active_contexts for case in cases] == [50, 75, 100]
    assert [case.chrome_process_count for case in cases] == [2, 3, 4]
    assert all(case.max_contexts_per_browser == 25 for case in cases)
    assert all(
        case.chrome_process_count * case.max_contexts_per_browser
        == case.active_contexts
        for case in cases
    )


@pytest.mark.parametrize(
    ("contexts", "processes", "per_browser"),
    [(101, 4, 25), (50, 1, 25), (50, 2, 26)],
)
def test_browser_capacity_case_rejects_invalid_limits(
    contexts: int,
    processes: int,
    per_browser: int,
) -> None:
    with pytest.raises(ValueError):
        BrowserCapacityCase("invalid", contexts, processes, per_browser)


def test_resource_aggregation_uses_available_samples() -> None:
    now = datetime.now(UTC)
    summary = summarize_browser_resources(
        (
            BrowserResourceSample(now, 1.0, 100, 3.0, 1_000, 5, 2, 10),
            BrowserResourceSample(now, 3.0, 300, 7.0, 3_000, 7, 2, 20),
            BrowserResourceSample(now, None, None, None, None, None, 2, 20),
        )
    )

    assert summary.application_cpu_percent.average == 2.0
    assert summary.application_cpu_percent.peak == 3.0
    assert summary.application_ram_bytes.average == 200.0
    assert summary.chrome_cpu_percent.average == 5.0
    assert summary.chrome_ram_bytes.peak == 3_000.0
    assert summary.observed_chrome_processes.average == 6.0


async def test_case_enforces_context_count_and_browser_process_accounting() -> None:
    case = BrowserCapacityCase("test", 6, 2, 3)
    manager = FakeManager(case)

    def factory(_: BrowserCapacityCase, __: PrometheusMetrics) -> FakeManager:
        return manager

    result = await run_browser_capacity_case(
        case,
        hold_seconds=0,
        sample_interval_seconds=0.01,
        allocation_workers=2,
        manager_factory=cast(ManagerFactory, factory),
        process_probe=FakeProbe(),
    )

    assert result.achieved_active_contexts == 6
    assert result.connected_browser_processes == 2
    assert result.contexts_per_browser == (3, 3)
    assert result.context_creation.count == 6
    assert result.navigation.count == 6
    assert all(sample.managed_chrome_processes == 2 for sample in result.samples)
    assert all(sample.observed_chrome_processes == 6 for sample in result.samples)
    assert result.clean_execution
    assert all(context.closed for context in manager.created)
    assert manager.shutdown_called


async def test_failed_case_cleans_up_every_created_context() -> None:
    case = BrowserCapacityCase("failed", 6, 2, 3)
    manager = FakeManager(case, fail_after=3)

    def factory(_: BrowserCapacityCase, __: PrometheusMetrics) -> FakeManager:
        return manager

    result = await run_browser_capacity_case(
        case,
        hold_seconds=0,
        sample_interval_seconds=0.01,
        allocation_workers=2,
        manager_factory=cast(ManagerFactory, factory),
        process_probe=FakeProbe(),
    )

    assert result.achieved_active_contexts == 3
    assert result.context_creation_failures == 3
    assert not result.clean_execution
    assert result.cleanup_complete
    assert all(context.closed for context in manager.created)
    assert manager.shutdown_called


async def test_report_serializes_machine_and_human_readable_results(tmp_path: Path) -> None:
    case = BrowserCapacityCase("serialize", 2, 1, 2)
    manager = FakeManager(case)

    def factory(_: BrowserCapacityCase, __: PrometheusMetrics) -> FakeManager:
        return manager

    result = await run_browser_capacity_case(
        case,
        hold_seconds=0,
        sample_interval_seconds=0.01,
        manager_factory=cast(ManagerFactory, factory),
        process_probe=FakeProbe(),
    )
    report = BrowserCapacityBenchmarkReport.build(
        (result,), navigation_mode=NavigationMode.LOCAL
    )
    report_path = tmp_path / "capacity.json"

    report.write_json(report_path)
    payload: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))

    assert payload["navigation_mode"] == "LOCAL"
    assert payload["staging_status"] == "NOT RUN"
    assert payload["cases"][0]["clean_execution"] is True
    assert payload["comparison"][0]["contexts_per_browser"] == [2]
    assert "serialize | 1 | 2/2" in report.render_text()
