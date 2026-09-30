from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from queue_load_test.browser import BrowserManager
from queue_load_test.direct_replay.store import read_protected_json, write_protected_json
from queue_load_test.harness.primed_proxy_spike import (
    DEFAULT_DIAGNOSTIC_URL,
    TEST_NAMES,
    AffinityResult,
    PrimedSpikeRunner,
    SessionSummary,
    SpikeEvidence,
    SpikeStatus,
    SpikeTestResult,
    build_restart_metadata,
    classify_affinity,
    compare_restart_observation,
    determine_outcome,
    deterministic_sticky_session_id,
    load_spike_config,
    mask_ip,
    not_run_evidence,
    parse_ip_response,
    remove_protected_artifacts,
    render_report,
    safe_evidence_dict,
)


def valid_environment() -> dict[str, str]:
    return {
        "RUN_PRIMED_PROXY_SPIKE": "1",
        "PRIMED_PROXY_SERVER": "http://proxy.operator-supplied.invalid",
        "PRIMED_PROXY_PORT": "8888",
        "PRIMED_PROXY_USERNAME": "actual-user-secret",
        "PRIMED_PROXY_PASSWORD": "actual-password-secret",
        "PRIMED_PROXY_USERNAME_TEMPLATE": (
            "{username}-operator-documented-literals-session-{session_id}"
        ),
    }


def test_proxy_configuration_is_built_from_environment_without_secret_repr() -> None:
    loaded = load_spike_config(valid_environment())

    assert loaded.ready
    assert loaded.config is not None
    proxy = loaded.config.proxy_for("proxy-spike-1")
    assert proxy.server == "http://proxy.operator-supplied.invalid:8888"
    assert proxy.username.startswith("actual-user-secret-operator-documented-literals")
    assert proxy.password == "actual-password-secret"
    assert proxy.sticky_session_id in proxy.username
    assert "actual-user-secret" not in repr(loaded.config)
    assert "actual-password-secret" not in repr(loaded.config)
    assert "actual-user-secret" not in repr(proxy)
    assert "actual-password-secret" not in repr(proxy)


def test_config_fails_closed_for_missing_password() -> None:
    environment = valid_environment()
    environment.pop("PRIMED_PROXY_PASSWORD")

    loaded = load_spike_config(environment)

    assert not loaded.ready
    assert loaded.missing == ("PRIMED_PROXY_PASSWORD",)


def test_config_rejects_malformed_server_and_does_not_repeat_value() -> None:
    environment = valid_environment()
    environment["PRIMED_PROXY_SERVER"] = "user:password@not-a-proxy-uri"

    loaded = load_spike_config(environment)

    assert not loaded.ready
    assert loaded.errors == (
        "PRIMED_PROXY_SERVER must include an explicit http, https, or socks5 protocol",
    )
    assert "password@" not in loaded.errors[0]


def test_config_rejects_invalid_or_invented_template_fields() -> None:
    environment = valid_environment()
    environment["PRIMED_PROXY_USERNAME_TEMPLATE"] = (
        "{username}-{country}-{session_id}"
    )

    loaded = load_spike_config(environment)

    assert not loaded.ready
    assert "exactly one {username}" in loaded.errors[0]


def test_config_accepts_documented_random_integer_placeholder() -> None:
    environment = valid_environment()
    environment["PRIMED_PROXY_USERNAME_TEMPLATE"] = (
        "{username}-operator-documented-literals-session-{random_integer}"
    )

    loaded = load_spike_config(environment)

    assert loaded.ready
    assert loaded.config is not None
    proxy = loaded.config.proxy_for("proxy-spike-1")
    assert proxy.sticky_session_id in proxy.username


