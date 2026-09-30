"""Opt-in, low-bandwidth Primed Residential proxy compatibility spike.

This module is deliberately outside production configuration and session persistence.
It uses only small HTTPS public-IP responses and never contacts Queue-it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib
import ipaddress
import json
import os
import platform
import re
import string
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from itertools import pairwise
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from dotenv import dotenv_values

from queue_load_test.browser import BrowserManager, BrowserProxySettings, PatchrightBackend
from queue_load_test.direct_replay.store import read_protected_json, write_protected_json
from queue_load_test.harness.resource_benchmark import is_browser_main_process
from queue_load_test.models import BrowserBackendName

LIVE_GATE = "RUN_PRIMED_PROXY_SPIKE"
DEFAULT_REPORT_PATH = Path("test_spike_results.md")
DEFAULT_STATE_PATH = Path(".primed-proxy-spike/restart-state.json")
DEFAULT_DIAGNOSTIC_URL = "https://api.ipify.org?format=json"
ALLOWED_DIAGNOSTIC_URLS = frozenset(
    {
        DEFAULT_DIAGNOSTIC_URL,
        "https://api64.ipify.org?format=json",
        "https://checkip.amazonaws.com/",
        "https://icanhazip.com/",
        "https://ifconfig.me/ip",
    }
)
LOGICAL_REFERENCES = ("proxy-spike-1", "proxy-spike-2", "proxy-spike-3")
PRIMED_DOCUMENTATION = (
    "https://primed-proxies.gitbook.io/primed-proxies-documentation/"
    "residential-proxies/making-requests"
)
_RESTART_FORMAT = "queue-load-test.primed-proxy-spike-restart"
_RESTART_VERSION = 1


class SpikeStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    NOT_RUN = "NOT RUN"


class FailureClass(StrEnum):
    AUTHENTICATION = "AUTHENTICATION"
    BROWSER = "BROWSER"
    CONFIGURATION = "CONFIGURATION"
    ENDPOINT = "ENDPOINT"
    NETWORK = "NETWORK"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class AffinityResult(StrEnum):
    SAME = "SAME"
    CHANGED = "CHANGED"
    UNKNOWN = "UNKNOWN"


class SpikeConfigurationError(ValueError):
    """A sanitized configuration failure containing environment names only."""


@dataclass(frozen=True, slots=True, repr=False)
class PrimedProxyConfiguration:
    """One resolved in-memory proxy configuration; credentials never enter repr."""

    logical_reference: str
    sticky_session_id: str = field(repr=False)
    server: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            "PrimedProxyConfiguration(provider='primed', "
            f"logical_reference={self.logical_reference!r}, credentials='<redacted>')"
        )

    def browser_proxy(self) -> BrowserProxySettings:
        return {
            "server": self.server,
            "username": self.username,
            "password": self.password,
        }


@dataclass(frozen=True, slots=True, repr=False)
class PrimedSpikeConfig:
    server: str = field(repr=False)
    base_username: str = field(repr=False)
    password: str = field(repr=False)
    username_template: str = field(repr=False)
    diagnostic_url: str
    logical_session_count: int
    observations_per_session: int
    park_interval_seconds: float
    inactivity_intervals_seconds: tuple[float, ...]
    navigation_timeout_seconds: float
    test_authentication_failure: bool

    def __repr__(self) -> str:
        return (
            "PrimedSpikeConfig(provider='primed', credentials='<redacted>', "
            f"logical_session_count={self.logical_session_count}, "
            f"diagnostic_url={self.diagnostic_url!r})"
        )

    @property
    def logical_references(self) -> tuple[str, ...]:
        return LOGICAL_REFERENCES[: self.logical_session_count]

    def proxy_for(self, logical_reference: str) -> PrimedProxyConfiguration:
        if logical_reference not in self.logical_references:
            raise SpikeConfigurationError("logical proxy session is outside configured population")
        session_id = deterministic_sticky_session_id(logical_reference)
        username = self.username_template.format(
            username=self.base_username,
            session_id=session_id,
            random_integer=session_id,
        )
        if not username or username == self.base_username:
            raise SpikeConfigurationError(
                "PRIMED_PROXY_USERNAME_TEMPLATE did not produce a sticky-session username"
            )
        return PrimedProxyConfiguration(
            logical_reference=logical_reference,
            sticky_session_id=session_id,
            server=self.server,
            username=username,
            password=self.password,
        )

    def secret_candidates(self) -> tuple[str, ...]:
        values = [self.base_username, self.password]
        for logical_reference in self.logical_references:
            proxy = self.proxy_for(logical_reference)
            values.extend((proxy.username, authenticated_proxy_uri(proxy)))
        return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, slots=True)
class ConfigLoadResult:
    config: PrimedSpikeConfig | None
    gate_enabled: bool
    missing: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    credentials_source: str = "provided mapping"
    environment_file_mode: str | None = None

    @property
    def ready(self) -> bool:
        return self.gate_enabled and self.config is not None and not self.missing and not self.errors


@dataclass(frozen=True, slots=True, repr=False)
class IpObservation:
    logical_reference: str
    stage: str
    duration_seconds: float
    ip: str | None = field(default=None, repr=False)
    error: FailureClass | None = None

    @property
    def succeeded(self) -> bool:
        return self.ip is not None and self.error is None

    @property
    def masked_ip(self) -> str | None:
        return mask_ip(self.ip) if self.ip is not None else None

    def __repr__(self) -> str:
        return (
            f"IpObservation(logical_reference={self.logical_reference!r}, "
            f"stage={self.stage!r}, succeeded={self.succeeded!r}, "
            f"masked_ip={self.masked_ip!r}, error={self.error!r})"
        )


@dataclass(slots=True)
class SpikeTestResult:
    name: str
    status: SpikeStatus = SpikeStatus.NOT_RUN
    observations: int = 0
    same_count: int = 0
    changed_count: int = 0
    errors: int = 0
    masked_example_ip: str | None = None
    evidence: str = "not run"


@dataclass(slots=True)
class SessionSummary:
    logical_reference: str
    observations: int = 0
    same_count: int = 0
    changed_count: int = 0
    masked_ip: str | None = None
    browser_restart: AffinityResult = AffinityResult.UNKNOWN
    process_restart: AffinityResult = AffinityResult.UNKNOWN
    longest_same_interval_seconds: float | None = None
    errors: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class FreshContextCycle:
    logical_reference: str
    observation: int
    affinity: str
    duration_seconds: float
    masked_ip: str | None
    error: str | None


@dataclass(slots=True)
class SpikeEvidence:
    started_at: datetime
    config_ready: bool
    missing_configuration: tuple[str, ...]
    configuration_errors: tuple[str, ...]
    credentials_source: str
    environment_file_mode: str | None
    tests: dict[str, SpikeTestResult]
    sessions: dict[str, SessionSummary]
    request_count: int = 0
    diagnostic_url: str = DEFAULT_DIAGNOSTIC_URL
    direct_ip_masked: str | None = None
    direct_ip: str | None = field(default=None, repr=False)
    browser_version: str | None = None
    final_context_count: int | None = None
    final_process_count: int | None = None
    protected_state_removed: bool | None = None
    temporary_files_remaining: int = 0
    orphan_process: bool | None = None
    collisions: int = 0
    configuration_borrow_suspected: bool = False
    inactivity_intervals_tested: tuple[float, ...] = ()
    fresh_context_cycles: list[FreshContextCycle] = field(default_factory=list)
    captured_surfaces: list[str] = field(default_factory=list, repr=False)
    outcome: str = "SPIKE_PARTIAL"


TEST_NAMES = (
    "Basic proxy connectivity",
    "Fresh-context affinity",
    "Browser-process restart affinity",
    "Independent Python/application restart affinity",
    "Inactivity 10s",
    "Inactivity 60s",
    "Optional longer inactivity",
    "Three-session concurrency",
    "Bypass check",
    "Failure cleanup",
    "Secret leakage audit",
    "Final context count",
    "Final managed browser-process count",
)


def _initial_tests() -> dict[str, SpikeTestResult]:
    return {name: SpikeTestResult(name=name) for name in TEST_NAMES}


def deterministic_sticky_session_id(logical_reference: str) -> str:
    """Return a stable eight-digit non-secret identifier for a logical session."""

    digest = hashlib.sha256(f"quetty-primed-spike:{logical_reference}".encode()).digest()
    return str(10_000_000 + int.from_bytes(digest[:8], "big") % 90_000_000)


def classify_affinity(previous_ip: str | None, current_ip: str | None) -> AffinityResult:
    if previous_ip is None or current_ip is None:
        return AffinityResult.UNKNOWN
    return AffinityResult.SAME if previous_ip == current_ip else AffinityResult.CHANGED


def mask_ip(value: str) -> str:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv4Address):
        parts = value.split(".")
        return ".".join((*parts[:3], "xxx"))
    groups = address.exploded.split(":")
    return ":".join((*groups[:3], "xxxx", "xxxx", "xxxx", "xxxx", "xxxx"))


def hash_ip(value: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()


def build_restart_metadata(
    *,
    logical_reference: str,
    ip: str,
    diagnostic_url: str,
    salt: str | None = None,
) -> dict[str, object]:
    """Build protected, hash-comparable metadata without retaining the raw IP."""

    selected_salt = salt or hashlib.sha256(os.urandom(32)).hexdigest()
    return {
        "format": _RESTART_FORMAT,
        "version": _RESTART_VERSION,
        "logical_reference": logical_reference,
        "salt": selected_salt,
        "previous_ip_sha256": hash_ip(ip, selected_salt),
        "previous_masked_ip": mask_ip(ip),
        "diagnostic_url": diagnostic_url,
        "created_at": datetime.now(UTC).isoformat(),
    }


def compare_restart_observation(metadata: Mapping[str, object], ip: str | None) -> AffinityResult:
    if ip is None:
        return AffinityResult.UNKNOWN
    salt = metadata.get("salt")
    previous_digest = metadata.get("previous_ip_sha256")
    if not isinstance(salt, str) or not isinstance(previous_digest, str):
        return AffinityResult.UNKNOWN
    return (
        AffinityResult.SAME
        if hash_ip(ip, salt) == previous_digest
        else AffinityResult.CHANGED
    )


def remove_protected_artifacts(paths: Sequence[Path]) -> bool:
    for path in paths:
        path.unlink(missing_ok=True)
    return not any(path.exists() for path in paths)


def _integer_field(document: Mapping[str, object], key: str) -> int:
    value = document.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_ip_response(text: str) -> str:
    candidate = text.strip()
    with contextlib.suppress(json.JSONDecodeError):
        payload = json.loads(candidate)
        if isinstance(payload, dict):
            for key in ("ip", "origin", "query"):
                value = payload.get(key)
                if isinstance(value, str):
                    candidate = value.split(",", maxsplit=1)[0].strip()
                    break
    return str(ipaddress.ip_address(candidate))


def classify_failure(exc: BaseException) -> FailureClass:
    name = type(exc).__name__.casefold()
    detail = str(exc).casefold()
    if "auth" in name or "auth" in detail or "407" in detail:
        return FailureClass.AUTHENTICATION
    if "timeout" in name or "timed out" in detail:
        return FailureClass.TIMEOUT
    if any(token in detail for token in ("name_not_resolved", "connection", "tunnel")):
        return FailureClass.NETWORK
    if isinstance(exc, (ipaddress.AddressValueError, json.JSONDecodeError)):
        return FailureClass.ENDPOINT
    if "browser" in name or "targetclosed" in name:
        return FailureClass.BROWSER
    return FailureClass.UNKNOWN


def _parse_positive_float(name: str, raw: str, *, maximum: float) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise SpikeConfigurationError(f"{name} must be a number") from exc
    if value <= 0 or value > maximum:
        raise SpikeConfigurationError(f"{name} must be greater than 0 and at most {maximum:g}")
    return value


def _validate_template(template: str) -> None:
    fields: list[str] = []
    try:
        parsed = tuple(string.Formatter().parse(template))
    except ValueError as exc:
        raise SpikeConfigurationError("PRIMED_PROXY_USERNAME_TEMPLATE is malformed") from exc
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if format_spec or conversion:
            raise SpikeConfigurationError(
                "PRIMED_PROXY_USERNAME_TEMPLATE cannot use conversions or format specs"
            )
        fields.append(field_name)
    accepted_fields = (
        fields.count("username") == 1
        and len(fields) == 2
        and (fields.count("session_id") == 1 or fields.count("random_integer") == 1)
    )
    if not accepted_fields:
        raise SpikeConfigurationError(
            "PRIMED_PROXY_USERNAME_TEMPLATE must be the authenticated username only and "
            "contain exactly one {username} plus one {session_id} or {random_integer}; "
            "embed every provider-documented literal explicitly"
        )


def _normalize_server(server: str, port: str | None) -> str:
    try:
        parsed = urlsplit(server)
        embedded_port = parsed.port
    except ValueError as exc:
        raise SpikeConfigurationError("PRIMED_PROXY_SERVER is malformed") from exc
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
        raise SpikeConfigurationError(
            "PRIMED_PROXY_SERVER must include an explicit http, https, or socks5 protocol"
        )
    if parsed.username is not None or parsed.password is not None:
        raise SpikeConfigurationError(
            "PRIMED_PROXY_SERVER must not contain credentials; use separate environment values"
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise SpikeConfigurationError("PRIMED_PROXY_SERVER cannot contain a path, query, or fragment")
    if embedded_port is not None and port:
        raise SpikeConfigurationError(
            "set the proxy port either in PRIMED_PROXY_SERVER or PRIMED_PROXY_PORT, not both"
        )
    if embedded_port is None:
        if not port:
            raise SpikeConfigurationError(
                "PRIMED_PROXY_PORT is required when PRIMED_PROXY_SERVER has no embedded port"
            )
        try:
            numeric_port = int(port)
        except ValueError as exc:
            raise SpikeConfigurationError("PRIMED_PROXY_PORT must be an integer") from exc
        if not 1 <= numeric_port <= 65535:
            raise SpikeConfigurationError("PRIMED_PROXY_PORT must be between 1 and 65535")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        netloc = f"{host}:{numeric_port}"
    else:
        netloc = parsed.netloc
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def _parse_intervals(raw: str) -> tuple[float, ...]:
    values: list[float] = []
    for item in raw.split(","):
        if not item.strip():
            continue
        values.append(
            _parse_positive_float(
                "PRIMED_PROXY_INACTIVITY_SECONDS",
                item.strip(),
                maximum=300,
            )
        )
    if not values:
        raise SpikeConfigurationError("PRIMED_PROXY_INACTIVITY_SECONDS cannot be empty")
    return tuple(values)


def _parse_operator_connection(raw: str) -> tuple[str, str, str, str]:
    """Parse Primed's documented server:port:username:password connection format."""

    scheme, separator, connection = raw.partition("://")
    if not separator or scheme not in {"http", "https", "socks5"}:
        raise SpikeConfigurationError(
            "PRIMED_PROXY_TEMP must include an explicit http, https, or socks5 protocol"
        )
    parts = connection.rsplit(":", 3)
    if len(parts) != 4:
        raise SpikeConfigurationError(
            "PRIMED_PROXY_TEMP must use the documented server:port:username:password format"
        )
    host, port, resolved_username, password = parts
    if not host or not port or not resolved_username or not password:
        raise SpikeConfigurationError("PRIMED_PROXY_TEMP contains an empty connection field")
    server = _normalize_server(f"{scheme}://{host}", port)
    session_match = re.search(r"-sessionid-(\d{8})(?=-|$)", resolved_username)
    country_marker = resolved_username.find("-cc-")
    if session_match is None or country_marker <= 0 or country_marker >= session_match.start():
        raise SpikeConfigurationError(
            "PRIMED_PROXY_TEMP username must contain the documented -cc- modifier and "
            "an eight-digit -sessionid- value"
        )
    base_username = resolved_username[:country_marker]
    username_template = (
        "{username}"
        + resolved_username[country_marker : session_match.start(1)]
        + "{random_integer}"
        + resolved_username[session_match.end(1) :]
    )
    _validate_template(username_template)
    return server, base_username, password, username_template


