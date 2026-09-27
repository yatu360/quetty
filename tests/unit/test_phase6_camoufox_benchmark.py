from typing import Any, cast

from queue_load_test.harness.phase6_camoufox_benchmark import (
    CapacityCase,
    churn_failed,
    identity_digest,
    stats,
    stop_reason,
)
from queue_load_test.harness.resource_benchmark import (
    PsutilProcessResourceProbe,
    is_browser_main_process,
)
from queue_load_test.models import BrowserBackendName


def test_browser_main_process_classification_is_backend_specific() -> None:
    chrome_main = ["/Applications/Google Chrome", "--remote-debugging-pipe"]
    chrome_renderer = [*chrome_main, "--type=renderer"]
    camoufox_main = ["/x/Camoufox.app/Contents/MacOS/camoufox", "-juggler-pipe"]
    content = ["/x/plugin-container.app/Contents/MacOS/plugin-container", "-isForBrowser"]

    assert is_browser_main_process(BrowserBackendName.CHROME, chrome_main)
    assert not is_browser_main_process(BrowserBackendName.CHROME, chrome_renderer)
    assert not is_browser_main_process(BrowserBackendName.CHROME, camoufox_main)
    assert is_browser_main_process(BrowserBackendName.CAMOUFOX, camoufox_main)
    assert not is_browser_main_process(BrowserBackendName.CAMOUFOX, content)
    assert not is_browser_main_process(BrowserBackendName.CAMOUFOX, chrome_main)
    assert not is_browser_main_process(BrowserBackendName.CAMOUFOX, [])


class FakeProcess:
    def __init__(
        self,
        pid: int,
        name: str,
        cmdline: list[str],
        *,
        rss: int = 100,
        children: list["FakeProcess"] | None = None,
    ) -> None:
        self.pid = pid
        self._name = name
        self._cmdline = cmdline
        self._rss = rss
        self._children = children or []

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        return self._cmdline

    def cpu_percent(self, interval: object = None) -> float:
        return 10.0

    def memory_info(self) -> Any:
        return type("Memory", (), {"rss": self._rss})()

    def children(self, recursive: bool = False) -> list["FakeProcess"]:
        found = []
        for child in self._children:
            found.append(child)
            if recursive:
                found.extend(child.children(recursive=True))
        return found


def _psutil(application: FakeProcess) -> Any:
    return type("FakePsutil", (), {"Process": staticmethod(lambda pid: application)})()


def _camoufox_tree() -> FakeProcess:
    content = [
        FakeProcess(11 + index, "plugin-container", ["plugin-container"], rss=50)
        for index in range(3)
    ]
    root = FakeProcess(10, "camoufox", ["/x/camoufox", "-juggler-pipe"], rss=400, children=content)
    driver = FakeProcess(2, "node", ["node", "cli.js"], rss=70, children=[root])
    chrome = FakeProcess(
        20, "Google Chrome", ["/c/Google Chrome", "--remote-debugging-pipe"], rss=300
    )
    return FakeProcess(1, "python", ["python"], rss=200, children=[driver, chrome])


def test_probe_counts_the_camoufox_tree_not_chrome_or_the_driver() -> None:
    probe = PsutilProcessResourceProbe(
        _psutil(_camoufox_tree()), backend=BrowserBackendName.CAMOUFOX
    )

    snapshot = probe.sample()

    assert snapshot.observed_browser_processes == 4
    assert snapshot.observed_browser_main_processes == 1
    assert snapshot.browser_ram_bytes == 400 + 3 * 50
    # Historical field names carry the same browser-tree values.
    assert snapshot.chrome_ram_bytes == snapshot.browser_ram_bytes


def test_probe_keeps_existing_chrome_accounting() -> None:
    probe = PsutilProcessResourceProbe(_psutil(_camoufox_tree()))

    snapshot = probe.sample()

    assert snapshot.observed_browser_processes == 1
    assert snapshot.observed_browser_main_processes == 1
    assert snapshot.browser_ram_bytes == 300


def test_capacity_families_respect_the_shipped_profiles() -> None:
    serialized = CapacityCase.per_process(BrowserBackendName.CAMOUFOX, 3)
    unserialized = CapacityCase.shared(BrowserBackendName.CAMOUFOX, 30)
    chrome = CapacityCase.shared(BrowserBackendName.CHROME, 50)

    assert (serialized.processes, serialized.contexts, serialized.serialized) == (3, 3, True)
    assert serialized.family == "camoufox-serialized"
    assert (unserialized.processes, unserialized.serialized) == (2, False)
    assert unserialized.family == "camoufox-unserialized"
    assert chrome.processes == 2 and chrome.family == "chrome-shared-processes"
    assert CapacityCase.per_process(BrowserBackendName.CHROME, 2).serialized is False


def _result(**overrides: Any) -> dict[str, Any]:
    failures = {
        "context_creation_failures": 0,
        "browser_crashes": 0,
        "navigation_failures": 0,
        "identity_mismatches": 0,
        "browser_operation_timeouts": 0,
    }
    failures.update(overrides.pop("failures", {}))
    result: dict[str, Any] = {
        "status": "COMPLETED",
        "error_category": None,
        "failures": failures,
        "achieved_active_contexts": 5,
        "configured_contexts": 5,
        "resources": {
            "application_rss_bytes": {"peak": 100.0},
            "browser_tree_rss_bytes": {"peak": 200.0},
            "browser_cpu_percent": {"average": 50.0},
        },
    }
    result.update(overrides)
    return result


def test_stop_reason_uses_objective_stability_and_resource_evidence() -> None:
    assert stop_reason(_result(), host_ram_bytes=10_000, logical_cpus=4) is None
    assert stop_reason(
        _result(failures={"browser_crashes": 1}), host_ram_bytes=None, logical_cpus=4
    )
    assert stop_reason(
        _result(failures={"identity_mismatches": 1}), host_ram_bytes=None, logical_cpus=4
    )
    assert stop_reason(_result(achieved_active_contexts=4), host_ram_bytes=None, logical_cpus=4)
    assert stop_reason(_result(), host_ram_bytes=300, logical_cpus=4)  # 300 >= 80% of 300
    busy = _result()
    busy["resources"]["browser_cpu_percent"]["average"] = 400.0
    assert stop_reason(busy, host_ram_bytes=None, logical_cpus=4)
    assert stop_reason(
        _result(status="FAILED", error_category="TimeoutError"),
        host_ram_bytes=None,
        logical_cpus=4,
    )


def test_churn_failure_requires_an_unsuccessful_restore() -> None:
    assert not churn_failed({"restoration": {}})
    assert not churn_failed({"restoration": {"restores": 8, "successes": 8}})
    assert churn_failed({"restoration": {"restores": 8, "successes": 7}})


def test_identity_digest_is_order_independent_and_sensitive_to_queue_ids() -> None:
    first = identity_digest({"a": "q1", "b": "q2"})
    assert first == identity_digest({"b": "q2", "a": "q1"})
    assert first != identity_digest({"a": "q1", "b": "q3"})


def test_stats_handles_empty_and_populated_samples() -> None:
    assert stats([])["count"] == 0
    summary = stats([1.0, 2.0, 3.0])
    assert summary["count"] == 3
    assert cast(float, summary["max"]) == 3.0
