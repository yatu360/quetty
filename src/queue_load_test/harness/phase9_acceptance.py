"""Phase 9 acceptance: per-session IPRoyal routing and proxy-IP observation, end to end.

This runs the real operator stack (FastAPI app and lifespan, ``ApplicationRunRuntime``,
bounded creation, scheduler/monitor, operator actions, the headed pool, SQLite) with an
installed Patchright or Chrome browser and the *unmodified* production proxy path.

Two local stand-ins replace the outside world, so this is **not** IPRoyal, ipify, or
Queue-it evidence:

- ``LocalAuthProxy`` plays the IPRoyal gateway: it requires Basic auth, records the
  sticky-session ID of every request, maps each sticky session to one stable synthetic
  exit IP (documentation ranges), and answers for a fake ipify hostname.
- ``LocalQueueSimulator`` plays the target behind a non-loopback alias, so Chromium
  routes it through the proxy exactly like a real target (Chromium never proxies
  loopback).

Real-provider continuity is measured separately by the gated live harness
(``phase9_iproyal_live``). The JSON report contains no IPs, provider session IDs, or
credentials: only counts and PASS/FAIL gates.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sqlite3
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from queue_load_test.config import Settings
from queue_load_test.harness.local_auth_proxy import (
    DEFAULT_IPIFY_HOST,
    IPIFY_HTTP_500,
    IPIFY_INVALID_IP,
    IPIFY_MALFORMED,
    IPIFY_OK,
    IPIFY_TIMEOUT,
    MODE_FORWARD,
    MODE_UPSTREAM_DOWN,
    LocalAuthProxy,
)
from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.harness.phase5_workflow import _browser_main_pids, until
from queue_load_test.models import BrowserBackendName
from queue_load_test.proxy import IPRoyalProxyConfigurationError, is_valid_proxy_session_id
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.utils.instance_lock import InstanceLock
from queue_load_test.web.app import create_app
from queue_load_test.web.service import ApplicationRunRuntime

SIM_HOST = "sim.quetty.test"
FAKE_USERNAME = "FAKEUSER_phase9accept"
FAKE_PASSWORD = "FAKEPASS_phase9accept"
INITIAL_SESSIONS = 3
CHANGED_EXIT_IP = "198.51.100.251"
DEFAULT_RESULT_PATH = Path("docs/results/phase9_iproyal_acceptance_result.json")
HX = {"HX-Request": "true"}


@dataclass(slots=True)
class Check:
    name: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Recorder:
    checks: list[Check] = field(default_factory=list)

    def check(self, name: str, passed: bool, **detail: Any) -> bool:
        self.checks.append(Check(name, bool(passed), detail))
        return bool(passed)


class _Capture(logging.Handler):
    """Collect every log record (message and structured context) for the secret audit."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        context = getattr(record, "observability_context", None)
        self.lines.append(f"{record.getMessage()} {context!r} {record.exc_text or ''}")