def _spike_environment(env_file: Path) -> tuple[dict[str, str], str, str | None]:
    file_values: dict[str, str] = {}
    file_mode: str | None = None
    if env_file.is_file():
        file_values = {
            key: value
            for key, value in dotenv_values(env_file).items()
            if isinstance(value, str)
        }
        file_mode = oct(env_file.stat().st_mode & 0o777)
    source = {**file_values, **os.environ}
    description = "process environment"
    if file_values:
        description = f"git-ignored {env_file.name} plus process environment overrides"
    return source, description, file_mode


def load_spike_config(
    environ: Mapping[str, str] | None = None,
    *,
    env_file: Path = Path(".env"),
) -> ConfigLoadResult:
    if environ is None:
        source, credentials_source, environment_file_mode = _spike_environment(env_file)
    else:
        source = dict(environ)
        credentials_source = "provided mapping"
        environment_file_mode = None
    gate_enabled = source.get(LIVE_GATE, "").strip() == "1"
    operator_connection = source.get("PRIMED_PROXY_TEMP", "").strip()
    required = (
        ()
        if operator_connection
        else (
            "PRIMED_PROXY_SERVER",
            "PRIMED_PROXY_USERNAME",
            "PRIMED_PROXY_PASSWORD",
            "PRIMED_PROXY_USERNAME_TEMPLATE",
        )
    )
    missing = tuple(name for name in required if not source.get(name, "").strip())
    if missing:
        return ConfigLoadResult(
            config=None,
            gate_enabled=gate_enabled,
            missing=missing,
            credentials_source=credentials_source,
            environment_file_mode=environment_file_mode,
        )
    validation_errors: list[str] = []
    server: str | None = None
    base_username: str | None = None
    password: str | None = None
    template: str | None = None
    if operator_connection:
        try:
            server, base_username, password, template = _parse_operator_connection(
                operator_connection
            )
        except SpikeConfigurationError as exc:
            validation_errors.append(str(exc))
    else:
        try:
            server = _normalize_server(
                source["PRIMED_PROXY_SERVER"].strip(),
                source.get("PRIMED_PROXY_PORT", "").strip() or None,
            )
        except SpikeConfigurationError as exc:
            validation_errors.append(str(exc))
        template = source["PRIMED_PROXY_USERNAME_TEMPLATE"]
        base_username = source["PRIMED_PROXY_USERNAME"]
        password = source["PRIMED_PROXY_PASSWORD"]
        try:
            _validate_template(template)
        except SpikeConfigurationError as exc:
            validation_errors.append(str(exc))
    if validation_errors:
        return ConfigLoadResult(
            config=None,
            gate_enabled=gate_enabled,
            errors=tuple(validation_errors),
            credentials_source=credentials_source,
            environment_file_mode=environment_file_mode,
        )
    assert server is not None
    assert base_username is not None
    assert password is not None
    assert template is not None
    try:
        diagnostic_url = source.get(
            "PRIMED_PROXY_DIAGNOSTIC_URL", DEFAULT_DIAGNOSTIC_URL
        ).strip()
        if diagnostic_url not in ALLOWED_DIAGNOSTIC_URLS:
            raise SpikeConfigurationError(
                "PRIMED_PROXY_DIAGNOSTIC_URL must be one of the built-in benign HTTPS IP services"
            )
        count = int(source.get("PRIMED_PROXY_LOGICAL_SESSION_COUNT", "3"))
        if not 1 <= count <= 3:
            raise SpikeConfigurationError(
                "PRIMED_PROXY_LOGICAL_SESSION_COUNT must be between 1 and 3"
            )
        cycles = int(source.get("PRIMED_PROXY_OBSERVATIONS_PER_SESSION", "3"))
        if cycles != 3:
            raise SpikeConfigurationError(
                "PRIMED_PROXY_OBSERVATIONS_PER_SESSION must be 3 for this fixed spike"
            )
        config = PrimedSpikeConfig(
            server=server,
            base_username=base_username,
            password=password,
            username_template=template,
            diagnostic_url=diagnostic_url,
            logical_session_count=count,
            observations_per_session=cycles,
            park_interval_seconds=_parse_positive_float(
                "PRIMED_PROXY_PARK_INTERVAL_SECONDS",
                source.get("PRIMED_PROXY_PARK_INTERVAL_SECONDS", "2"),
                maximum=60,
            ),
            inactivity_intervals_seconds=_parse_intervals(
                source.get("PRIMED_PROXY_INACTIVITY_SECONDS", "10,60")
            ),
            navigation_timeout_seconds=_parse_positive_float(
                "PRIMED_PROXY_NAVIGATION_TIMEOUT_SECONDS",
                source.get("PRIMED_PROXY_NAVIGATION_TIMEOUT_SECONDS", "20"),
                maximum=120,
            ),
            test_authentication_failure=(
                source.get("PRIMED_PROXY_TEST_AUTH_FAILURE", "0").strip() == "1"
            ),
        )
        resolved = [config.proxy_for(item).username for item in config.logical_references]
        if len(set(resolved)) != len(resolved):
            raise SpikeConfigurationError(
                "PRIMED_PROXY_USERNAME_TEMPLATE did not create distinct logical sessions"
            )
    except (SpikeConfigurationError, ValueError) as exc:
        return ConfigLoadResult(
            config=None,
            gate_enabled=gate_enabled,
            errors=(str(exc),),
            credentials_source=credentials_source,
            environment_file_mode=environment_file_mode,
        )
    return ConfigLoadResult(
        config=config,
        gate_enabled=gate_enabled,
        credentials_source=credentials_source,
        environment_file_mode=environment_file_mode,
    )