def test_config_accepts_complete_operator_connection_without_secret_repr() -> None:
    environment = {
        "RUN_PRIMED_PROXY_SPIKE": "1",
        "PRIMED_PROXY_TEMP": (
            "http://proxy.operator-supplied.invalid:8888:actual-user-secret-cc-gb-"
            "pool-p2p-sessionid-87992834-sessiontime-60:actual-password-secret"
        ),
    }

    loaded = load_spike_config(environment)

    assert loaded.ready
    assert loaded.config is not None
    proxy = loaded.config.proxy_for("proxy-spike-1")
    assert proxy.server == "http://proxy.operator-supplied.invalid:8888"
    assert "87992834" not in proxy.username
    assert proxy.sticky_session_id in proxy.username
    assert "actual-user-secret" not in repr(loaded.config)
    assert "actual-password-secret" not in repr(proxy)


def test_complete_operator_connection_requires_explicit_protocol_and_eight_digit_id() -> None:
    no_protocol = {
        "RUN_PRIMED_PROXY_SPIKE": "1",
        "PRIMED_PROXY_TEMP": "proxy.invalid:8888:user-cc-gb-sessionid-12345678:password",
    }
    short_id = {
        "RUN_PRIMED_PROXY_SPIKE": "1",
        "PRIMED_PROXY_TEMP": (
            "http://proxy.invalid:8888:user-cc-gb-sessionid-1234:password"
        ),
    }

    assert "explicit" in load_spike_config(no_protocol).errors[0]
    assert "eight-digit" in load_spike_config(short_id).errors[0]


def test_logical_session_identifier_is_deterministic_distinct_and_eight_digits() -> None:
    first = deterministic_sticky_session_id("proxy-spike-1")
    again = deterministic_sticky_session_id("proxy-spike-1")
    second = deterministic_sticky_session_id("proxy-spike-2")

    assert first == again
    assert first != second
    assert first.isdigit()
    assert len(first) == 8


def test_same_changed_classification_uses_exact_values() -> None:
    assert classify_affinity("203.0.113.10", "203.0.113.10") is AffinityResult.SAME
    assert classify_affinity("203.0.113.10", "203.0.113.11") is AffinityResult.CHANGED
    assert classify_affinity(None, "203.0.113.11") is AffinityResult.UNKNOWN


def test_ip_parsing_and_masking_never_return_full_ip_in_mask() -> None:
    assert parse_ip_response('{"ip":"203.0.113.42"}') == "203.0.113.42"
    assert parse_ip_response("2001:db8::42\n") == "2001:db8::42"
    assert mask_ip("203.0.113.42") == "203.0.113.xxx"
    assert "42" not in mask_ip("2001:db8::42")


def test_restart_metadata_is_hash_comparable_and_contains_no_raw_ip(tmp_path: Path) -> None:
    metadata = build_restart_metadata(
        logical_reference="proxy-spike-1",
        ip="203.0.113.42",
        diagnostic_url=DEFAULT_DIAGNOSTIC_URL,
        salt="fixed-salt",
    )
    path = tmp_path / "restart.json"
    write_protected_json(path, metadata)
    serialized = path.read_text(encoding="utf-8")

    assert "203.0.113.42" not in serialized
    assert "203.0.113.xxx" in serialized
    assert compare_restart_observation(metadata, "203.0.113.42") is AffinityResult.SAME
    assert compare_restart_observation(metadata, "203.0.113.43") is AffinityResult.CHANGED
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_protected_json(path)["logical_reference"] == "proxy-spike-1"


def test_cleanup_removes_protected_restart_artifacts(tmp_path: Path) -> None:
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")

    assert remove_protected_artifacts((first, second))
    assert not first.exists()
    assert not second.exists()


def _evidence() -> SpikeEvidence:
    tests = {
        name: SpikeTestResult(name=name, status=SpikeStatus.PASS) for name in TEST_NAMES
    }
    tests["Optional longer inactivity"].status = SpikeStatus.NOT_RUN
    return SpikeEvidence(
        started_at=datetime(2026, 9, 30, tzinfo=UTC),
        config_ready=True,
        missing_configuration=(),
        configuration_errors=(),
        credentials_source="provided mapping",
        environment_file_mode=None,
        tests=tests,
        sessions={"proxy-spike-1": SessionSummary("proxy-spike-1")},
        final_context_count=0,
        final_process_count=0,
        orphan_process=False,
        protected_state_removed=True,
    )


