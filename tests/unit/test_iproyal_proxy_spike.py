from __future__ import annotations

import json
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from queue_load_test.browser import BrowserManager
from queue_load_test.direct_replay.store import write_protected_json
from queue_load_test.harness.iproyal_proxy_spike import (
    LONG_CHECKPOINT_MINUTES,
    TEST_NAMES,
    AffinityResult,
    IPRoyalSpikeRunner,
    LongCheckpoint,
    SessionSummary,
    SpikeConfigurationError,
    SpikeEvidence,
    SpikeStatus,
    SpikeTestResult,
    build_long_checkpoint_state,
    build_restart_metadata,
    classify_affinity,
    classify_geo_country,
    compare_long_checkpoint,
    compare_restart_observation,
    construct_effective_password,
    determine_outcome,
    deterministic_sticky_session_id,
    load_spike_config,
    mask_ip,
    not_run_evidence,
    parse_geo_response,
    parse_ip_response,
    remove_protected_artifacts,
    render_report,
    safe_evidence_dict,
    validate_provider_session_id,
)


def valid_environment() -> dict[str, str]:
    return {
        "RUN_IPROYAL_PROXY_SPIKE": "1",
        "IPROYAL_PROXY_SERVER": "http://proxy.operator.invalid:1234",
        "IPROYAL_PROXY_USERNAME": "actual-user-secret",
        "IPROYAL_PROXY_PASSWORD": "actual-password-secret",
        "IPROYAL_PROXY_COUNTRY": "gb",
        "IPROYAL_PROXY_LIFETIME": "2h",
    }


def test_config_constructs_effective_password_without_secret_repr() -> None:
    loaded = load_spike_config(valid_environment())
    assert loaded.ready and loaded.config is not None
    proxy = loaded.config.proxy_for("iproyal-spike-1")
    assert proxy.password == (
        f"actual-password-secret_country-gb_session-{proxy.sticky_session_id}_lifetime-2h"
    )
    assert proxy.username == "actual-user-secret"
    assert "actual-user-secret" not in repr(loaded.config)
    assert "actual-password-secret" not in repr(proxy)


@pytest.mark.parametrize(
    "missing",
    [
        "IPROYAL_PROXY_SERVER",
        "IPROYAL_PROXY_USERNAME",
        "IPROYAL_PROXY_PASSWORD",
        "IPROYAL_PROXY_COUNTRY",
        "IPROYAL_PROXY_LIFETIME",
    ],
)
def test_config_fails_closed_for_missing_values(missing: str) -> None:
    environment = valid_environment()
    environment.pop(missing)
    loaded = load_spike_config(environment)
    assert not loaded.ready
    assert loaded.missing == (missing,)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("IPROYAL_PROXY_SERVER", "not-a-server", "explicit"),
        ("IPROYAL_PROXY_SERVER", "http://proxy.invalid", "port"),
        ("IPROYAL_PROXY_COUNTRY", "us", "must be gb"),
        ("IPROYAL_PROXY_LIFETIME", "1h", "exactly 2h"),
    ],
)
def test_config_rejects_invalid_nonsecret_values(field: str, value: str, message: str) -> None:
    environment = valid_environment()
    environment[field] = value
    loaded = load_spike_config(environment)
    assert not loaded.ready
    assert message in loaded.errors[0]


def test_session_ids_are_stable_unique_and_eight_alphanumeric() -> None:
    values = [deterministic_sticky_session_id(f"iproyal-spike-{index}") for index in range(1, 4)]
    assert values == [
        deterministic_sticky_session_id(f"iproyal-spike-{index}") for index in range(1, 4)
    ]
    assert len(set(values)) == 3
    assert all(re.fullmatch(r"[A-Za-z0-9]{8}", value) for value in values)


def test_session_validation_and_password_construction() -> None:
    assert (
        construct_effective_password("base", country="gb", session_id="Ab12Cd34", lifetime="2h")
        == "base_country-gb_session-Ab12Cd34_lifetime-2h"
    )
    assert validate_provider_session_id("Ab12Cd34") == "Ab12Cd34"
    with pytest.raises(SpikeConfigurationError):
        validate_provider_session_id("bad-id")


def test_affinity_geo_parsing_and_masking() -> None:
    assert classify_affinity("203.0.113.10", "203.0.113.10") is AffinityResult.SAME
    assert classify_affinity("203.0.113.10", "203.0.113.11") is AffinityResult.CHANGED
    assert classify_geo_country("gb\n") is SpikeStatus.PASS
    assert classify_geo_country("US") is SpikeStatus.FAIL
    assert classify_geo_country(None) is SpikeStatus.UNKNOWN
    assert parse_geo_response('{"ip":"203.0.113.42","country":"GB"}') == "GB"
    assert parse_geo_response("gb\n") == "GB"
    assert parse_ip_response('{"ip":"203.0.113.42"}') == "203.0.113.42"
    assert mask_ip("203.0.113.42") == "203.0.113.xxx"