class Database:
    """Read-only evidence queries through an independent SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def rows(self, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self.path) as connection:
            return list(connection.execute(sql, parameters))

    def value(self, sql: str, parameters: tuple[object, ...] = ()) -> Any:
        rows = self.rows(sql, parameters)
        return rows[0][0] if rows else None

    def sessions(self) -> dict[str, dict[str, Any]]:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM queue_sessions").fetchall()
        return {str(row["session_id"]): dict(row) for row in rows}

    def ready(self) -> dict[str, dict[str, Any]]:
        """Sessions with a Queue ID and a baseline proxy IP."""

        return {
            key: row
            for key, row in self.sessions().items()
            if row["queue_id"] is not None
            and row["status"] != "FAILED"
            and row["proxy_ip"] is not None
        }

    def field(self, session_id: str, name: str) -> Any:
        row = self.sessions().get(session_id)
        return row[name] if row is not None else None


def acceptance_settings(
    directory: Path,
    *,
    database: Path,
    backend: BrowserBackendName,
    proxy: LocalAuthProxy,
    credentials: bool = True,
) -> Settings:
    values: dict[str, object] = {
        "DATABASE_URL": f"sqlite:///{database}",
        "STATE_DIRECTORY": directory / "state",
        "DIRECT_MONITOR_DIRECTORY": directory / "direct-monitor",
        "SESSION_MODE": "HYBRID",
        "BROWSER_BACKEND": backend.value,
        "MONITORING_STRATEGY": "headed_window",
        "CHROME_PROCESS_COUNT": 1,
        "MAX_CONTEXTS_PER_BROWSER": 5,
        "MAX_ACTIVE_CONTEXTS": 5,
        "MAX_MANUAL_OPEN_SESSIONS": 2,
        "MANUAL_OPEN_LEASE_SECONDS": 6,
        "OPERATOR_WORKERS": 2,
        "CREATION_WORKERS": 2,
        "MONITOR_WORKERS": 2,
        "MONITOR_SCHEDULER_TICK_SECONDS": 0.2,
        "MONITOR_RETRY_MAX_ATTEMPTS": 1,
        "ADMISSION_WAIT_SECONDS": 0,
        "SHUTDOWN_TIMEOUT_SECONDS": 10,
        "QUEUE_POLL_SECONDS": 2,
        "POLL_JITTER_SECONDS": 0,
        "PRE_QUEUE_POLL_MIN_SECONDS": 1.5,
        "PRE_QUEUE_POLL_MAX_SECONDS": 2,
        "ACTIVE_EARLY_POLL_MIN_SECONDS": 1.5,
        "ACTIVE_EARLY_POLL_MAX_SECONDS": 2,
        "ACTIVE_MID_POLL_MIN_SECONDS": 1.5,
        "ACTIVE_MID_POLL_MAX_SECONDS": 2,
        "SERVICED_SOON_POLL_MIN_SECONDS": 1.5,
        "SERVICED_SOON_POLL_MAX_SECONDS": 2,
        "MAX_MANUAL_REQUESTED_SESSIONS": 50,
        "CREATION_HEADLESS": True,
        "PROXY_IP_ENDPOINT": f"http://{DEFAULT_IPIFY_HOST}/?format=json",
        "PROXY_IP_TIMEOUT_SECONDS": 2,
        "PROXY_IP_CONNECT_TIMEOUT_SECONDS": 2,
        "IPROYAL_PROXY_COUNTRY": "gb",
        "IPROYAL_PROXY_LIFETIME": "2h",
        "IPROYAL_PROXY_SERVER": proxy.url,
    }
    if credentials:
        values.update(
            {
                "IPROYAL_PROXY_ENABLED": True,
                "IPROYAL_PROXY_USERNAME": FAKE_USERNAME,
                "IPROYAL_PROXY_PASSWORD": FAKE_PASSWORD,
            }
        )
    return Settings(_env_file=None, **values)  # type: ignore[arg-type, call-arg]


@dataclass
class World:
    backend: BrowserBackendName
    directory: Path
    database: Path
    db: Database
    simulator: LocalQueueSimulator
    proxy: LocalAuthProxy
    record: Recorder
    capture: _Capture
    evidence: dict[str, Any]
    every_provider_id: set[str] = field(default_factory=set)

    @property
    def target(self) -> str:
        return f"http://{SIM_HOST}:{self.simulator.port}/entry"

    def build(self, settings: Settings) -> Any:
        repository = SQLiteSessionRepository(self.database)
        runtime = ApplicationRunRuntime(
            settings=settings, repository=repository, manual_headless=True
        )
        return create_app(
            settings=settings,
            repository=repository,
            runtime=runtime,
            instance_lock=InstanceLock.for_database(self.database),
        )

    def settings(self, *, credentials: bool = True) -> Settings:
        return acceptance_settings(
            self.directory,
            database=self.database,
            backend=self.backend,
            proxy=self.proxy,
            credentials=credentials,
        )

    def lookups(self, provider_session_id: str) -> int:
        return self.proxy.ip_lookups.count(provider_session_id)

    def all_traffic_proxied(self) -> bool:
        return self.simulator.requests == len(self.proxy.requests)


def _changed(db: Database, session_id: str, **previous: Any) -> Callable[[], bool]:
    """A predicate true once every named column differs from its previous value."""

    def predicate() -> bool:
        return all(db.field(session_id, name) != value for name, value in previous.items())

    return predicate


async def _wait(predicate: Callable[[], Awaitable[bool] | bool], timeout: float = 30) -> bool:
    return await until(predicate, timeout=timeout, interval=0.2) is not None


async def _scenario_a_three_sessions(world: World, ui: httpx.AsyncClient) -> list[str]:
    response = await ui.post(
        "/setup",
        data={"target_url": world.target, "requested_sessions": str(INITIAL_SESSIONS)},
    )
    ready = await _wait(lambda: len(world.db.ready()) == INITIAL_SESSIONS, timeout=60)
    rows = world.db.ready()
    session_ids = sorted(rows)
    provider_ids = [rows[key]["proxy_session_id"] for key in session_ids]
    world.every_provider_id.update(provider_ids)
    own_ip = all(
        rows[key]["proxy_ip"] == world.proxy.exit_ip(rows[key]["proxy_session_id"])
        for key in session_ids
    )
    dashboard = (await ui.get("/partials/sessions")).text
    run = world.db.rows(
        "SELECT proxy_provider, proxy_country, proxy_lifetime, browser_backend FROM run_config"
    )
    world.evidence["configuration"] = {
        "provider": run[0][0] if run else None,
        "country": run[0][1] if run else None,
        "lifetime": run[0][2] if run else None,
        "browser_backend": run[0][3] if run else None,
        "monitoring_strategy": "headed_window",
        "session_count": INITIAL_SESSIONS,
    }
    world.record.check(
        "A_three_sessions_three_distinct_provider_ids",
        response.status_code in {200, 303}
        and ready
        and len(set(session_ids)) == INITIAL_SESSIONS
        and len(set(provider_ids)) == INITIAL_SESSIONS
        and all(is_valid_proxy_session_id(item) for item in provider_ids),
        sessions=len(session_ids),
        distinct_provider_ids=len(set(provider_ids)),
    )
    world.record.check(
        "A_initial_acquisition_proxied_with_each_sessions_own_id",
        set(world.proxy.session_ids()) == set(provider_ids) and world.all_traffic_proxied(),
        proxied_requests=len(world.proxy.requests),
        simulator_requests=world.simulator.requests,
    )
    world.record.check(
        "A_initial_ip_observation_through_each_sessions_own_id",
        own_ip
        and all(world.lookups(pid) >= 1 for pid in provider_ids)
        and all(rows[key]["proxy_ip_checked_at"] is not None for key in session_ids),
        lookups=len(world.proxy.ip_lookups),
    )
    world.record.check(
        "A_dashboard_shows_all_three_with_proxy_ip_columns",
        all(rows[key]["proxy_ip"] in dashboard for key in session_ids)
        and all(f"<th>{header}</th>" in dashboard for header in
                ("Proxy IP", "Queue Checked", "IP Checked")),
    )
    return session_ids


async def _scenario_b_repeated_checks(world: World, session_id: str) -> None:
    db = world.db
    provider_id = db.field(session_id, "proxy_session_id")
    queue_id = db.field(session_id, "queue_id")
    for cycle in range(2):
        queue_checked = db.field(session_id, "last_checked_at")
        ip_checked = db.field(session_id, "proxy_ip_checked_at")
        lookups = world.lookups(provider_id)
        advanced = await _wait(_changed(db, session_id, last_checked_at=queue_checked,
                                        proxy_ip_checked_at=ip_checked))
        row = db.sessions()[session_id]
        conditions = {
            "advanced": advanced,
            "same_provider_id": row["proxy_session_id"] == provider_id,
            "same_queue_id": row["queue_id"] == queue_id,
            "lookup_through_same_id": world.lookups(provider_id) > lookups,
            # ISO-8601 UTC strings order chronologically: IP observed after the check.
            "ip_checked_not_before_queue_checked": str(row["proxy_ip_checked_at"])
            >= str(row["last_checked_at"]),
        }
        world.record.check(
            f"B_automatic_check_{cycle + 1}_reuses_provider_id_and_observes_ip",
            all(conditions.values()),
            **conditions,
        )


async def _scenario_c_and_m_ip_failure_independence(world: World, session_id: str) -> None:
    db = world.db
    for mode in (IPIFY_HTTP_500, IPIFY_TIMEOUT, IPIFY_MALFORMED, IPIFY_INVALID_IP):
        ip = db.field(session_id, "proxy_ip")
        ip_checked = db.field(session_id, "proxy_ip_checked_at")
        queue_checked = db.field(session_id, "last_checked_at")
        world.proxy.ipify_mode = mode
        advanced = await _wait(_changed(db, session_id, last_checked_at=queue_checked))
        # Let the in-flight lookup for that check finish (bounded by its deadline).
        await asyncio.sleep(2.5)
        row = db.sessions()[session_id]
        world.record.check(
            f"C_queue_check_survives_ipify_{mode}",
            advanced
            and row["status"] not in {"FAILED", "CONNECTION_LOST"}
            and row["last_error"] is None
            and row["proxy_ip"] == ip
            and row["proxy_ip_checked_at"] == ip_checked,
            status=row["status"],
        )
        world.proxy.ipify_mode = IPIFY_OK
    recovered = await _wait(
        lambda: db.field(session_id, "proxy_ip_checked_at") != ip_checked
    )
    world.record.check("C_ip_checked_advances_again_after_ipify_recovers", recovered)


async def _scenario_l_ip_change(world: World, ui: httpx.AsyncClient, session_id: str) -> None:
    db = world.db
    provider_id = db.field(session_id, "proxy_session_id")
    queue_id = db.field(session_id, "queue_id")
    rows_before = set(db.sessions())
    world.proxy.exit_ips[provider_id] = CHANGED_EXIT_IP
    changed = await _wait(lambda: db.field(session_id, "proxy_ip") == CHANGED_EXIT_IP)
    dashboard = (await ui.get("/partials/sessions")).text
    world.record.check(
        "L_ip_change_recorded_without_rotation_or_replace",
        changed
        and db.field(session_id, "proxy_session_id") == provider_id
        and db.field(session_id, "queue_id") == queue_id
        and int(db.field(session_id, "proxy_ip_changed_count")) >= 1
        and db.field(session_id, "proxy_ip_changed_at") is not None
        and set(db.sessions()) == rows_before
        and "changed ×" in dashboard
        and CHANGED_EXIT_IP in dashboard,
    )


async def _settle(world: World) -> None:
    await _wait(
        lambda: int(world.db.value(
            "SELECT COUNT(*) FROM queue_sessions WHERE worker_id IS NOT NULL"
        )) == 0,
        timeout=20,
    )
    # One more beat so an in-flight post-check lookup (bounded) completes.
    await asyncio.sleep(2.5)


async def _scenario_d_refresh(world: World, ui: httpx.AsyncClient, session_id: str) -> None:
    db = world.db
    provider_id = db.field(session_id, "proxy_session_id")
    await ui.post("/monitoring/pause", headers=HX)
    await _settle(world)
    for label in ("paused", "active"):
        if label == "active":
            await ui.post("/monitoring/resume", headers=HX)
        lookups = world.lookups(provider_id)
        ip_checked = db.field(session_id, "proxy_ip_checked_at")
        queue_checked = db.field(session_id, "last_checked_at")
        mark = len(world.proxy.requests)
        await ui.post(f"/sessions/{session_id}/refresh?token=p9-refresh-{label}", headers=HX)
        done = await _wait(_changed(db, session_id, last_checked_at=queue_checked,
                                    proxy_ip_checked_at=ip_checked))
        own = {
            request.provider_session_id
            for request in world.proxy.requests[mark:]
        }
        exact = world.lookups(provider_id) == lookups + 1 if label == "paused" else True
        world.record.check(
            f"D_refresh_while_{label}_reuses_id_and_observes_ip_once",
            done
            and exact
            and db.field(session_id, "proxy_session_id") == provider_id
            and (label == "active" or own == {provider_id}),
            lookups_added=world.lookups(provider_id) - lookups,
        )


async def _scenario_e_manual_open(world: World, ui: httpx.AsyncClient, session_id: str) -> None:
    db = world.db
    provider_id = db.field(session_id, "proxy_session_id")
    queue_id = db.field(session_id, "queue_id")
    await ui.post("/monitoring/pause", headers=HX)
    await _settle(world)
    lookups = world.lookups(provider_id)
    mark = len(world.proxy.requests)
    providers_before = set(db.rows("SELECT proxy_session_id FROM queue_sessions"))
    await ui.post(f"/sessions/{session_id}/open", headers=HX)
    opened = await _wait(lambda: db.field(session_id, "manual_owner_id") is not None)
    await asyncio.sleep(3)  # the open window must not poll ipify
    lookups_while_open = world.lookups(provider_id) - lookups
    await ui.post(f"/sessions/{session_id}/close", headers=HX)
    closed = await _wait(lambda: db.field(session_id, "manual_owner_id") is None)
    observed_at_close = await _wait(lambda: world.lookups(provider_id) == lookups + 1, 10)
    used = {request.provider_session_id for request in world.proxy.requests[mark:]}
    world.record.check(
        "E_manual_open_uses_existing_id_and_observes_ip_once_at_close",
        opened
        and closed
        and lookups_while_open == 0
        and observed_at_close
        and used == {provider_id}
        and db.field(session_id, "queue_id") == queue_id
        and set(db.rows("SELECT proxy_session_id FROM queue_sessions")) == providers_before,
        lookups_while_open=lookups_while_open,
    )
    await ui.post("/monitoring/resume", headers=HX)
    queue_checked = db.field(session_id, "last_checked_at")
    mark = len(world.proxy.requests)
    later = await _wait(lambda: db.field(session_id, "last_checked_at") != queue_checked)
    world.record.check(
        "E_later_automatic_monitoring_keeps_the_same_id",
        later
        and provider_id in {r.provider_session_id for r in world.proxy.requests[mark:]}
        and db.field(session_id, "proxy_session_id") == provider_id,
    )


async def _scenario_f_browser_kill(world: World) -> None:
    db = world.db
    before = {
        key: (row["queue_id"], row["proxy_session_id"], row["last_checked_at"])
        for key, row in db.ready().items()
    }
    lookups = {pid: world.lookups(pid) for _, pid, _ in before.values()}
    victims = _browser_main_pids(world.backend)
    for pid in victims:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    recovered = await _wait(
        lambda: all(
            db.field(key, "last_checked_at") != checked for key, (_, _, checked) in before.items()
        ),
        timeout=45,
    )
    world.record.check(
        "F_browser_kill_restores_with_same_ids_and_observes_ip",
        bool(victims)
        and recovered
        and all(
            db.field(key, "queue_id") == queue_id and db.field(key, "proxy_session_id") == pid
            for key, (queue_id, pid, _) in before.items()
        )
        and all(world.lookups(pid) > count for pid, count in lookups.items())
        and world.all_traffic_proxied(),
        killed_processes=len(victims),
    )


async def _scenario_h_add(world: World, ui: httpx.AsyncClient) -> str | None:
    before = set(world.db.ready())
    await ui.post("/sessions/new?token=p9-add", headers=HX)
    added = await _wait(lambda: len(set(world.db.ready()) - before) == 1, timeout=45)
    new = next(iter(set(world.db.ready()) - before), None)
    row = world.db.sessions().get(new or "", {})
    pid = row.get("proxy_session_id")
    world.record.check(
        "H_add_new_session_new_provider_id_with_baseline_ip",
        added
        and pid is not None
        and pid not in world.every_provider_id
        and row.get("proxy_ip") == world.proxy.exit_ip(pid)
        and row.get("proxy_ip_checked_at") is not None
        and row.get("last_checked_at") is not None,
    )
    if pid:
        world.every_provider_id.add(pid)
    return new


async def _wait_action(ui: httpx.AsyncClient, text: str, timeout: float = 45) -> bool:
    async def seen() -> bool:
        return text in (await ui.get("/partials/sessions")).text

    return await _wait(seen, timeout=timeout)


async def _scenario_i_replace(world: World, ui: httpx.AsyncClient, victim: str, keep: str) -> None:
    db = world.db
    before = set(db.ready())
    await ui.post(f"/sessions/{victim}/replace?token=p9-replace", headers=HX)
    replaced = await _wait(
        lambda: victim not in db.sessions() and len(set(db.ready()) - before) == 1, timeout=45
    )
    new = next(iter(set(db.ready()) - before), None)
    pid = db.field(new, "proxy_session_id") if new else None
    world.record.check(
        "I_replace_creates_new_provider_id_and_new_ip_baseline",
        replaced
        and pid is not None
        and pid not in world.every_provider_id
        and db.field(new, "proxy_ip") == world.proxy.exit_ip(pid),  # type: ignore[arg-type]
    )
    if pid:
        world.every_provider_id.add(pid)

    await ui.post("/monitoring/pause", headers=HX)
    await _settle(world)
    snapshot = db.sessions()[keep]
    rows_before = set(db.sessions())
    world.proxy.mode = MODE_UPSTREAM_DOWN
    await ui.post(f"/sessions/{keep}/replace?token=p9-replace-fail", headers=HX)
    failed = await _wait_action(ui, "Replace: failed")
    world.proxy.mode = MODE_FORWARD
    await asyncio.sleep(1)
    after = db.sessions()[keep]
    fields = ("queue_id", "proxy_session_id", "proxy_ip", "proxy_ip_checked_at")
    world.record.check(
        "I_failed_replace_preserves_original_identity_and_ip_metadata",
        failed
        and all(after[name] == snapshot[name] for name in fields)
        and set(db.sessions()) == rows_before,
    )
    await ui.post("/monitoring/resume", headers=HX)


async def _scenario_j_delete(world: World, ui: httpx.AsyncClient, victim: str) -> None:
    db = world.db
    others = {
        key: row["proxy_session_id"] for key, row in db.sessions().items() if key != victim
    }
    # Delete is refused while automatic monitoring holds the lease (by design), so
    # pause and let in-flight checks finish first, as an operator would.
    await ui.post("/monitoring/pause", headers=HX)
    await _settle(world)
    state_file = world.directory / "state" / f"{victim}.json"
    existed = state_file.exists()
    deleted_at = time.time()
    await ui.post(f"/sessions/{victim}/delete?token=p9-delete", headers=HX)
    gone = await _wait(lambda: victim not in db.sessions())
    # The row goes first; local state cleanup follows asynchronously.
    removed = await _wait(lambda: not state_file.exists(), timeout=10)
    rewritten = state_file.exists() and state_file.stat().st_mtime > deleted_at
    unchanged = {key: db.field(key, "proxy_session_id") for key in others} == others
    world.record.check(
        "J_delete_removes_row_assignment_and_ip_metadata_only",
        existed and gone and removed and not rewritten and unchanged,
        gone=gone,
        state_file_removed=removed,
        others_unchanged=unchanged,
    )
    await ui.post("/monitoring/resume", headers=HX)


async def _security_audit(world: World, ui: httpx.AsyncClient, label: str) -> None:
    pages = [
        (await ui.get(path)).text
        for path in ("/dashboard", "/partials/sessions", "/partials/summary")
    ]
    metrics = (await ui.get("/metrics")).text
    secrets = (FAKE_USERNAME, FAKE_PASSWORD)
    ips = {row["proxy_ip"] for row in world.db.sessions().values() if row["proxy_ip"]}
    world.record.check(
        f"security_dashboard_and_metrics_{label}",
        not any(secret in page for page in pages for secret in secrets)
        and not any(secret in metrics for secret in secrets)
        and not any(ip in metrics for ip in ips)
        and not any(pid in metrics for pid in world.every_provider_id),
    )


async def _restart(world: World) -> None:
    db = world.db
    provider_ids = {key: row["proxy_session_id"] for key, row in db.ready().items()}
    ips = {key: row["proxy_ip"] for key, row in db.ready().items()}
    checked = {key: row["last_checked_at"] for key, row in db.ready().items()}
    app = world.build(world.settings())
    mark = len(world.proxy.requests)
    lookup_mark = len(world.proxy.ip_lookups)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ui:
            dashboard = (await ui.get("/partials/sessions")).text
            survived = all(ip in dashboard for ip in ips.values())
            rechecked = await _wait(
                lambda: all(db.field(key, "last_checked_at") != value
                            for key, value in checked.items()),
                timeout=45,
            )
            await asyncio.sleep(2.5)
            used = {request.provider_session_id for request in world.proxy.requests[mark:]}
            looked = set(world.proxy.ip_lookups[lookup_mark:])
            world.record.check(
                "G_application_restart_reuses_persisted_ids_for_check_and_ip",
                rechecked
                and survived
                and {key: db.field(key, "proxy_session_id") for key in provider_ids}
                == provider_ids
                and used <= set(provider_ids.values())
                and set(provider_ids.values()) <= looked
                and db.value("SELECT proxy_provider FROM run_config") == "iproyal",
            )
            await _security_audit(world, ui, "after_restart")
            await _scenario_k_reset(world, ui)
            await _settle(world)


async def _restart_without_credentials(world: World) -> None:
    served = world.simulator.requests
    proxied = len(world.proxy.requests)
    lookups = len(world.proxy.ip_lookups)
    app = world.build(world.settings(credentials=False))
    failure: str | None = None
    try:
        async with app.router.lifespan_context(app):
            failure = "started"
    except IPRoyalProxyConfigurationError:
        failure = "IPRoyalProxyConfigurationError"
    except Exception as exc:  # noqa: BLE001 - reported by type only
        failure = type(exc).__name__
    world.record.check(
        "G_restart_without_credentials_fails_closed_with_zero_traffic",
        failure == "IPRoyalProxyConfigurationError"
        and world.simulator.requests == served
        and len(world.proxy.requests) == proxied
        and len(world.proxy.ip_lookups) == lookups
        and not _browser_main_pids(world.backend),
        failure=failure,
    )


async def _scenario_k_reset(world: World, ui: httpx.AsyncClient) -> None:
    db = world.db
    old_ids = set(world.every_provider_id)
    await ui.post("/run/reset", headers=HX)
    reset = await _wait(
        lambda: not db.sessions() and db.value("SELECT COUNT(*) FROM run_config") == 0
    )
    await ui.post("/setup", data={"target_url": world.target, "requested_sessions": "2"})
    fresh = await _wait(lambda: len(db.ready()) == 2, timeout=60)
    new_ids = {row["proxy_session_id"] for row in db.ready().values()}
    world.record.check(
        "K_reset_removes_everything_and_new_run_gets_new_provider_ids",
        reset and fresh and len(new_ids) == 2 and not (new_ids & old_ids),
    )
    world.every_provider_id.update(new_ids)


async def run_backend(backend: BrowserBackendName, directory: Path) -> dict[str, Any]:
    record = Recorder()
    capture = _Capture()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    simulator = LocalQueueSimulator(
        new_identity_prefix=f"p9-{backend.value.lower()}", advertised_host=SIM_HOST
    )
    await simulator.start()
    proxy = LocalAuthProxy(aliases={SIM_HOST: simulator.port})
    await proxy.start()
    database = directory / "operator.sqlite3"
    world = World(
        backend=backend,
        directory=directory,
        database=database,
        db=Database(database),
        simulator=simulator,
        proxy=proxy,
        record=record,
        capture=capture,
        evidence={"browser_backend": backend.value},
    )
    baseline_browsers = len(_browser_main_pids(backend))
    started = time.perf_counter()
    try:
        app = world.build(world.settings())
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ui:
                sessions = await _scenario_a_three_sessions(world, ui)
                first = sessions[0]
                await _scenario_b_repeated_checks(world, first)
                await _scenario_c_and_m_ip_failure_independence(world, first)
                await _scenario_l_ip_change(world, ui, first)
                await _scenario_d_refresh(world, ui, first)
                await _scenario_e_manual_open(world, ui, first)
                await _scenario_f_browser_kill(world)
                added = await _scenario_h_add(world, ui)
                await _scenario_i_replace(world, ui, victim=added or sessions[2], keep=first)
                await _scenario_j_delete(world, ui, sessions[1])
                await _security_audit(world, ui, "during_run")
                await _settle(world)
        await _restart_without_credentials(world)
        await _restart(world)
    finally:
        root.removeHandler(capture)
        root.setLevel(previous_level)
        await proxy.close()
        await simulator.close()

    database_bytes = database.read_bytes() if database.exists() else b""
    logs = "\n".join(capture.lines)
    every_ip = {proxy.exit_ip(pid) for pid in world.every_provider_id} | {CHANGED_EXIT_IP}
    record.check(
        "security_no_credentials_in_sqlite",
        FAKE_USERNAME.encode() not in database_bytes
        and FAKE_PASSWORD.encode() not in database_bytes,
    )
    record.check(
        "security_no_credentials_or_ips_in_logs",
        FAKE_USERNAME not in logs
        and FAKE_PASSWORD not in logs
        and not any(ip in logs for ip in every_ip),
        log_records=len(capture.lines),
    )
    record.check(
        "resources_return_to_baseline",
        len(_browser_main_pids(backend)) <= baseline_browsers
        and int(Database(database).value(
            "SELECT COUNT(*) FROM queue_sessions "
            "WHERE worker_id IS NOT NULL OR manual_owner_id IS NOT NULL"
        ) or 0) == 0,
        browser_processes_after=len(_browser_main_pids(backend)),
    )
    record.check("all_target_traffic_proxied", world.all_traffic_proxied())
    world.evidence.update(
        {
            "duration_seconds": round(time.perf_counter() - started, 1),
            "unique_provider_ids_assigned": len(world.every_provider_id),
            "ip_lookups": len(proxy.ip_lookups),
            "ip_lookups_without_sticky_session": proxy.ip_lookups.count(None),
            "proxied_target_requests": len(proxy.requests),
            "checks": [asdict(check) for check in record.checks],
            "passed": all(check.passed for check in record.checks),
        }
    )
    return world.evidence


async def run_acceptance(backends: tuple[BrowserBackendName, ...]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for backend in backends:
        with tempfile.TemporaryDirectory(prefix=f"phase9-{backend.value.lower()}-") as temp:
            results[backend.value] = await run_backend(backend, Path(temp))
    return {
        "phase": 9,
        "kind": "local_application_acceptance",
        "evidence_boundary": (
            "Local stand-ins only (LocalAuthProxy for IPRoyal and ipify, LocalQueueSimulator "
            "for the target). Not IPRoyal, ipify, or Queue-it evidence."
        ),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "backends": results,
        "passed": all(item["passed"] for item in results.values()),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--backend",
        action="append",
        choices=[BrowserBackendName.PATCHRIGHT.value, BrowserBackendName.CHROME.value],
        help="Backend(s) to run; defaults to Patchright and Chrome.",
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_RESULT_PATH)
    args = parser.parse_args(argv)
    backends = tuple(
        BrowserBackendName.parse(item) for item in (args.backend or ["patchright", "chrome"])
    )
    result = asyncio.run(run_acceptance(backends))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "report": str(args.report)}))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