def test_result_aggregation_requires_all_critical_gates() -> None:
    evidence = _evidence()
    assert determine_outcome(evidence) == "SPIKE_PASS"

    evidence.tests["Fresh-context affinity"].status = SpikeStatus.FAIL
    assert determine_outcome(evidence) == "SPIKE_PARTIAL"

    evidence.tests["Basic proxy connectivity"].status = SpikeStatus.FAIL
    assert determine_outcome(evidence) == "SPIKE_FAIL"


def test_report_generation_is_safe_and_contains_required_sections() -> None:
    evidence = _evidence()
    evidence.sessions["proxy-spike-1"].masked_ip = "203.0.113.xxx"
    evidence.outcome = determine_outcome(evidence)

    report = render_report(evidence)

    assert "# Primed Residential Proxy Spike Results" in report
    assert "## Configuration Safety" in report
    assert "## Test Results" in report
    assert "## Per-Logical-Session Summary" in report
    assert "## Provider-Behavior Observations" in report
    assert "## Cleanup" in report
    assert "## Decision Matrix" in report
    assert "## Final Outcome" in report
    assert "## Implementation Recommendation" in report
    assert "203.0.113.xxx" in report
    assert "203.0.113.42" not in report
    assert "QueueSession -> immutable proxy assignment" in report


def test_not_run_report_lists_exact_missing_configuration() -> None:
    loaded = load_spike_config({})
    evidence = not_run_evidence(loaded)
    report = render_report(evidence)

    assert "RUN_PRIMED_PROXY_SPIKE=1" in report
    assert "PRIMED_PROXY_PASSWORD" in report
    assert "NOT RUN" in report
    assert "`SPIKE_PARTIAL`" in report
    assert evidence.request_count == 0


def test_config_loads_git_ignored_environment_file_without_mutating_process_environment(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in valid_environment().items()),
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    loaded = load_spike_config(env_file=env_file)

    assert loaded.ready
    assert loaded.credentials_source == "git-ignored .env plus process environment overrides"
    assert loaded.environment_file_mode == "0o600"


def test_safe_machine_representation_excludes_full_observed_ips() -> None:
    evidence = _evidence()
    evidence.direct_ip = "198.51.100.25"
    evidence.direct_ip_masked = "198.51.100.xxx"
    machine = json.dumps(safe_evidence_dict(evidence), default=str)

    assert "198.51.100.25" not in machine
    assert "198.51.100.xxx" in machine


class FakePage:
    async def goto(self, *_: object, **__: object) -> None:
        raise TimeoutError("secret username and password must not be retained")

    def locator(self, _: str) -> object:
        raise AssertionError("body should not be read after failure")


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


async def test_observation_failure_keeps_only_classification_and_closes_context() -> None:
    loaded = load_spike_config(valid_environment())
    assert loaded.config is not None
    fake = FakeManager()
    runner = PrimedSpikeRunner(
        loaded.config,
        manager_factory=lambda: cast(BrowserManager, cast(Any, fake)),
    )

    observation = await runner.observe("proxy-spike-1", "failure")

    assert not observation.succeeded
    assert observation.error is not None
    assert fake.owned.closed
    assert "secret username" not in repr(observation)


def test_secret_leakage_audit_passes_for_sanitized_surfaces() -> None:
    loaded = load_spike_config(valid_environment())
    assert loaded.config is not None
    runner = PrimedSpikeRunner(loaded.config)
    runner.evidence.captured_surfaces.append("sanitized child output")

    runner.secret_audit()

    assert runner.evidence.tests["Secret leakage audit"].status is SpikeStatus.PASS