def test_restart_metadata_hashes_raw_ip_and_is_mode_0600(tmp_path: Path) -> None:
    metadata = build_restart_metadata(
        logical_reference="iproyal-spike-1",
        ip="203.0.113.42",
        diagnostic_url="https://api.ipify.org?format=json",
        salt="fixed",
    )
    path = tmp_path / "restart.json"
    write_protected_json(path, metadata)
    assert "203.0.113.42" not in path.read_text(encoding="utf-8")
    assert compare_restart_observation(metadata, "203.0.113.42") is AffinityResult.SAME
    assert compare_restart_observation(metadata, "203.0.113.43") is AffinityResult.CHANGED
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_long_checkpoint_state_resumes_without_raw_ip(tmp_path: Path) -> None:
    state = build_long_checkpoint_state(
        logical_reference="iproyal-spike-1",
        provider_session_id=deterministic_sticky_session_id("iproyal-spike-1"),
        ip="203.0.113.42",
        country="gb",
        lifetime="2h",
        started_epoch=100.0,
        salt="fixed",
    )
    path = tmp_path / "long.json"
    write_protected_json(path, state)
    assert "203.0.113.42" not in path.read_text(encoding="utf-8")
    assert compare_long_checkpoint(state, "203.0.113.42") is AffinityResult.SAME
    assert compare_long_checkpoint(state, "203.0.113.43") is AffinityResult.CHANGED
    assert LONG_CHECKPOINT_MINUTES == (5, 30)


def test_cleanup_removes_protected_artifacts(tmp_path: Path) -> None:
    paths = (tmp_path / "one.json", tmp_path / "two.json")
    for path in paths:
        path.write_text("{}", encoding="utf-8")
    assert remove_protected_artifacts(paths)
    assert not any(path.exists() for path in paths)


def _evidence() -> SpikeEvidence:
    tests = {name: SpikeTestResult(name=name, status=SpikeStatus.PASS) for name in TEST_NAMES}
    return SpikeEvidence(
        started_at=datetime(2026, 9, 30, tzinfo=UTC),
        config_ready=True,
        missing_configuration=(),
        configuration_errors=(),
        credentials_source="provided mapping",
        environment_file_mode="0o600",
        tests=tests,
        sessions={f"iproyal-spike-{i}": SessionSummary(f"iproyal-spike-{i}") for i in range(1, 4)},
        configured_country="gb",
        configured_lifetime="2h",
        final_context_count=0,
        final_process_count=0,
        orphan_process=False,
        protected_state_removed=True,
    )


def test_aggregation_requires_critical_and_long_tests() -> None:
    evidence = _evidence()
    assert determine_outcome(evidence) == "SPIKE_PASS"
    evidence.tests["30-minute disconnected affinity"].status = SpikeStatus.PARTIAL
    assert determine_outcome(evidence) == "SPIKE_PARTIAL"
    evidence.tests["Basic proxy connectivity"].status = SpikeStatus.FAIL
    assert determine_outcome(evidence) == "SPIKE_FAIL"


def test_report_has_required_sections_and_no_raw_ip() -> None:
    evidence = _evidence()
    evidence.long_checkpoints.append(
        LongCheckpoint(5, 301.0, AffinityResult.SAME, "203.0.113.xxx", True, False, None)
    )
    evidence.outcome = determine_outcome(evidence)
    report = render_report(evidence)
    for heading in (
        "# IPRoyal Residential Proxy Spike Results",
        "## Configuration Safety",
        "## Test Results",
        "## Fresh-context cycle detail",
        "## Long disconnect detail",
        "## Per-logical-session summary",
        "## Provider behavior observations",
        "## Cleanup",
        "## Decision Matrix",
        "## Final Outcome",
        "## Production recommendation",
    ):
        assert heading in report
    assert "203.0.113.42" not in report
    assert "18. Did the IP change" in report


def test_not_run_report_and_safe_machine_result() -> None:
    evidence = not_run_evidence(load_spike_config({}))
    report = render_report(evidence)
    assert "RUN_IPROYAL_PROXY_SPIKE=1" in report
    assert "IPROYAL_PROXY_PASSWORD" in report
    assert "`SPIKE_PARTIAL`" in report
    evidence.direct_ip = "198.51.100.25"
    evidence.direct_ip_masked = "198.51.100.xxx"
    machine = json.dumps(safe_evidence_dict(evidence), default=str)
    assert "198.51.100.25" not in machine


def test_dotenv_load_reports_mode(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in valid_environment().items()),
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    loaded = load_spike_config(env_file=env_file)
    assert loaded.ready
    assert loaded.environment_file_mode == "0o600"


class FakePage:
    async def goto(self, *_: object, **__: object) -> None:
        raise TimeoutError("secret username and password")

    def locator(self, _: str) -> object:
        raise AssertionError


class FakeContext:
    async def new_page(self) -> FakePage:
        return FakePage()


class FakeOwned:
    def __init__(self) -> None:
        self.context = FakeContext()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeManager:
    def __init__(self) -> None:
        self.owned = FakeOwned()

    async def create_context(self, **_: object) -> FakeOwned:
        return self.owned


async def test_failure_is_sanitized_and_context_closes() -> None:
    loaded = load_spike_config(valid_environment())
    assert loaded.config is not None
    fake = FakeManager()
    runner = IPRoyalSpikeRunner(
        loaded.config,
        manager_factory=lambda: cast(BrowserManager, cast(Any, fake)),
    )
    observation = await runner.observe("iproyal-spike-1", "failure")
    assert not observation.succeeded
    assert fake.owned.closed
    assert "secret username" not in repr(observation)


def test_secret_audit_passes_sanitized_surfaces() -> None:
    loaded = load_spike_config(valid_environment())
    assert loaded.config is not None
    runner = IPRoyalSpikeRunner(loaded.config)
    runner.evidence.captured_surfaces.append("sanitized")
    runner.secret_audit()
    assert runner.evidence.tests["Secret leakage audit"].status is SpikeStatus.PASS
