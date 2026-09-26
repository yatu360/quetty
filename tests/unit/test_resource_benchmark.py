import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from queue_load_test.browser import BrowserCapacity, BrowserManager
from queue_load_test.config import Settings
from queue_load_test.harness.resource_benchmark import (
    ProcessResourceSnapshot,
    PsutilProcessResourceProbe,
    ResourceBenchmarkRecorder,
    ResourceSample,
    compare_reports,
)
from queue_load_test.metrics import PrometheusMetrics


class FakeBrowserManager:
    async def capacity(self) -> BrowserCapacity:
        return BrowserCapacity(
            chrome_processes=2,
            connected_processes=2,
            active_contexts=7,
            available_contexts=18,
            maximum_active_contexts=25,
            processes=(),
        )


class FakeProbe:
    def sample(self) -> ProcessResourceSnapshot:
        return ProcessResourceSnapshot(
            application_cpu_percent=20.0,
            application_ram_bytes=200,
            chrome_cpu_percent=40.0,
            chrome_ram_bytes=400,
            observed_chrome_processes=8,
            open_file_descriptors=12,
        )


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "STAGING_URL": "https://staging.example.test",
        "TARGET_QUEUE_IDS": 100,
        "SESSION_MODE": "HYBRID",
        "CHROME_PROCESS_COUNT": 2,
        "MAX_CONTEXTS_PER_BROWSER": 13,
        "MAX_ACTIVE_CONTEXTS": 25,
    }
    values.update(overrides)
    return Settings(**values)


async def test_resource_samples_are_aggregated_with_peaks_and_averages() -> None:
    recorder = ResourceBenchmarkRecorder()
    metrics = PrometheusMetrics()
    metrics.set_creation_activity(in_flight=1, queue_depth=4)
    metrics.set_monitoring_activity(active_workers=2, queue_depth=3)
    metrics.set_monitoring_backlog(50)

    await recorder.capture(
        browser_manager=cast(BrowserManager, FakeBrowserManager()),
        metrics=metrics,
        process_probe=FakeProbe(),
    )
    recorder.samples.append(
        ResourceSample(
            timestamp=datetime.now(UTC),
            application_cpu_percent=40.0,
            application_ram_bytes=300,
            chrome_cpu_percent=60.0,
            chrome_ram_bytes=600,
            observed_chrome_processes=10,
            managed_chrome_processes=2,
            active_browser_contexts=11,
            open_file_descriptors=None,
            creation_queue_depth=2,
            monitoring_queue_depth=5,
            due_session_backlog=75,
        )
    )
    recorder.record_creation(2.0, success=True)
    recorder.record_creation(4.0, success=True)
    recorder.record_context_creation(0.2)
    recorder.record_restore(3.0)
    recorder.record_check(5.0)
    recorder.creation_phase_seconds = 4.0
    recorder.monitoring_phase_seconds = 2.0
    metrics.record_browser_crash()
    metrics.record_context_creation_failure()
    metrics.record_navigation_failure()
    metrics.record_restore(
        1,
        success=False,
        used_storage_state=False,
        identity_mismatch=True,
    )

    report = recorder.build_report(
        settings(),
        metrics,
        test_duration_seconds=10,
        sample_interval_seconds=1,
    )

    assert report.summary["application_cpu_percent"] == {
        "average": 30.0,
        "peak": 40.0,
    }
    assert report.summary["application_ram_bytes"] == {
        "average": 250.0,
        "peak": 300.0,
    }
    assert report.summary["active_context_peak"] == 11
    assert report.summary["due_session_backlog_peak"] == 75
    assert report.summary["browser_crashes"] == 1
    assert report.summary["context_creation_failures"] == 1
    assert report.summary["navigation_failures"] == 1
    assert report.summary["restore_failures"] == 1
    assert report.summary["identity_mismatches"] == 1
    assert report.summary["creation_throughput_per_second"] == 0.5
    assert report.summary["monitoring_throughput_per_second"] == 0.5
    assert report.summary["latencies"] == {
        "creation": {
            "count": 2,
            "average_seconds": 3.0,
            "p50_seconds": 3.0,
            "p95_seconds": 3.9,
        },
        "context_creation": {
            "count": 1,
            "average_seconds": 0.2,
            "p50_seconds": 0.2,
            "p95_seconds": 0.2,
        },
        "check": {
            "count": 1,
            "average_seconds": 5.0,
            "p50_seconds": 5.0,
            "p95_seconds": 5.0,
        },
        "restore": {
            "count": 1,
            "average_seconds": 3.0,
            "p50_seconds": 3.0,
            "p95_seconds": 3.0,
        },
    }


def test_report_serialization_is_machine_readable_and_comparable(tmp_path: Path) -> None:
    metrics = PrometheusMetrics()
    one = ResourceBenchmarkRecorder().build_report(
        settings(CHROME_PROCESS_COUNT=1, MAX_CONTEXTS_PER_BROWSER=25),
        metrics,
        test_duration_seconds=2,
        sample_interval_seconds=1,
        staging_status="NOT RUN",
    )
    two = ResourceBenchmarkRecorder().build_report(
        settings(),
        metrics,
        test_duration_seconds=2,
        sample_interval_seconds=1,
        staging_status="NOT RUN",
    )
    path = tmp_path / "resources.json"

    two.write_json(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    comparison = compare_reports((one, two))

    assert payload["configuration"]["chrome_process_count"] == 2
    assert payload["staging_status"] == "NOT RUN"
    assert payload["scope_warning"].endswith("Phase 3 or Phase 4 performance.")
    assert [row["chrome_process_count"] for row in comparison] == [1, 2]


class DisappearingProcess:
    pid = 123

    def __init__(self) -> None:
        self.calls = 0

    def cpu_percent(self, interval: object = None) -> float:
        self.calls += 1
        if self.calls > 1:
            raise OSError("gone")
        return 0.0

    def memory_info(self) -> object:
        raise OSError("gone")


class FakePsutil:
    def __init__(self) -> None:
        self.process = DisappearingProcess()

    def Process(self, pid: int) -> DisappearingProcess:
        return self.process


def test_process_probe_tolerates_disappearing_application() -> None:
    probe = PsutilProcessResourceProbe(cast(Any, FakePsutil()))

    assert probe.available is True
    assert probe.sample() == ProcessResourceSnapshot()


def test_process_probe_tolerates_unavailable_optional_dependency(monkeypatch: Any) -> None:
    def unavailable(_: str) -> object:
        raise ImportError

    monkeypatch.setattr("importlib.import_module", unavailable)
    probe = PsutilProcessResourceProbe()

    assert probe.available is False
    assert probe.sample() == ProcessResourceSnapshot()
