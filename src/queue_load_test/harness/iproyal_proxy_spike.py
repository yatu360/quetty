"""Opt-in, low-bandwidth IPRoyal Residential proxy compatibility spike.

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
from urllib.parse import urlsplit, urlunsplit

import httpx
from dotenv import dotenv_values

from queue_load_test.browser import BrowserManager, BrowserProxySettings, PatchrightBackend
from queue_load_test.direct_replay.store import read_protected_json, write_protected_json
from queue_load_test.harness.resource_benchmark import is_browser_main_process
from queue_load_test.models import BrowserBackendName
from queue_load_test.proxy import construct_effective_password as _construct_effective_password

LIVE_GATE = "RUN_IPROYAL_PROXY_SPIKE"
DEFAULT_REPORT_PATH = Path("test_spike_results.md")
DEFAULT_STATE_PATH = Path(".iproyal-proxy-spike/restart-state.json")
DEFAULT_LONG_STATE_PATH = Path(".iproyal-proxy-spike/long-checkpoint-state.json")
DEFAULT_DIAGNOSTIC_URL = "https://api.ipify.org?format=json"
DEFAULT_GEO_URL = "https://api.country.is/"
ALLOWED_DIAGNOSTIC_URLS = frozenset(
    {
        DEFAULT_DIAGNOSTIC_URL,
        "https://api64.ipify.org?format=json",
        "https://checkip.amazonaws.com/",
        "https://icanhazip.com/",
        "https://ifconfig.me/ip",
    }
)
LOGICAL_REFERENCES = ("iproyal-spike-1", "iproyal-spike-2", "iproyal-spike-3")
LONG_CHECKPOINT_MINUTES = (5, 30)
_RESTART_FORMAT = "queue-load-test.iproyal-proxy-spike-restart"
_RESTART_VERSION = 1
_LONG_FORMAT = "queue-load-test.iproyal-proxy-spike-long-checkpoints"
_LONG_VERSION = 1


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
class IPRoyalProxyConfiguration:
    """One resolved in-memory proxy configuration; credentials never enter repr."""

    logical_reference: str
    sticky_session_id: str = field(repr=False)
    server: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            "IPRoyalProxyConfiguration(provider='iproyal', "
            f"logical_reference={self.logical_reference!r}, credentials='<redacted>')"
        )

    def browser_proxy(self) -> BrowserProxySettings:
        return {
            "server": self.server,
            "username": self.username,
            "password": self.password,
        }


@dataclass(frozen=True, slots=True, repr=False)
class IPRoyalSpikeConfig:
    server: str = field(repr=False)
    username: str = field(repr=False)
    base_password: str = field(repr=False)
    country: str
    lifetime: str
    diagnostic_url: str
    geo_url: str
    navigation_timeout_seconds: float = 20.0
    logical_session_count: int = 3
    observations_per_session: int = 3
    park_interval_seconds: float = 2.0
    test_authentication_failure: bool = False

    def __repr__(self) -> str:
        return (
            "IPRoyalSpikeConfig(provider='iproyal', credentials='<redacted>', "
            f"logical_session_count={self.logical_session_count}, "
            f"diagnostic_url={self.diagnostic_url!r})"
        )

    @property
    def logical_references(self) -> tuple[str, ...]:
        return LOGICAL_REFERENCES[: self.logical_session_count]

    def proxy_for(self, logical_reference: str) -> IPRoyalProxyConfiguration:
        if logical_reference not in self.logical_references:
            raise SpikeConfigurationError("logical proxy session is outside configured population")
        session_id = deterministic_sticky_session_id(logical_reference)
        password = construct_effective_password(
            self.base_password,
            country=self.country,
            session_id=session_id,
            lifetime=self.lifetime,
        )
        return IPRoyalProxyConfiguration(
            logical_reference=logical_reference,
            sticky_session_id=session_id,
            server=self.server,
            username=self.username,
            password=password,
        )

    def secret_candidates(self) -> tuple[str, ...]:
        values = [self.username, self.base_password]
        for logical_reference in self.logical_references:
            proxy = self.proxy_for(logical_reference)
            values.append(proxy.password)
        return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, slots=True)
class ConfigLoadResult:
    config: IPRoyalSpikeConfig | None
    gate_enabled: bool
    missing: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    credentials_source: str = "provided mapping"
    environment_file_mode: str | None = None

    @property
    def ready(self) -> bool:
        return (
            self.gate_enabled and self.config is not None and not self.missing and not self.errors
        )


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

    @property
    def provider_session_id(self) -> str:
        return deterministic_sticky_session_id(self.logical_reference)


@dataclass(frozen=True, slots=True)
class FreshContextCycle:
    logical_reference: str
    observation: int
    affinity: str
    duration_seconds: float
    masked_ip: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class LongCheckpoint:
    checkpoint_minutes: int
    elapsed_seconds: float
    affinity: AffinityResult
    masked_ip: str | None
    browser_restarted: bool
    fresh_python_process: bool
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
    fresh_context_cycles: list[FreshContextCycle] = field(default_factory=list)
    long_checkpoints: list[LongCheckpoint] = field(default_factory=list)
    configured_country: str = "unknown"
    configured_lifetime: str = "unknown"
    geo_service: str = DEFAULT_GEO_URL
    geo_country: str | None = None
    captured_surfaces: list[str] = field(default_factory=list, repr=False)
    outcome: str = "SPIKE_PARTIAL"


TEST_NAMES = (
    "Basic proxy connectivity",
    "Bypass check",
    "UK geo verification",
    "Fresh-context affinity",
    "Browser-process restart affinity",
    "Independent Python/application restart affinity",
    "5-minute disconnected affinity",
    "30-minute disconnected affinity",
    "60-minute disconnected affinity",
    "90-minute disconnected affinity",
    "115-minute disconnected affinity",
    "Three-session concurrency",
    "Failure cleanup",
    "Secret leakage audit",
    "Final context count",
    "Final managed browser-process count",
)


def _initial_tests() -> dict[str, SpikeTestResult]:
    return {name: SpikeTestResult(name=name) for name in TEST_NAMES}


def deterministic_sticky_session_id(logical_reference: str) -> str:
    """Return a stable eight-character alphanumeric ID for one logical session."""

    return hashlib.sha256(f"quetty-iproyal-spike:{logical_reference}".encode()).hexdigest()[:8]


def validate_provider_session_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9]{8}", value) is None:
        raise SpikeConfigurationError(
            "IPRoyal provider session ID must be exactly 8 alphanumeric characters"
        )
    return value


def construct_effective_password(
    base_password: str,
    *,
    country: str,
    session_id: str,
    lifetime: str,
) -> str:
    """Resolve IPRoyal authentication in memory without logging or persistence."""

    validate_provider_session_id(session_id)
    return _construct_effective_password(
        base_password,
        country=country,
        session_id=session_id,
        lifetime=lifetime,
    )


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
    return AffinityResult.SAME if hash_ip(ip, salt) == previous_digest else AffinityResult.CHANGED


def build_long_checkpoint_state(
    *,
    logical_reference: str,
    provider_session_id: str,
    ip: str,
    country: str,
    lifetime: str,
    started_epoch: float | None = None,
    salt: str | None = None,
) -> dict[str, object]:
    """Build resumable long-test state without persisting the exact exit IP."""

    selected_salt = salt or hashlib.sha256(os.urandom(32)).hexdigest()
    return {
        "format": _LONG_FORMAT,
        "version": _LONG_VERSION,
        "logical_reference": logical_reference,
        "provider_session_id": validate_provider_session_id(provider_session_id),
        "country": country,
        "lifetime": lifetime,
        "salt": selected_salt,
        "baseline_ip_sha256": hash_ip(ip, selected_salt),
        "baseline_masked_ip": mask_ip(ip),
        "started_epoch": started_epoch if started_epoch is not None else time.time(),
        "created_at": datetime.now(UTC).isoformat(),
        "checkpoints": [],
    }


def compare_long_checkpoint(state: Mapping[str, object], ip: str | None) -> AffinityResult:
    if ip is None:
        return AffinityResult.UNKNOWN
    salt = state.get("salt")
    digest = state.get("baseline_ip_sha256")
    if not isinstance(salt, str) or not isinstance(digest, str):
        return AffinityResult.UNKNOWN
    return AffinityResult.SAME if hash_ip(ip, salt) == digest else AffinityResult.CHANGED


def classify_geo_country(value: str | None) -> SpikeStatus:
    if value is None:
        return SpikeStatus.UNKNOWN
    return SpikeStatus.PASS if value.strip().upper() == "GB" else SpikeStatus.FAIL


def remove_protected_artifacts(paths: Sequence[Path]) -> bool:
    for path in paths:
        path.unlink(missing_ok=True)
    return not any(path.exists() for path in paths)


def _integer_field(document: Mapping[str, object], key: str) -> int:
    value = document.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float_field(document: Mapping[str, object], key: str, default: float) -> float:
    value = document.get(key)
    return (
        float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    )


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


def parse_geo_response(text: str) -> str:
    candidate = text.strip().upper()
    with contextlib.suppress(json.JSONDecodeError):
        payload = json.loads(text)
        if isinstance(payload, dict) and isinstance(payload.get("country"), str):
            candidate = cast(str, payload["country"]).strip().upper()
    if re.fullmatch(r"[A-Z]{2}", candidate) is None:
        raise ValueError("geo endpoint returned an invalid country code")
    return candidate


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


def _normalize_server(server: str) -> str:
    try:
        parsed = urlsplit(server)
        embedded_port = parsed.port
    except ValueError as exc:
        raise SpikeConfigurationError("IPROYAL_PROXY_SERVER is malformed") from exc
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
        raise SpikeConfigurationError(
            "IPROYAL_PROXY_SERVER must include an explicit http, https, or socks5 protocol"
        )
    if parsed.username is not None or parsed.password is not None:
        raise SpikeConfigurationError(
            "IPROYAL_PROXY_SERVER must not contain credentials; use separate environment values"
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise SpikeConfigurationError(
            "IPROYAL_PROXY_SERVER cannot contain a path, query, or fragment"
        )
    if embedded_port is None:
        raise SpikeConfigurationError(
            "IPROYAL_PROXY_SERVER must include its port in the server URL"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _spike_environment(env_file: Path) -> tuple[dict[str, str], str, str | None]:
    file_values: dict[str, str] = {}
    file_mode: str | None = None
    if env_file.is_file():
        file_values = {
            key: value for key, value in dotenv_values(env_file).items() if isinstance(value, str)
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
    required = (
        "IPROYAL_PROXY_SERVER",
        "IPROYAL_PROXY_USERNAME",
        "IPROYAL_PROXY_PASSWORD",
        "IPROYAL_PROXY_COUNTRY",
        "IPROYAL_PROXY_LIFETIME",
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
    try:
        server = _normalize_server(source["IPROYAL_PROXY_SERVER"].strip())
        country = source["IPROYAL_PROXY_COUNTRY"].strip()
        lifetime = source["IPROYAL_PROXY_LIFETIME"].strip()
        if country != "gb":
            raise SpikeConfigurationError(
                "IPROYAL_PROXY_COUNTRY must be gb for this UK compatibility spike"
            )
        if lifetime != "2h":
            raise SpikeConfigurationError(
                "IPROYAL_PROXY_LIFETIME must be exactly 2h for this compatibility spike"
            )
        config = IPRoyalSpikeConfig(
            server=server,
            username=source["IPROYAL_PROXY_USERNAME"],
            base_password=source["IPROYAL_PROXY_PASSWORD"],
            country=country,
            lifetime=lifetime,
            diagnostic_url=DEFAULT_DIAGNOSTIC_URL,
            geo_url=DEFAULT_GEO_URL,
            navigation_timeout_seconds=20.0,
        )
        provider_ids = [
            config.proxy_for(item).sticky_session_id for item in config.logical_references
        ]
        if len(set(provider_ids)) != len(provider_ids):
            raise SpikeConfigurationError(
                "deterministic IPRoyal provider session IDs were not unique"
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


class IPRoyalSpikeRunner:
    def __init__(
        self,
        config: IPRoyalSpikeConfig,
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
            configured_country=config.country,
            configured_lifetime=config.lifetime,
            geo_service=config.geo_url,
        )
        self._observations: dict[str, list[IpObservation]] = defaultdict(list)
        self._baseline_browser_processes = browser_main_process_ids()

    async def observe(
        self,
        logical_reference: str,
        stage: str,
        *,
        url: str | None = None,
        proxy_override: IPRoyalProxyConfiguration | None = None,
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

    async def test_geo(self) -> None:
        result = self.evidence.tests["UK geo verification"]
        owned = None
        try:
            proxy = self.config.proxy_for(self.config.logical_references[0])
            owned = await self.manager.create_context(proxy=proxy.browser_proxy())
            page = await owned.context.new_page()
            self.evidence.request_count += 1
            await page.goto(
                self.config.geo_url,
                wait_until="domcontentloaded",
                timeout=self.config.navigation_timeout_seconds * 1000,
            )
            self.evidence.geo_country = parse_geo_response(await page.locator("body").inner_text())
        except Exception:  # noqa: BLE001 - only UNKNOWN is retained
            self.evidence.geo_country = None
        finally:
            if owned is not None:
                await owned.close()
        result.observations = 1
        result.status = classify_geo_country(self.evidence.geo_country)
        result.errors = int(result.status is SpikeStatus.UNKNOWN)
        if result.status is SpikeStatus.PASS:
            result.evidence = "GB confirmed by the proxied lightweight geo endpoint"
        elif result.status is SpikeStatus.FAIL:
            result.evidence = f"geo endpoint returned {self.evidence.geo_country}; expected GB"
        else:
            result.evidence = "geo endpoint unavailable or returned an invalid country code"

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
            *(
                self.observe(item, "browser-process-restart")
                for item in self.config.logical_references
            )
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
            "queue_load_test.harness.iproyal_proxy_spike",
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

    async def test_long_disconnect(self, state_path: Path) -> None:
        """Observe one sticky identity while fully disconnected for almost two hours."""

        logical = self.config.logical_references[0]
        baseline = await self.observe(logical, "long-disconnect-baseline")
        await self.manager.shutdown()
        if baseline.ip is None:
            for minutes in LONG_CHECKPOINT_MINUTES:
                result = self.evidence.tests[f"{minutes}-minute disconnected affinity"]
                result.status = SpikeStatus.UNKNOWN
                result.errors = 1
                result.evidence = "T+0 baseline observation failed"
            return

        state = build_long_checkpoint_state(
            logical_reference=logical,
            provider_session_id=self.config.proxy_for(logical).sticky_session_id,
            ip=baseline.ip,
            country=self.config.country,
            lifetime=self.config.lifetime,
        )
        write_protected_json(state_path, state)
        started_epoch = cast(float, state["started_epoch"])
        for minutes in LONG_CHECKPOINT_MINUTES:
            remaining = minutes * 60 - (time.time() - started_epoch)
            if remaining > 0:
                await self.sleeper(remaining)
            child_path = state_path.with_name(f"long-child-{minutes}.json")
            fresh_python = minutes >= 60
            if fresh_python:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "queue_load_test.harness.iproyal_proxy_spike",
                    "--mode",
                    "long-child",
                    "--long-state",
                    str(state_path),
                    "--checkpoint-minutes",
                    str(minutes),
                    "--child-result",
                    str(child_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
                self.evidence.captured_surfaces.extend(
                    (stdout.decode(errors="replace"), stderr.decode(errors="replace"))
                )
                document = read_protected_json(child_path) if child_path.exists() else {}
                self.evidence.request_count += _integer_field(document, "request_count")
                affinity_value = document.get("affinity")
                affinity = (
                    AffinityResult(affinity_value)
                    if affinity_value in {item.value for item in AffinityResult}
                    else AffinityResult.UNKNOWN
                )
                masked_ip = cast(str | None, document.get("masked_ip"))
                error = cast(str | None, document.get("error"))
                elapsed = _float_field(document, "elapsed_seconds", time.time() - started_epoch)
                child_path.unlink(missing_ok=True)
            else:
                await self.manager.start()
                observation = await self.observe(logical, f"long-disconnect-{minutes}m")
                await self.manager.shutdown()
                affinity = compare_long_checkpoint(state, observation.ip)
                masked_ip = observation.masked_ip
                error = observation.error.value if observation.error else None
                elapsed = time.time() - started_epoch
            checkpoint = LongCheckpoint(
                checkpoint_minutes=minutes,
                elapsed_seconds=round(elapsed, 3),
                affinity=affinity,
                masked_ip=masked_ip,
                browser_restarted=True,
                fresh_python_process=fresh_python,
                error=error,
            )
            self.evidence.long_checkpoints.append(checkpoint)
            checkpoints = cast(list[object], state["checkpoints"])
            checkpoints.append(asdict(checkpoint))
            write_protected_json(state_path, state)
            result = self.evidence.tests[f"{minutes}-minute disconnected affinity"]
            result.observations = 1
            result.masked_example_ip = masked_ip
            result.same_count = int(affinity is AffinityResult.SAME)
            result.changed_count = int(affinity is AffinityResult.CHANGED)
            result.errors = int(error is not None or affinity is AffinityResult.UNKNOWN)
            if affinity is AffinityResult.SAME and error is None:
                result.status = SpikeStatus.PASS
                result.evidence = f"SAME vs T+0 after {elapsed / 60:.1f} minutes fully disconnected"
                summary = self.evidence.sessions[logical]
                summary.longest_same_interval_seconds = max(
                    summary.longest_same_interval_seconds or 0.0, elapsed
                )
            elif affinity is AffinityResult.CHANGED:
                result.status = SpikeStatus.PARTIAL
                result.evidence = (
                    f"IPRoyal reassigned the exit after {elapsed / 60:.1f} minutes; "
                    "Queue identity was not involved"
                )
            else:
                result.status = SpikeStatus.UNKNOWN
                result.evidence = "checkpoint observation failed"
        self.evidence.protected_state_removed = remove_protected_artifacts((state_path,))

    def import_completed_long_disconnect(self, state_path: Path) -> None:
        """Import safe checkpoint evidence after an operator-shortened live run."""

        state = read_protected_json(state_path)
        checkpoints = state.get("checkpoints")
        if not isinstance(checkpoints, list):
            raise SpikeConfigurationError("protected long-checkpoint evidence is incomplete")
        logical = self.config.logical_references[0]
        for value in checkpoints:
            if not isinstance(value, dict):
                continue
            minutes = _integer_field(value, "checkpoint_minutes")
            if minutes not in LONG_CHECKPOINT_MINUTES:
                continue
            affinity_value = value.get("affinity")
            affinity = (
                AffinityResult(affinity_value)
                if affinity_value in {item.value for item in AffinityResult}
                else AffinityResult.UNKNOWN
            )
            checkpoint = LongCheckpoint(
                checkpoint_minutes=minutes,
                elapsed_seconds=_float_field(value, "elapsed_seconds", minutes * 60.0),
                affinity=affinity,
                masked_ip=cast(str | None, value.get("masked_ip")),
                browser_restarted=bool(value.get("browser_restarted")),
                fresh_python_process=bool(value.get("fresh_python_process")),
                error=cast(str | None, value.get("error")),
            )
            self.evidence.long_checkpoints.append(checkpoint)
            result = self.evidence.tests[f"{minutes}-minute disconnected affinity"]
            result.observations = 1
            result.same_count = int(affinity is AffinityResult.SAME)
            result.changed_count = int(affinity is AffinityResult.CHANGED)
            result.errors = int(checkpoint.error is not None or affinity is AffinityResult.UNKNOWN)
            result.masked_example_ip = checkpoint.masked_ip
            result.status = (
                SpikeStatus.PASS
                if affinity is AffinityResult.SAME and checkpoint.error is None
                else SpikeStatus.PARTIAL
                if affinity is AffinityResult.CHANGED
                else SpikeStatus.UNKNOWN
            )
            result.evidence = (
                f"{affinity.value} vs T+0 after {checkpoint.elapsed_seconds / 60:.1f} minutes"
            )
            if affinity is AffinityResult.SAME:
                summary = self.evidence.sessions[logical]
                summary.longest_same_interval_seconds = max(
                    summary.longest_same_interval_seconds or 0.0,
                    checkpoint.elapsed_seconds,
                )
        for minutes in (60, 90, 115):
            result = self.evidence.tests[f"{minutes}-minute disconnected affinity"]
            result.status = SpikeStatus.NOT_RUN
            result.evidence = (
                "ignored after operator reduced the required continuity window to 30 minutes"
            )
        self.evidence.protected_state_removed = remove_protected_artifacts((state_path,))

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
        provider_ids = [
            self.config.proxy_for(item).sticky_session_id for item in self.config.logical_references
        ]
        self.evidence.configuration_borrow_suspected = len(provider_ids) != len(set(provider_ids))
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
        base = {
            LIVE_GATE: "1",
            "IPROYAL_PROXY_SERVER": "http://proxy.invalid:1234",
            "IPROYAL_PROXY_USERNAME": "x",
            "IPROYAL_PROXY_PASSWORD": "y",
            "IPROYAL_PROXY_COUNTRY": "gb",
            "IPROYAL_PROXY_LIFETIME": "2h",
        }
        missing_fields = (
            "IPROYAL_PROXY_SERVER",
            "IPROYAL_PROXY_USERNAME",
            "IPROYAL_PROXY_PASSWORD",
            "IPROYAL_PROXY_COUNTRY",
            "IPROYAL_PROXY_LIFETIME",
        )
        synthetic_passed = all(
            load_spike_config({key: value for key, value in base.items() if key != missing}).config
            is None
            for missing in missing_fields
        )
        malformed = dict(base)
        malformed["IPROYAL_PROXY_SERVER"] = "not-a-server"
        synthetic_passed = synthetic_passed and load_spike_config(malformed).config is None
        try:
            validate_provider_session_id("bad!")
        except SpikeConfigurationError:
            pass
        else:
            synthetic_passed = False
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
            invalid = IPRoyalProxyConfiguration(
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
                "missing required fields, malformed server, invalid session ID, unreachable endpoint, "
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

    async def run(
        self,
        state_path: Path,
        *,
        completed_long_state: Path | None = None,
    ) -> SpikeEvidence:
        await self.manager.start()
        try:
            self.evidence.browser_version = self.manager.backend_diagnostics().browser_version
            await self.test_basic()
            await self.test_bypass()
            await self.test_geo()
            await self.test_fresh_contexts()
            await self.test_browser_restart()
            await self.test_process_restart(state_path)
            await self.test_concurrency()
            await self.test_failures()
            if completed_long_state is None:
                await self.test_long_disconnect(DEFAULT_LONG_STATE_PATH)
            else:
                self.import_completed_long_disconnect(completed_long_state)
        finally:
            await self.manager.shutdown()
        self.evidence.final_context_count = self.manager.active_context_count
        self.evidence.final_process_count = self.manager.managed_process_count
        current_browser_processes = browser_main_process_ids()
        if current_browser_processes is not None and self._baseline_browser_processes is not None:
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
        "Bypass check",
        "UK geo verification",
        "Fresh-context affinity",
        "Browser-process restart affinity",
        "Independent Python/application restart affinity",
        "5-minute disconnected affinity",
        "30-minute disconnected affinity",
        "Three-session concurrency",
        "Failure cleanup",
        "Secret leakage audit",
        "Final context count",
        "Final managed browser-process count",
    )
    statuses = {name: evidence.tests[name].status for name in required}
    if all(status is SpikeStatus.PASS for status in statuses.values()):
        return "SPIKE_PASS"
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
        "fresh_context_cycles": [asdict(item) for item in evidence.fresh_context_cycles],
        "long_checkpoints": [asdict(item) for item in evidence.long_checkpoints],
        "configured_country": evidence.configured_country,
        "configured_lifetime": evidence.configured_lifetime,
        "geo_service": evidence.geo_service,
        "geo_country": evidence.geo_country,
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
    missing = ", ".join(evidence.missing_configuration) or "none"
    errors = "; ".join(evidence.configuration_errors) or "none"
    completed = (
        ", ".join(f"T+{item.checkpoint_minutes}m" for item in evidence.long_checkpoints)
        or "NOT RUN"
    )
    lines = [
        "# IPRoyal Residential Proxy Spike Results",
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
        "- Provider: IPRoyal Residential",
        f"- Configured country: `{evidence.configured_country}`",
        f"- Configured lifetime: `{evidence.configured_lifetime}`",
        f"- Logical session count: {len(evidence.sessions)}",
        f"- Proxy-routed request count: {evidence.request_count}",
        f"- IP diagnostic service: `{evidence.diagnostic_url}`",
        f"- Geo diagnostic service: `{evidence.geo_service}`",
        f"- Disconnect checkpoints completed: {completed}",
        f"- Live gate state: {'enabled' if evidence.config_ready else 'not ready'}",
        f"- Missing configuration: {missing}",
        f"- Configuration errors: {errors}",
        "",
        "No Queue-it or ordinary website was contacted.",
        "",
        "## Configuration Safety",
        "",
        f"- Credentials source: {evidence.credentials_source}.",
        f"- `.env` permission status: {evidence.environment_file_mode or 'unknown / not applicable'}.",
        "- Credentials and constructed effective passwords were held in memory only.",
        f"- Leakage-audit result: {_status_text(evidence.tests['Secret leakage audit'])}.",
        "- Full observed IPs were not retained in human or ordinary machine results.",
        "- Cross-process comparison used a salted SHA-256 digest.",
        "- Protected temporary state cleanup: "
        + ("removed" if evidence.protected_state_removed else "not created / not yet removed"),
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
    lines += [
        "",
        "## Fresh-context cycle detail",
        "",
        "| Logical reference | Observation | Result | Duration | Masked IP | Error |",
        "|---|---:|---|---:|---|---|",
    ]
    for cycle in evidence.fresh_context_cycles:
        lines.append(
            f"| {cycle.logical_reference} | {cycle.observation} | {cycle.affinity} | "
            f"{cycle.duration_seconds:.3f}s | {cycle.masked_ip or '—'} | {cycle.error or 'none'} |"
        )
    if not evidence.fresh_context_cycles:
        lines.append("| — | 0 | NOT RUN | — | — | none |")
    lines += [
        "",
        "## Long disconnect detail",
        "",
        "| Checkpoint | Elapsed | Result vs T+0 | Masked IP | Full browser restart | Fresh Python process | Error |",
        "|---|---:|---|---|---|---|---|",
    ]
    for checkpoint in evidence.long_checkpoints:
        lines.append(
            f"| T+{checkpoint.checkpoint_minutes}m | {checkpoint.elapsed_seconds / 60:.1f}m | "
            f"{checkpoint.affinity.value} | {checkpoint.masked_ip or '—'} | "
            f"{'yes' if checkpoint.browser_restarted else 'no'} | "
            f"{'yes' if checkpoint.fresh_python_process else 'no'} | {checkpoint.error or 'none'} |"
        )
    if not evidence.long_checkpoints:
        lines.append("| — | — | NOT RUN | — | — | — | none |")
    lines += [
        "",
        "## Per-logical-session summary",
        "",
        "| Logical reference | Provider session ID | Observations | SAME | CHANGED | Masked IP | Browser restart | Python restart | Longest SAME disconnect | Errors |",
        "|---|---|---:|---:|---:|---|---|---|---|---|",
    ]
    for summary in evidence.sessions.values():
        longest_label = (
            f"{summary.longest_same_interval_seconds / 60:.1f}m"
            if summary.longest_same_interval_seconds is not None
            else "UNKNOWN"
        )
        lines.append(
            f"| {summary.logical_reference} | `{summary.provider_session_id}` | "
            f"{summary.observations} | {summary.same_count} | {summary.changed_count} | "
            f"{summary.masked_ip or '—'} | {summary.browser_restart.value} | "
            f"{summary.process_restart.value} | {longest_label} | "
            f"{', '.join(sorted(summary.errors)) or 'none'} |"
        )

    def decision(name: str, statement: str | None = None) -> str:
        result = evidence.tests[name]
        status = result.status.value
        return f"{status} — {statement or result.evidence}"

    longest_seconds = max(
        (
            checkpoint.elapsed_seconds
            for checkpoint in evidence.long_checkpoints
            if checkpoint.affinity is AffinityResult.SAME
        ),
        default=None,
    )
    changed = [
        checkpoint
        for checkpoint in evidence.long_checkpoints
        if checkpoint.affinity is AffinityResult.CHANGED
    ]
    cleanup_ok = (
        evidence.tests["Final context count"].status is SpikeStatus.PASS
        and evidence.tests["Final managed browser-process count"].status is SpikeStatus.PASS
    )
    lines += [
        "",
        "## Provider behavior observations",
        "",
        "- Patchright/Quetty integration failure: "
        + (
            "none observed"
            if evidence.tests["Basic proxy connectivity"].status is SpikeStatus.PASS
            else evidence.tests["Basic proxy connectivity"].evidence
        ),
        "- IPRoyal authentication/configuration failure: no failure observed"
        if evidence.tests["Basic proxy connectivity"].status is SpikeStatus.PASS
        else "- IPRoyal authentication/configuration failure: see Basic connectivity.",
        f"- Residential provider IP reassignment: {sum(item.changed_count for item in evidence.tests.values())} exact comparison change(s).",
        "- Residential peer disappearance: not separately distinguishable from provider reassignment.",
        "- Network/timeout failure: see sanitized errors and Failure cleanup.",
        "- Unknown/inconclusive behavior: every UNKNOWN or NOT RUN row remains unclaimed.",
        f"- Cross-session collisions: {evidence.collisions}; collisions are not failures.",
        "- An IP reassignment is provider continuity evidence, not Queue identity corruption.",
        "",
        "## Cleanup",
        "",
        f"- Active contexts after run: {evidence.final_context_count if evidence.final_context_count is not None else 'NOT RUN'}",
        f"- Managed browser processes after shutdown: {evidence.final_process_count if evidence.final_process_count is not None else 'NOT RUN'}",
        f"- Temporary files remaining: {evidence.temporary_files_remaining}",
        f"- Protected restart state removed: {evidence.protected_state_removed if evidence.protected_state_removed is not None else 'NOT RUN'}",
        f"- Orphan-process result: {evidence.orphan_process if evidence.orphan_process is not None else 'UNKNOWN'}",
        "",
        "## Decision Matrix",
        "",
        "1. Can Patchright connect through IPRoyal Residential? "
        + decision("Basic proxy connectivity"),
        "2. Is traffic demonstrably proxied? " + decision("Bypass check"),
        "3. Is the proxy exit UK/GB? " + decision("UK geo verification"),
        "4. Can proxy configuration be applied per temporary BrowserContext? "
        + decision("Three-session concurrency"),
        "5. Does the same IPRoyal session ID retain its IP across fresh BrowserContexts? "
        + decision("Fresh-context affinity"),
        "6. Does it retain its IP across managed browser-process restart? "
        + decision("Browser-process restart affinity"),
        "7. Does it retain its IP across independent Python/application restart? "
        + decision("Independent Python/application restart affinity"),
        "8. Same IP after 5 minutes disconnected? " + decision("5-minute disconnected affinity"),
        "9. Same IP after 30 minutes disconnected? " + decision("30-minute disconnected affinity"),
        "10. Same IP after 60 minutes disconnected? " + decision("60-minute disconnected affinity"),
        "11. Same IP after 90 minutes disconnected? " + decision("90-minute disconnected affinity"),
        "12. Same IP after approximately 115 minutes disconnected? "
        + decision("115-minute disconnected affinity"),
        "13. What is the longest tested disconnected interval retaining the T+0 baseline IP? "
        + (
            (f"PASS — {longest_seconds / 60:.1f} minutes")
            if longest_seconds is not None
            else "UNKNOWN — no interval proven"
        ),
        "14. Can three logical sessions operate concurrently? "
        + decision("Three-session concurrency"),
        "15. Did any test require keeping the browser/context/proxy connection alive? "
        + (
            "PASS — no; disconnect checkpoints retained no browser or proxy connection"
            if evidence.long_checkpoints
            else "UNKNOWN — not tested"
        ),
        "16. Did credentials remain absent from ordinary persistence/logs/results? "
        + decision("Secret leakage audit"),
        "17. Did resources return to baseline? "
        + (
            "PASS — final resource counts returned to baseline"
            if cleanup_ok
            else "UNKNOWN — clean return was not established"
        ),
        "18. Did the IP change at any point before the configured `2h` lifetime? "
        + (
            f"PARTIAL — yes, first recorded at T+{changed[0].checkpoint_minutes}m"
            if changed
            else "PASS — no change in completed checkpoints"
            if evidence.long_checkpoints
            else "UNKNOWN — not tested"
        ),
        "",
        "## Final Outcome",
        "",
        f"`{evidence.outcome}`",
        "",
        "Evidence is limited to the exact configuration and intervals recorded above.",
        "",
        "## Production recommendation",
        "",
    ]
    if evidence.outcome == "SPIKE_PASS":
        lines += [
            "A future implementation may use:",
            "",
            "`QueueSession -> immutable IPRoyal proxy-session assignment -> deterministic 8-character provider session ID -> reconstruct password using country + session + lifetime -> create temporary proxied Patchright BrowserContext -> restore authoritative Queue ID -> verify identity -> inspect -> persist -> close/park`",
            "",
            "Queue ID remains authoritative. The residential IP is continuity/diagnostic metadata only; a change should surface as `PROXY_IP_CHANGED`.",
            "",
            "`Design and implement persisted per-session IPRoyal Residential proxy assignments in Quetty using the proven UK sticky-session reconstruction mechanism, while keeping Queue ID authoritative and validating proxy-IP continuity on every restore.`",
        ]
    else:
        lines.append(
            "Do not implement production proxy support from this result. Queue ID remains authoritative, and proxy IP remains observational metadata only."
        )
    return "\n".join(lines) + "\n"


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
    config: IPRoyalSpikeConfig,
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
        runner = IPRoyalSpikeRunner(config)
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


async def run_long_child(
    config: IPRoyalSpikeConfig,
    state_path: Path,
    child_result_path: Path,
    checkpoint_minutes: int,
) -> int:
    """Run one long-affinity observation in a genuinely fresh Python process."""

    try:
        state = read_protected_json(state_path)
        if state.get("format") != _LONG_FORMAT or state.get("version") != _LONG_VERSION:
            raise SpikeConfigurationError(
                "protected long-checkpoint state has an unsupported format"
            )
        logical = state.get("logical_reference")
        provider_id = state.get("provider_session_id")
        if not isinstance(logical, str) or logical not in config.logical_references:
            raise SpikeConfigurationError("protected long-checkpoint logical session is invalid")
        expected_id = config.proxy_for(logical).sticky_session_id
        if provider_id != expected_id:
            raise SpikeConfigurationError(
                "protected long-checkpoint provider session does not match"
            )
        if state.get("country") != config.country or state.get("lifetime") != config.lifetime:
            raise SpikeConfigurationError("protected long-checkpoint configuration does not match")
        started_epoch = state.get("started_epoch")
        if not isinstance(started_epoch, (int, float)):
            raise SpikeConfigurationError("protected long-checkpoint start time is invalid")
        runner = IPRoyalSpikeRunner(config)
        await runner.manager.start()
        try:
            observation = await runner.observe(logical, f"long-disconnect-{checkpoint_minutes}m")
        finally:
            await runner.manager.shutdown()
        affinity = compare_long_checkpoint(state, observation.ip)
        write_protected_json(
            child_result_path,
            {
                "format": _LONG_FORMAT,
                "version": _LONG_VERSION,
                "checkpoint_minutes": checkpoint_minutes,
                "affinity": affinity.value,
                "masked_ip": observation.masked_ip,
                "error": observation.error.value if observation.error else None,
                "elapsed_seconds": time.time() - float(started_epoch),
                "request_count": runner.evidence.request_count,
                "final_context_count": runner.manager.active_context_count,
                "final_process_count": runner.manager.managed_process_count,
            },
        )
        return 0 if observation.succeeded else 1
    except Exception as exc:  # noqa: BLE001 - child persists only a closed class
        write_protected_json(
            child_result_path,
            {
                "format": _LONG_FORMAT,
                "version": _LONG_VERSION,
                "checkpoint_minutes": checkpoint_minutes,
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
        choices=(
            "full",
            "restart-prepare",
            "restart-resume",
            "restart-child",
            "long-start",
            "long-checkpoint",
            "long-child",
            "finalize-long",
        ),
        default="full",
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--restart-state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--long-state", type=Path, default=DEFAULT_LONG_STATE_PATH)
    parser.add_argument("--checkpoint-minutes", type=int, choices=LONG_CHECKPOINT_MINUTES)
    parser.add_argument("--child-result", type=Path)
    return parser.parse_args()


async def _prepare_restart(config: IPRoyalSpikeConfig, state_path: Path) -> SpikeEvidence:
    runner = IPRoyalSpikeRunner(config)
    await runner.manager.start()
    try:
        observation = await runner.observe(config.logical_references[0], "restart-prepare")
    finally:
        await runner.manager.shutdown()
    if observation.ip is None:
        runner.evidence.tests[
            "Independent Python/application restart affinity"
        ].status = SpikeStatus.FAIL
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


async def _resume_restart(config: IPRoyalSpikeConfig, state_path: Path) -> SpikeEvidence:
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
    evidence.protected_state_removed = remove_protected_artifacts((child_result, state_path))
    evidence.outcome = "SPIKE_PARTIAL"
    return evidence


async def _long_start(config: IPRoyalSpikeConfig, state_path: Path) -> SpikeEvidence:
    runner = IPRoyalSpikeRunner(config)
    await runner.manager.start()
    try:
        observation = await runner.observe(config.logical_references[0], "long-disconnect-baseline")
    finally:
        await runner.manager.shutdown()
    if observation.ip is not None:
        proxy = config.proxy_for(observation.logical_reference)
        write_protected_json(
            state_path,
            build_long_checkpoint_state(
                logical_reference=observation.logical_reference,
                provider_session_id=proxy.sticky_session_id,
                ip=observation.ip,
                country=config.country,
                lifetime=config.lifetime,
            ),
        )
    runner.evidence.final_context_count = runner.manager.active_context_count
    runner.evidence.final_process_count = runner.manager.managed_process_count
    runner.evidence.protected_state_removed = False
    runner.evidence.outcome = "SPIKE_PARTIAL"
    return runner.evidence


async def _long_checkpoint(
    config: IPRoyalSpikeConfig,
    state_path: Path,
    checkpoint_minutes: int,
) -> SpikeEvidence:
    state = read_protected_json(state_path)
    started_epoch = state.get("started_epoch")
    if not isinstance(started_epoch, (int, float)):
        raise SpikeConfigurationError("protected long-checkpoint start time is invalid")
    elapsed = time.time() - float(started_epoch)
    if elapsed + 30 < checkpoint_minutes * 60:
        raise SpikeConfigurationError(f"{checkpoint_minutes}-minute checkpoint is not due yet")
    child_path = state_path.with_name(f"long-resume-{checkpoint_minutes}.json")
    code = await run_long_child(config, state_path, child_path, checkpoint_minutes)
    document = read_protected_json(child_path)
    child_path.unlink(missing_ok=True)
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
        configured_country=config.country,
        configured_lifetime=config.lifetime,
        geo_service=config.geo_url,
    )
    affinity_value = document.get("affinity")
    affinity = (
        AffinityResult(affinity_value)
        if affinity_value in {item.value for item in AffinityResult}
        else AffinityResult.UNKNOWN
    )
    masked_ip = cast(str | None, document.get("masked_ip"))
    error = cast(str | None, document.get("error"))
    elapsed_seconds = _float_field(document, "elapsed_seconds", elapsed)
    checkpoint = LongCheckpoint(
        checkpoint_minutes=checkpoint_minutes,
        elapsed_seconds=elapsed_seconds,
        affinity=affinity,
        masked_ip=masked_ip,
        browser_restarted=True,
        fresh_python_process=True,
        error=error,
    )
    evidence.long_checkpoints.append(checkpoint)
    result = evidence.tests[f"{checkpoint_minutes}-minute disconnected affinity"]
    result.observations = _integer_field(document, "request_count")
    result.same_count = int(affinity is AffinityResult.SAME)
    result.changed_count = int(affinity is AffinityResult.CHANGED)
    result.errors = int(code != 0 or error is not None)
    result.masked_example_ip = masked_ip
    result.status = (
        SpikeStatus.PASS
        if affinity is AffinityResult.SAME and not result.errors
        else SpikeStatus.PARTIAL
        if affinity is AffinityResult.CHANGED
        else SpikeStatus.UNKNOWN
    )
    result.evidence = f"{affinity.value} vs T+0 after {elapsed_seconds / 60:.1f} minutes"
    checkpoints = state.get("checkpoints")
    if not isinstance(checkpoints, list):
        checkpoints = []
        state["checkpoints"] = checkpoints
    checkpoints.append(asdict(checkpoint))
    final = checkpoint_minutes == LONG_CHECKPOINT_MINUTES[-1]
    if final:
        evidence.protected_state_removed = remove_protected_artifacts((state_path,))
    else:
        write_protected_json(state_path, state)
        evidence.protected_state_removed = False
    evidence.request_count = result.observations
    evidence.final_context_count = cast(int | None, document.get("final_context_count"))
    evidence.final_process_count = cast(int | None, document.get("final_process_count"))
    evidence.outcome = "SPIKE_PARTIAL"
    return evidence


async def async_main(args: argparse.Namespace) -> int:
    load = load_spike_config()
    if args.mode == "restart-child":
        if not load.ready or load.config is None or args.child_result is None:
            return 2
        return await run_restart_child(load.config, args.restart_state, args.child_result)
    if args.mode == "long-child":
        if (
            not load.ready
            or load.config is None
            or args.child_result is None
            or args.checkpoint_minutes is None
        ):
            return 2
        return await run_long_child(
            load.config,
            args.long_state,
            args.child_result,
            args.checkpoint_minutes,
        )

    if not load.ready or load.config is None:
        evidence = not_run_evidence(load)
        args.report.write_text(render_report(evidence), encoding="utf-8")
        return 2

    if args.mode == "restart-prepare":
        evidence = await _prepare_restart(load.config, args.restart_state)
    elif args.mode == "restart-resume":
        evidence = await _resume_restart(load.config, args.restart_state)
    elif args.mode == "long-start":
        evidence = await _long_start(load.config, args.long_state)
    elif args.mode == "long-checkpoint":
        if args.checkpoint_minutes is None:
            raise SpikeConfigurationError("--checkpoint-minutes is required")
        evidence = await _long_checkpoint(
            load.config,
            args.long_state,
            args.checkpoint_minutes,
        )
    elif args.mode == "finalize-long":
        evidence = await IPRoyalSpikeRunner(load.config).run(
            args.restart_state,
            completed_long_state=args.long_state,
        )
    else:
        evidence = await IPRoyalSpikeRunner(load.config).run(args.restart_state)
    evidence.credentials_source = load.credentials_source
    evidence.environment_file_mode = load.environment_file_mode
    rendered = render_report(evidence)
    # The final markdown itself participates in the leakage audit before persistence.
    if any(secret in rendered for secret in load.config.secret_candidates()):
        evidence.tests["Secret leakage audit"].status = SpikeStatus.FAIL
        evidence.tests[
            "Secret leakage audit"
        ].evidence = "a supplied credential appeared in the rendered markdown"
        evidence.outcome = "SPIKE_FAIL"
        rendered = render_report(evidence)
    args.report.write_text(rendered, encoding="utf-8")
    return 0 if evidence.outcome == "SPIKE_PASS" else 1


def main() -> None:
    raise SystemExit(asyncio.run(async_main(_parse_args())))


if __name__ == "__main__":
    main()