def authenticated_proxy_uri(proxy: PrimedProxyConfiguration) -> str:
    parsed = urlsplit(proxy.server)
    credentials = f"{quote(proxy.username, safe='')}:{quote(proxy.password, safe='')}@"
    return urlunsplit((parsed.scheme, credentials + parsed.netloc, "", "", ""))


def _manager() -> BrowserManager:
    return BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=3,
        max_active_contexts=3,
        headless=True,
        backend=PatchrightBackend(),
        timezone_id="Europe/London",
        operation_timeout_seconds=45,
        close_timeout_seconds=10,
    )


def browser_main_process_ids() -> set[int] | None:
    """Return descendant Chrome main-process IDs when optional psutil is installed."""

    try:
        psutil: Any = importlib.import_module("psutil")
    except ImportError:
        return None
    try:
        root = psutil.Process(os.getpid())
        processes = root.children(recursive=True)
    except (psutil.Error, OSError):
        return None
    identifiers: set[int] = set()
    for process in processes:
        try:
            if is_browser_main_process(BrowserBackendName.PATCHRIGHT, process.cmdline()):
                identifiers.add(process.pid)
        except (psutil.Error, OSError):
            continue
    return identifiers


class PrimedSpikeRunner:
    def __init__(
        self,
        config: PrimedSpikeConfig,
        *,
        manager_factory: Callable[[], BrowserManager] = _manager,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.manager_factory = manager_factory
        self.sleeper = sleeper
        self.manager = manager_factory()
        self.evidence = SpikeEvidence(
            started_at=datetime.now(UTC),
            config_ready=True,
            missing_configuration=(),
            configuration_errors=(),
            credentials_source="environment configuration",
            environment_file_mode=None,
            tests=_initial_tests(),
            sessions={item: SessionSummary(item) for item in config.logical_references},
            diagnostic_url=config.diagnostic_url,
        )
        self._observations: dict[str, list[IpObservation]] = defaultdict(list)
        self._baseline_browser_processes = browser_main_process_ids()

    async def observe(
        self,
        logical_reference: str,
        stage: str,
        *,
        url: str | None = None,
        proxy_override: PrimedProxyConfiguration | None = None,
    ) -> IpObservation:
        started = time.perf_counter()
        owned = None
        try:
            proxy = proxy_override or self.config.proxy_for(logical_reference)
            owned = await self.manager.create_context(proxy=proxy.browser_proxy())
            page = await owned.context.new_page()
            self.evidence.request_count += 1
            await page.goto(
                url or self.config.diagnostic_url,
                wait_until="domcontentloaded",
                timeout=self.config.navigation_timeout_seconds * 1000,
            )
            body = await page.locator("body").inner_text()
            observed_ip = parse_ip_response(body)
            observation = IpObservation(
                logical_reference=logical_reference,
                stage=stage,
                duration_seconds=round(time.perf_counter() - started, 3),
                ip=observed_ip,
            )
        except Exception as exc:  # noqa: BLE001 - only the closed classification is retained
            observation = IpObservation(
                logical_reference=logical_reference,
                stage=stage,
                duration_seconds=round(time.perf_counter() - started, 3),
                error=classify_failure(exc),
            )
        finally:
            if owned is not None:
                await owned.close()
        self._record_observation(observation)
        return observation

    def _record_observation(self, observation: IpObservation) -> None:
        self._observations[observation.logical_reference].append(observation)
        summary = self.evidence.sessions[observation.logical_reference]
        summary.observations += 1
        if observation.masked_ip is not None:
            summary.masked_ip = observation.masked_ip
        if observation.error is not None:
            summary.errors.add(observation.error.value)

    async def direct_ip(self) -> None:
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=self.config.navigation_timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.get(self.config.diagnostic_url)
                response.raise_for_status()
            self.evidence.direct_ip = parse_ip_response(response.text)
            self.evidence.direct_ip_masked = mask_ip(self.evidence.direct_ip)
        except (httpx.HTTPError, ValueError):
            self.evidence.direct_ip = None

    async def test_basic(self) -> IpObservation:
        result = self.evidence.tests["Basic proxy connectivity"]
        observation = await self.observe(self.config.logical_references[0], "basic")
        capacity = await self.manager.capacity(repair=False)
        result.observations = 1
        result.masked_example_ip = observation.masked_ip
        result.errors = int(not observation.succeeded)
        if observation.succeeded and capacity.active_contexts == 0:
            result.status = SpikeStatus.PASS
            result.evidence = (
                f"Patchright navigation succeeded in {observation.duration_seconds:.3f}s; "
                "context accounting returned to zero."
            )
        else:
            result.status = SpikeStatus.FAIL
            failure = observation.error.value if observation.error else "CONTEXT_LEAK"
            result.evidence = f"sanitized failure classification: {failure}"
        return observation

    async def test_fresh_contexts(self) -> None:
        result = self.evidence.tests["Fresh-context affinity"]

        async def exercise(logical_reference: str) -> list[IpObservation]:
            observations: list[IpObservation] = []
            for cycle in range(self.config.observations_per_session):
                observations.append(
                    await self.observe(logical_reference, f"fresh-context-{cycle + 1}")
                )
                if cycle + 1 < self.config.observations_per_session:
                    await self.sleeper(self.config.park_interval_seconds)
            return observations

        batches = await asyncio.gather(*(exercise(item) for item in self.config.logical_references))
        all_observations = [item for batch in batches for item in batch]
        for logical_reference, observations in zip(
            self.config.logical_references, batches, strict=True
        ):
            summary = self.evidence.sessions[logical_reference]
            previous_ip: str | None = None
            for index, current in enumerate(observations, start=1):
                affinity = (
                    classify_affinity(previous_ip, current.ip)
                    if index > 1
                    else AffinityResult.UNKNOWN
                )
                self.evidence.fresh_context_cycles.append(
                    FreshContextCycle(
                        logical_reference=logical_reference,
                        observation=index,
                        affinity="BASELINE" if index == 1 else affinity.value,
                        duration_seconds=current.duration_seconds,
                        masked_ip=current.masked_ip,
                        error=current.error.value if current.error else None,
                    )
                )
                previous_ip = current.ip
            for previous, current in pairwise(observations):
                affinity = classify_affinity(previous.ip, current.ip)
                if affinity is AffinityResult.SAME:
                    result.same_count += 1
                    summary.same_count += 1
                elif affinity is AffinityResult.CHANGED:
                    result.changed_count += 1
                    summary.changed_count += 1
        result.observations = len(all_observations)
        result.errors = sum(not item.succeeded for item in all_observations)
        result.masked_example_ip = next(
            (item.masked_ip for item in all_observations if item.masked_ip), None
        )
        if result.errors:
            result.status = SpikeStatus.FAIL
            result.evidence = "one or more new-context observations failed"
        elif result.changed_count:
            result.status = SpikeStatus.FAIL
            result.evidence = (
                f"{result.changed_count} exact IP changes across fresh BrowserContexts"
            )
        else:
            result.status = SpikeStatus.PASS
            result.evidence = (
                f"{result.same_count} comparisons stayed SAME across completely new contexts"
            )

    async def test_browser_restart(self) -> None:
        result = self.evidence.tests["Browser-process restart affinity"]
        previous = {
            logical: next(
                (item.ip for item in reversed(self._observations[logical]) if item.ip), None
            )
            for logical in self.config.logical_references
        }
        await self.manager.shutdown()
        clean_shutdown = (
            self.manager.active_context_count == 0 and self.manager.managed_process_count == 0
        )
        await self.manager.start()
        observations = await asyncio.gather(
            *(self.observe(item, "browser-process-restart") for item in self.config.logical_references)
        )
        for observation in observations:
            affinity = classify_affinity(previous[observation.logical_reference], observation.ip)
            self.evidence.sessions[observation.logical_reference].browser_restart = affinity
            if affinity is AffinityResult.SAME:
                result.same_count += 1
            elif affinity is AffinityResult.CHANGED:
                result.changed_count += 1
        result.observations = len(observations)
        result.errors = sum(not item.succeeded for item in observations)
        result.masked_example_ip = next((item.masked_ip for item in observations if item.ip), None)
        if clean_shutdown and not result.errors and not result.changed_count:
            result.status = SpikeStatus.PASS
            result.evidence = "all tested identities remained SAME after managed browser restart"
        else:
            result.status = SpikeStatus.FAIL
            result.evidence = (
                f"clean pre-restart shutdown={clean_shutdown}; changed={result.changed_count}; "
                f"errors={result.errors}"
            )

    async def test_process_restart(self, state_path: Path) -> None:
        result = self.evidence.tests["Independent Python/application restart affinity"]
        logical = self.config.logical_references[0]
        previous_ip = next(
            (item.ip for item in reversed(self._observations[logical]) if item.ip), None
        )
        if previous_ip is None:
            result.status = SpikeStatus.UNKNOWN
            result.evidence = "no successful pre-restart observation was available"
            return
        await self.manager.shutdown()
        state_document = build_restart_metadata(
            logical_reference=logical,
            ip=previous_ip,
            diagnostic_url=self.config.diagnostic_url,
        )
        write_protected_json(state_path, state_document)
        child_result_path = state_path.with_name("restart-child-result.json")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "queue_load_test.harness.primed_proxy_spike",
            "--mode",
            "restart-child",
            "--restart-state",
            str(state_path),
            "--child-result",
            str(child_result_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        self.evidence.captured_surfaces.extend(
            (
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
            )
        )
        child_document: dict[str, object] = {}
        if process.returncode == 0 and child_result_path.exists():
            child_document = read_protected_json(child_result_path)
            self.evidence.request_count += _integer_field(child_document, "request_count")
        same = child_document.get("affinity") == AffinityResult.SAME.value
        child_error = child_document.get("error")
        result.observations = _integer_field(child_document, "request_count")
        result.same_count = int(same)
        result.changed_count = int(child_document.get("affinity") == AffinityResult.CHANGED.value)
        result.errors = int(process.returncode != 0 or child_error is not None)
        result.masked_example_ip = cast(str | None, child_document.get("masked_ip"))
        affinity = (
            AffinityResult.SAME
            if same
            else AffinityResult.CHANGED
            if result.changed_count
            else AffinityResult.UNKNOWN
        )
        self.evidence.sessions[logical].process_restart = affinity
        result.status = SpikeStatus.PASS if same and not result.errors else SpikeStatus.FAIL
        result.evidence = (
            "a fresh Python process and fresh Patchright manager observed the SAME IP hash"
            if result.status is SpikeStatus.PASS
            else "fresh-process comparison changed or failed"
        )
        self.evidence.protected_state_removed = remove_protected_artifacts(
            (child_result_path, state_path)
        )
        await self.manager.start()

    async def test_inactivity(self) -> None:
        logical = self.config.logical_references[0]
        baseline = await self.observe(logical, "inactivity-baseline")
        previous = baseline.ip
        for interval in self.config.inactivity_intervals_seconds:
            await self.sleeper(interval)
            observation = await self.observe(logical, f"inactivity-{interval:g}s")
            affinity = classify_affinity(previous, observation.ip)
            name = (
                "Inactivity 10s"
                if interval == 10
                else "Inactivity 60s"
                if interval == 60
                else "Optional longer inactivity"
            )
            result = self.evidence.tests[name]
            result.observations += 2
            result.masked_example_ip = observation.masked_ip
            if affinity is AffinityResult.SAME:
                result.same_count += 1
                result.status = SpikeStatus.PASS
                result.evidence = f"SAME after {interval:g}s completely disconnected"
                summary = self.evidence.sessions[logical]
                summary.longest_same_interval_seconds = max(
                    summary.longest_same_interval_seconds or 0,
                    interval,
                )
            elif affinity is AffinityResult.CHANGED:
                result.changed_count += 1
                result.status = SpikeStatus.PARTIAL
                result.evidence = (
                    f"provider reassigned the exit IP after {interval:g}s; not a Quetty code error"
                )
            else:
                result.errors += 1
                result.status = SpikeStatus.UNKNOWN
                result.evidence = f"observation error after {interval:g}s"
            previous = observation.ip
            self.evidence.inactivity_intervals_tested += (interval,)

    async def test_concurrency(self) -> None:
        result = self.evidence.tests["Three-session concurrency"]
        observations = await asyncio.gather(
            *(self.observe(item, "concurrency") for item in self.config.logical_references)
        )
        capacity = await self.manager.capacity(repair=False)
        successful = [item for item in observations if item.succeeded]
        ips = [item.ip for item in successful if item.ip]
        self.evidence.collisions = len(ips) - len(set(ips))
        result.observations = len(observations)
        result.errors = len(observations) - len(successful)
        result.masked_example_ip = next((item.masked_ip for item in successful), None)
        configs = [self.config.proxy_for(item).username for item in self.config.logical_references]
        self.evidence.configuration_borrow_suspected = len(configs) != len(set(configs))
        if len(successful) == len(observations) and capacity.active_contexts == 0:
            result.status = SpikeStatus.PASS
            result.evidence = (
                f"{len(successful)}/{len(observations)} succeeded; collisions="
                f"{self.evidence.collisions}; distinct configurations retained"
            )
        else:
            result.status = SpikeStatus.FAIL
            result.evidence = (
                f"{len(successful)}/{len(observations)} succeeded; final active contexts="
                f"{capacity.active_contexts}"
            )

    async def test_bypass(self) -> None:
        result = self.evidence.tests["Bypass check"]
        await self.direct_ip()
        proxy_ip = next(
            (
                item.ip
                for values in self._observations.values()
                for item in values
                if item.ip is not None
            ),
            None,
        )
        result.observations = int(proxy_ip is not None) + int(self.evidence.direct_ip is not None)
        result.masked_example_ip = mask_ip(proxy_ip) if proxy_ip else None
        if proxy_ip is None or self.evidence.direct_ip is None:
            result.status = SpikeStatus.UNKNOWN
            result.evidence = "direct or proxied public-IP observation failed"
        elif proxy_ip == self.evidence.direct_ip:
            result.status = SpikeStatus.PARTIAL
            result.evidence = "proxy exit matched direct egress; bypass suspected/inconclusive"
        else:
            result.status = SpikeStatus.PASS
            result.evidence = "proxy-observed exit differed from separately observed direct egress"

    async def test_failures(self) -> None:
        result = self.evidence.tests["Failure cleanup"]
        synthetic_passed = all(
            (
                load_spike_config({LIVE_GATE: "1"}).config is None,
                load_spike_config(
                    {
                        LIVE_GATE: "1",
                        "PRIMED_PROXY_SERVER": "not-a-server",
                        "PRIMED_PROXY_USERNAME": "x",
                        "PRIMED_PROXY_PASSWORD": "y",
                        "PRIMED_PROXY_USERNAME_TEMPLATE": "{username}-{session_id}",
                    }
                ).config
                is None,
                load_spike_config(
                    {
                        LIVE_GATE: "1",
                        "PRIMED_PROXY_SERVER": "http://proxy.invalid:1",
                        "PRIMED_PROXY_USERNAME": "x",
                        "PRIMED_PROXY_PASSWORD": "y",
                        "PRIMED_PROXY_USERNAME_TEMPLATE": "{username}",
                    }
                ).config
                is None,
            )
        )
        unreachable = await self.observe(
            self.config.logical_references[0],
            "unreachable-endpoint",
            url="https://127.0.0.1:1/",
        )
        authentication_exercised = False
        authentication_failed_safely = True
        if self.config.test_authentication_failure:
            authentication_exercised = True
            valid = self.config.proxy_for(self.config.logical_references[0])
            invalid = PrimedProxyConfiguration(
                logical_reference=valid.logical_reference,
                sticky_session_id=valid.sticky_session_id,
                server=valid.server,
                username=valid.username,
                password=f"invalid-{hashlib.sha256(valid.password.encode()).hexdigest()[:12]}",
            )
            auth_observation = await self.observe(
                valid.logical_reference,
                "invalid-authentication",
                proxy_override=invalid,
            )
            authentication_failed_safely = (
                not auth_observation.succeeded
                and auth_observation.error is FailureClass.AUTHENTICATION
            )
        recovery = await self.observe(self.config.logical_references[0], "post-failure-recovery")
        capacity = await self.manager.capacity(repair=False)
        result.observations = 2 + int(authentication_exercised)
        result.errors = int(unreachable.succeeded) + int(not authentication_failed_safely)
        if (
            synthetic_passed
            and not unreachable.succeeded
            and recovery.succeeded
            and capacity.active_contexts == 0
            and authentication_failed_safely
        ):
            result.status = SpikeStatus.PASS
            auth_note = "exercised" if authentication_exercised else "not requested"
            result.evidence = (
                "missing password, malformed server, invalid template, unreachable endpoint, "
                f"cleanup, and recovery behaved safely; invalid auth={auth_note}"
            )
        else:
            result.status = SpikeStatus.FAIL
            result.evidence = "one or more sanitized failure/cleanup/recovery checks failed"

    def secret_audit(self) -> None:
        result = self.evidence.tests["Secret leakage audit"]
        safe_json = json.dumps(safe_evidence_dict(self.evidence), sort_keys=True, default=str)
        surfaces = [
            safe_json,
            repr(self.config),
            *(repr(self.config.proxy_for(item)) for item in self.config.logical_references),
            *self.evidence.captured_surfaces,
        ]
        secrets = self.config.secret_candidates()
        leaked = any(secret in surface for secret in secrets for surface in surfaces)
        result.status = SpikeStatus.FAIL if leaked else SpikeStatus.PASS
        result.evidence = (
            "a supplied credential or authenticated proxy URI appeared in an audited surface"
            if leaked
            else "credentials and authenticated proxy URIs were absent from logs, reprs, JSON, markdown inputs, and temporary artifacts"
        )

    async def run(self, state_path: Path) -> SpikeEvidence:
        await self.manager.start()
        try:
            self.evidence.browser_version = self.manager.backend_diagnostics().browser_version
            await self.test_basic()
            await self.test_bypass()
            await self.test_fresh_contexts()
            await self.test_browser_restart()
            await self.test_process_restart(state_path)
            await self.test_inactivity()
            await self.test_concurrency()
            await self.test_failures()
        finally:
            await self.manager.shutdown()
        self.evidence.final_context_count = self.manager.active_context_count
        self.evidence.final_process_count = self.manager.managed_process_count
        current_browser_processes = browser_main_process_ids()
        if (
            current_browser_processes is not None
            and self._baseline_browser_processes is not None
        ):
            self.evidence.orphan_process = bool(
                current_browser_processes - self._baseline_browser_processes
            )
        else:
            self.evidence.orphan_process = None
        context_result = self.evidence.tests["Final context count"]
        context_result.status = (
            SpikeStatus.PASS if self.evidence.final_context_count == 0 else SpikeStatus.FAIL
        )
        context_result.evidence = f"final active contexts={self.evidence.final_context_count}"
        process_result = self.evidence.tests["Final managed browser-process count"]
        process_result.status = (
            SpikeStatus.FAIL
            if self.evidence.final_process_count != 0 or self.evidence.orphan_process is True
            else SpikeStatus.PASS
            if self.evidence.orphan_process is False
            else SpikeStatus.PARTIAL
        )
        process_result.evidence = (
            f"final managed processes={self.evidence.final_process_count}; "
            f"OS orphan check={self.evidence.orphan_process}"
        )
        self.secret_audit()
        self.evidence.outcome = determine_outcome(self.evidence)
        return self.evidence


def determine_outcome(evidence: SpikeEvidence) -> str:
    required = (
        "Basic proxy connectivity",
        "Fresh-context affinity",
        "Browser-process restart affinity",
        "Independent Python/application restart affinity",
        "Bypass check",
        "Secret leakage audit",
        "Final context count",
        "Final managed browser-process count",
    )
    statuses = {name: evidence.tests[name].status for name in required}
    if all(status is SpikeStatus.PASS for status in statuses.values()):
        inactivity = [
            evidence.tests[name].status
            for name in ("Inactivity 10s", "Inactivity 60s", "Optional longer inactivity")
            if evidence.tests[name].status is not SpikeStatus.NOT_RUN
        ]
        return (
            "SPIKE_PARTIAL"
            if any(status is SpikeStatus.PARTIAL for status in inactivity)
            else "SPIKE_PASS"
        )
    if any(
        statuses[name] is SpikeStatus.FAIL
        for name in (
            "Basic proxy connectivity",
            "Bypass check",
            "Secret leakage audit",
            "Final context count",
            "Final managed browser-process count",
        )
    ):
        return "SPIKE_FAIL"
    return "SPIKE_PARTIAL"


def safe_evidence_dict(evidence: SpikeEvidence) -> dict[str, object]:
    return {
        "started_at": evidence.started_at.isoformat(),
        "config_ready": evidence.config_ready,
        "missing_configuration": evidence.missing_configuration,
        "configuration_errors": evidence.configuration_errors,
        "tests": {name: asdict(result) for name, result in evidence.tests.items()},
        "sessions": {
            name: {
                **asdict(summary),
                "errors": sorted(summary.errors),
            }
            for name, summary in evidence.sessions.items()
        },
        "request_count": evidence.request_count,
        "diagnostic_url": evidence.diagnostic_url,
        "direct_ip_masked": evidence.direct_ip_masked,
        "browser_version": evidence.browser_version,
        "final_context_count": evidence.final_context_count,
        "final_process_count": evidence.final_process_count,
        "protected_state_removed": evidence.protected_state_removed,
        "temporary_files_remaining": evidence.temporary_files_remaining,
        "orphan_process": evidence.orphan_process,
        "collisions": evidence.collisions,
        "configuration_borrow_suspected": evidence.configuration_borrow_suspected,
        "inactivity_intervals_tested": evidence.inactivity_intervals_tested,
        "fresh_context_cycles": [asdict(item) for item in evidence.fresh_context_cycles],
        "outcome": evidence.outcome,
    }


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not installed"


def _status_text(result: SpikeTestResult) -> str:
    return result.status.value


def render_report(evidence: SpikeEvidence) -> str:
    intervals = sorted(set(evidence.inactivity_intervals_tested))
    missing = ", ".join(evidence.missing_configuration) or "none"
    errors = "; ".join(evidence.configuration_errors) or "none"
    lines = [
        "# Primed Residential Proxy Spike Results",
        "",
        "## Date and Environment",
        "",
        f"- Date/time: {evidence.started_at.isoformat()}",
        f"- OS: {platform.platform()}",
        f"- Python version: {platform.python_version()}",
        f"- Quetty commit: `{_git_commit()}`",
        "- Browser backend: Patchright with installed Google Chrome",
        f"- Patchright version: {_package_version('patchright')}",
        f"- Installed Chrome version: {evidence.browser_version or 'NOT RUN / unknown'}",
        "- Primed product: Residential",
        f"- Logical session count: {len(evidence.sessions)}",
        f"- Proxy-routed request/navigation count: {evidence.request_count}",
        f"- Diagnostic IP service used: `{evidence.diagnostic_url}`",
        "- Inactivity intervals tested: "
        + (", ".join(f"{item:g}s" for item in intervals) if intervals else "NOT RUN"),
        f"- Live gate: {'enabled' if evidence.config_ready else 'not ready'}",
        f"- Missing configuration: {missing}",
        f"- Configuration errors: {errors}",
        "",
        "No Queue-it or other production/unauthorised target was contacted.",
        "",
        "## Configuration Safety",
        "",
        f"- Credentials source: {evidence.credentials_source}.",
        "- Environment file mode: "
        + (evidence.environment_file_mode or "not applicable / unknown"),
        "- Credentials were not written to SQLite or any ordinary result file.",
        "- Credential leakage audit: "
        + _status_text(evidence.tests["Secret leakage audit"]),
        "- Raw exit IPs were compared in memory and were not retained in this report.",
        "- Cross-process comparison uses a salted SHA-256 value, never the raw IP.",
        "- Protected restart artifacts: "
        + (
            "removed"
            if evidence.protected_state_removed is True
            else "not created"
            if evidence.protected_state_removed is None
            else "removal failed"
        ),
        "",
        "## Test Results",
        "",
        "| Test | Result | Observations | SAME | CHANGED | Errors | Masked example | Evidence |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for name in TEST_NAMES:
        result = evidence.tests[name]
        lines.append(
            f"| {name} | {result.status.value} | {result.observations} | "
            f"{result.same_count} | {result.changed_count} | {result.errors} | "
            f"{result.masked_example_ip or '—'} | {result.evidence} |"
        )
    lines.extend(
        [
            "",
            "### Fresh-context cycle detail",
            "",
            "| Logical session | Observation | Result vs prior context | Duration | Masked IP | Error |",
            "|---|---:|---|---:|---|---|",
        ]
    )
    if evidence.fresh_context_cycles:
        for cycle in evidence.fresh_context_cycles:
            lines.append(
                f"| {cycle.logical_reference} | {cycle.observation} | {cycle.affinity} | "
                f"{cycle.duration_seconds:.3f}s | {cycle.masked_ip or '—'} | "
                f"{cycle.error or 'none'} |"
            )
    else:
        lines.append("| — | 0 | NOT RUN | — | — | none |")
    lines.extend(
        [
            "",
            "## Per-Logical-Session Summary",
            "",
            "| Logical session reference | Observations | SAME / CHANGED | Masked IP | Browser restart | Process restart | Longest disconnected interval with same IP | Sanitized errors |",
            "|---|---:|---|---|---|---|---|---|",
        ]
    )
    for summary in evidence.sessions.values():
        longest_label = (
            f"{summary.longest_same_interval_seconds:g}s"
            if summary.longest_same_interval_seconds is not None
            else "UNKNOWN"
        )
        lines.append(
            f"| {summary.logical_reference} | {summary.observations} | "
            f"{summary.same_count} / {summary.changed_count} | {summary.masked_ip or '—'} | "
            f"{summary.browser_restart.value} | {summary.process_restart.value} | "
            f"{longest_label} | {', '.join(sorted(summary.errors)) or 'none'} |"
        )
    basic = evidence.tests["Basic proxy connectivity"]
    fresh = evidence.tests["Fresh-context affinity"]
    browser_restart = evidence.tests["Browser-process restart affinity"]
    process_restart = evidence.tests["Independent Python/application restart affinity"]
    bypass = evidence.tests["Bypass check"]
    concurrency = evidence.tests["Three-session concurrency"]
    cleanup_ok = (
        evidence.tests["Final context count"].status is SpikeStatus.PASS
        and evidence.tests["Final managed browser-process count"].status is SpikeStatus.PASS
    )
    longest_values = [
        item.longest_same_interval_seconds
        for item in evidence.sessions.values()
        if item.longest_same_interval_seconds is not None
    ]
    longest_interval = max(longest_values) if longest_values else None

    def decision(status: SpikeStatus, statement: str) -> str:
        normalized = status if status in {
            SpikeStatus.PASS,
            SpikeStatus.FAIL,
            SpikeStatus.PARTIAL,
        } else SpikeStatus.UNKNOWN
        return f"{normalized.value} — {statement}"

    lines.extend(
        [
            "",
            "## Provider-Behavior Observations",
            "",
            "- Quetty/browser integration failures: "
            + ("none observed" if basic.status is SpikeStatus.PASS else basic.evidence),
            "- Primed authentication failures: "
            + (
                "none observed"
                if basic.status is SpikeStatus.PASS
                else "UNKNOWN or classified in the results table"
            ),
            f"- Provider IP reassignment: {fresh.changed_count} immediate fresh-context changes; inactivity results are shown separately.",
            "- Provider timeout/network failures: see Failure cleanup and sanitized session errors.",
            "- Unknown/inconclusive behavior: every NOT RUN or UNKNOWN row remains unclaimed.",
            (
                "- Cross-session IP collisions are observational only and are not failures: "
                f"{evidence.collisions} collision(s)."
            ),
            "",
            "## Cleanup",
            "",
            f"- Active contexts after run: {evidence.final_context_count if evidence.final_context_count is not None else 'NOT RUN'}",
            f"- Managed browser processes after shutdown: {evidence.final_process_count if evidence.final_process_count is not None else 'NOT RUN'}",
            f"- Temporary files remaining: {evidence.temporary_files_remaining}",
            "- Protected spike-state file: "
            + (
                "removed"
                if evidence.protected_state_removed is True
                else "not created"
                if evidence.protected_state_removed is None
                else "not removed"
            ),
            "- Orphan process: "
            + ("no" if evidence.orphan_process is False else "NOT RUN / unknown" if evidence.orphan_process is None else "yes"),
            "",
            "## Decision Matrix",
            "",
            "1. Can Patchright connect through Primed Residential? "
            + decision(basic.status, basic.evidence),
            "2. Is traffic demonstrably proxied? " + decision(bypass.status, bypass.evidence),
            "3. Can the proxy be configured per temporary BrowserContext? "
            + decision(
                concurrency.status,
                (
                    "all temporary contexts completed with distinct logical proxy "
                    "configurations; IP reassignment is assessed separately"
                    if concurrency.status is SpikeStatus.PASS
                    else concurrency.evidence
                ),
            ),
            "4. Does the same Primed sticky-session identity retain its IP after BrowserContext destruction? "
            + decision(fresh.status, fresh.evidence),
            "5. Does it retain its IP after managed browser-process destruction? "
            + decision(browser_restart.status, browser_restart.evidence),
            "6. Does it retain its IP across a new Python/application process? "
            + decision(process_restart.status, process_restart.evidence),
            "7. What is the longest tested disconnected interval that retained the same IP? "
            + decision(
                SpikeStatus.PASS if longest_interval is not None else SpikeStatus.UNKNOWN,
                (
                    f"{longest_interval:g}s"
                    if longest_interval is not None
                    else "no live interval was proven"
                ),
            ),
            "8. Were multiple logical proxy sessions usable concurrently? "
            + decision(concurrency.status, concurrency.evidence),
            "9. Did any test require keeping a browser/context alive to preserve affinity? "
            + decision(
                SpikeStatus.PARTIAL if fresh.changed_count else fresh.status,
                (
                    "no context/browser was retained, but affinity was not consistently "
                    "preserved after destruction"
                    if fresh.changed_count
                    else "no; every affinity observation closed the prior context"
                ),
            ),
            "10. Did credentials remain absent from normal persistence/logging/results? "
            + decision(
                evidence.tests["Secret leakage audit"].status,
                evidence.tests["Secret leakage audit"].evidence,
            ),
            "11. Did all resources cleanly return to baseline? "
            + decision(
                SpikeStatus.PASS if cleanup_ok else SpikeStatus.UNKNOWN if not evidence.config_ready else SpikeStatus.FAIL,
                "final manager accounting is zero" if cleanup_ok else "not established",
            ),
            "12. Were any IP changes observed, and under what condition? "
            + decision(
                SpikeStatus.PARTIAL
                if any(item.changed_count for item in evidence.tests.values())
                else SpikeStatus.PASS
                if evidence.config_ready
                else SpikeStatus.UNKNOWN,
                "see the per-test SAME/CHANGED counts; no behavior is generalized beyond tested intervals",
            ),
            "",
            "## Final Outcome",
            "",
            f"`{evidence.outcome}`",
            "",
            (
                "The live Primed probe did not run, so provider compatibility and affinity remain UNKNOWN."
                if not evidence.config_ready
                else "This outcome is limited to the exact endpoint, host, configuration, and intervals recorded above."
            ),
            "",
            "## Implementation Recommendation",
            "",
        ]
    )
    if evidence.outcome == "SPIKE_PASS":
        lines.extend(
            [
                "A future production design may use:",
                "",
                "`QueueSession -> immutable proxy assignment -> deterministic Primed sticky-session identity -> create temporary proxied BrowserContext -> normal Queue ID restoration/verification -> inspect -> persist -> close/park`",
                "",
                "Queue ID must remain authoritative. Observed proxy IP remains observational metadata, never session identity.",
                "",
                "Exact follow-up:",
                "",
                "`Design and implement persisted per-session Primed Residential proxy assignments in Quetty using the proven sticky-session reconstruction mechanism, without changing Queue ID authority or bounded parked-session architecture.`",
            ]
        )
    else:
        lines.append(
            "Do not implement production proxy assignment yet. Collect the missing or failed live evidence first; Queue ID remains authoritative and proxy IP must never become identity."
        )
    lines.extend(
        [
            "",
            (
                "Primed syntax was not guessed. The operator must supply the exact "
                "documented username template through environment configuration; see "
                f"[{PRIMED_DOCUMENTATION}]({PRIMED_DOCUMENTATION})."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def not_run_evidence(load: ConfigLoadResult) -> SpikeEvidence:
    missing = list(load.missing)
    if not load.gate_enabled:
        missing.insert(0, f"{LIVE_GATE}=1")
    evidence = SpikeEvidence(
        started_at=datetime.now(UTC),
        config_ready=False,
        missing_configuration=tuple(missing),
        configuration_errors=load.errors,
        credentials_source=load.credentials_source,
        environment_file_mode=load.environment_file_mode,
        tests=_initial_tests(),
        sessions={item: SessionSummary(item) for item in LOGICAL_REFERENCES},
    )
    for result in evidence.tests.values():
        result.status = SpikeStatus.NOT_RUN
        result.evidence = "live probe was fail-closed because its gate/configuration was absent"
    evidence.outcome = "SPIKE_PARTIAL"
    return evidence


async def run_restart_child(
    config: PrimedSpikeConfig,
    state_path: Path,
    child_result_path: Path,
) -> int:
    try:
        state = read_protected_json(state_path)
        if state.get("format") != _RESTART_FORMAT or state.get("version") != _RESTART_VERSION:
            raise SpikeConfigurationError("protected restart metadata has an unsupported format")
        logical = state.get("logical_reference")
        salt = state.get("salt")
        previous_digest = state.get("previous_ip_sha256")
        if not all(isinstance(item, str) for item in (logical, salt, previous_digest)):
            raise SpikeConfigurationError("protected restart metadata is incomplete")
        runner = PrimedSpikeRunner(config)
        await runner.manager.start()
        try:
            observation = await runner.observe(cast(str, logical), "independent-process-restart")
        finally:
            await runner.manager.shutdown()
        affinity = compare_restart_observation(state, observation.ip)
        result: dict[str, object] = {
            "format": _RESTART_FORMAT,
            "version": _RESTART_VERSION,
            "logical_reference": logical,
            "affinity": affinity.value,
            "masked_ip": observation.masked_ip,
            "error": observation.error.value if observation.error else None,
            "request_count": runner.evidence.request_count,
            "final_context_count": runner.manager.active_context_count,
            "final_process_count": runner.manager.managed_process_count,
        }
        write_protected_json(child_result_path, result)
        return 0 if observation.succeeded else 1
    except Exception as exc:  # noqa: BLE001 - child emits only a closed class
        write_protected_json(
            child_result_path,
            {
                "format": _RESTART_FORMAT,
                "version": _RESTART_VERSION,
                "affinity": AffinityResult.UNKNOWN.value,
                "error": classify_failure(exc).value,
                "request_count": 0,
            },
        )
        return 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("full", "restart-prepare", "restart-resume", "restart-child"),
        default="full",
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--restart-state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--child-result", type=Path)
    return parser.parse_args()


async def _prepare_restart(config: PrimedSpikeConfig, state_path: Path) -> SpikeEvidence:
    runner = PrimedSpikeRunner(config)
    await runner.manager.start()
    try:
        observation = await runner.observe(config.logical_references[0], "restart-prepare")
    finally:
        await runner.manager.shutdown()
    if observation.ip is None:
        runner.evidence.tests["Independent Python/application restart affinity"].status = (
            SpikeStatus.FAIL
        )
        return runner.evidence
    write_protected_json(
        state_path,
        build_restart_metadata(
            logical_reference=observation.logical_reference,
            ip=observation.ip,
            diagnostic_url=config.diagnostic_url,
        ),
    )
    test = runner.evidence.tests["Independent Python/application restart affinity"]
    test.status = SpikeStatus.UNKNOWN
    test.observations = 1
    test.masked_example_ip = observation.masked_ip
    test.evidence = "prepare phase completed; run restart-resume in a new process"
    runner.evidence.final_context_count = runner.manager.active_context_count
    runner.evidence.final_process_count = runner.manager.managed_process_count
    runner.evidence.protected_state_removed = False
    runner.evidence.outcome = "SPIKE_PARTIAL"
    return runner.evidence


async def _resume_restart(config: PrimedSpikeConfig, state_path: Path) -> SpikeEvidence:
    child_result = state_path.with_name("restart-resume-result.json")
    code = await run_restart_child(config, state_path, child_result)
    evidence = SpikeEvidence(
        started_at=datetime.now(UTC),
        config_ready=True,
        missing_configuration=(),
        configuration_errors=(),
        credentials_source="environment configuration",
        environment_file_mode=None,
        tests=_initial_tests(),
        sessions={item: SessionSummary(item) for item in config.logical_references},
        diagnostic_url=config.diagnostic_url,
    )
    document = read_protected_json(child_result)
    test = evidence.tests["Independent Python/application restart affinity"]
    affinity = document.get("affinity")
    test.status = SpikeStatus.PASS if code == 0 and affinity == "SAME" else SpikeStatus.FAIL
    test.observations = _integer_field(document, "request_count")
    test.same_count = int(affinity == "SAME")
    test.changed_count = int(affinity == "CHANGED")
    test.errors = int(document.get("error") is not None)
    test.masked_example_ip = cast(str | None, document.get("masked_ip"))
    test.evidence = "comparison completed in this independent Python process"
    evidence.request_count = test.observations
    evidence.final_context_count = cast(int | None, document.get("final_context_count"))
    evidence.final_process_count = cast(int | None, document.get("final_process_count"))
    evidence.protected_state_removed = remove_protected_artifacts(
        (child_result, state_path)
    )
    evidence.outcome = "SPIKE_PARTIAL"
    return evidence


async def async_main(args: argparse.Namespace) -> int:
    load = load_spike_config()
    if args.mode == "restart-child":
        if not load.ready or load.config is None or args.child_result is None:
            return 2
        return await run_restart_child(load.config, args.restart_state, args.child_result)

    if not load.ready or load.config is None:
        evidence = not_run_evidence(load)
        args.report.write_text(render_report(evidence), encoding="utf-8")
        return 2

    if args.mode == "restart-prepare":
        evidence = await _prepare_restart(load.config, args.restart_state)
    elif args.mode == "restart-resume":
        evidence = await _resume_restart(load.config, args.restart_state)
    else:
        evidence = await PrimedSpikeRunner(load.config).run(args.restart_state)
    evidence.credentials_source = load.credentials_source
    evidence.environment_file_mode = load.environment_file_mode
    rendered = render_report(evidence)
    # The final markdown itself participates in the leakage audit before persistence.
    if any(secret in rendered for secret in load.config.secret_candidates()):
        evidence.tests["Secret leakage audit"].status = SpikeStatus.FAIL
        evidence.tests["Secret leakage audit"].evidence = (
            "a supplied credential appeared in the rendered markdown"
        )
        evidence.outcome = "SPIKE_FAIL"
        rendered = render_report(evidence)
    args.report.write_text(rendered, encoding="utf-8")
    return 0 if evidence.outcome == "SPIKE_PASS" else 1


def main() -> None:
    raise SystemExit(asyncio.run(async_main(_parse_args())))


if __name__ == "__main__":
    main()
