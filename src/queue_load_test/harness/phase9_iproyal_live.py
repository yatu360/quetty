"""Gated live revalidation of IPRoyal sticky-session continuity on the production path.

Runs only with ``RUN_PHASE9_IPROYAL_LIVE=1`` **and** ``--confirm-live-iproyal``.
Otherwise it sends nothing and writes a ``NOT RUN`` report. It uses the production
``SessionProxyResolver`` (credentials from ``.env``), ``BrowserManager`` with
Patchright, and ``ProxyIpObserver`` against ipify. Its only targets are ipify's
lightweight JSON endpoint, opened in a temporary context and with httpx. It never
contacts Queue-it.

For each sticky session it checks that the browser context and the production
post-check observer see the *same* exit through the *same* sticky session. It then
measures the affinity of one session across:

- a fresh context;
- a browser-process restart;
- an independent Python process that reloads the persisted row and re-reads
  credentials;
- optional idle checkpoints, with nothing kept open in between.

The report contains salted-hash comparisons only: no IPs, credentials, or provider
session IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from queue_load_test.browser import BrowserManager, PatchrightBackend
from queue_load_test.config import Settings
from queue_load_test.models import (
    BrowserBackendName,
    ProxyProvider,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.proxy import (
    DEFAULT_PROXY_IP_ENDPOINT,
    ProxyIpObserver,
    ProxyResolutionError,
    SessionProxyResolver,
    generate_proxy_session_id,
    parse_proxy_ip_response,
)
from queue_load_test.repository import SQLiteSessionRepository

_MODULE = "queue_load_test.harness.phase9_iproyal_live"
LIVE_GATE = "RUN_PHASE9_IPROYAL_LIVE"
SALT_ENV = "PHASE9_LIVE_SALT"
DEFAULT_REPORT = Path("docs/results/phase9_iproyal_live_result.json")
SESSIONS = 3


def _hash(ip: str | None, salt: str) -> str | None:
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest() if ip else None


def _affinity(baseline: str | None, current: str | None) -> str:
    if baseline is None or current is None:
        return "UNKNOWN"
    return "SAME" if baseline == current else "CHANGED"


def _run(settings: Settings) -> RunConfig:
    return RunConfig(
        run_id="phase9-live",
        target_url="https://api.ipify.org/",
        requested_sessions=SESSIONS,
        created_at=datetime.now(UTC),
        browser_backend=BrowserBackendName.PATCHRIGHT,
        proxy_provider=ProxyProvider.IPROYAL,
        proxy_country=settings.iproyal_proxy_country,
        proxy_lifetime=settings.iproyal_proxy_lifetime,
    )


def _manager() -> BrowserManager:
    return BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=SESSIONS,
        max_active_contexts=SESSIONS,
        headless=True,
        backend=PatchrightBackend(),
        operation_timeout_seconds=45,
        close_timeout_seconds=10,
    )


async def _browser_ip(
    manager: BrowserManager, resolver: SessionProxyResolver, session: QueueSession
) -> str | None:
    resolved = resolver.resolve(session)
    assert resolved is not None
    async with manager.context(proxy=resolved.browser_proxy()) as context:
        page = await context.new_page()
        response = await page.goto(DEFAULT_PROXY_IP_ENDPOINT, timeout=30_000)
        if response is None or response.status != 200:
            return None
        try:
            return parse_proxy_ip_response((await response.body())[:1_024])
        except ValueError:
            return None


async def _observer_ip(resolver: SessionProxyResolver, session: QueueSession) -> str | None:
    resolved = resolver.resolve(session)
    assert resolved is not None
    observation = await ProxyIpObserver(timeout_seconds=15, connect_timeout_seconds=10).observe(
        resolved
    )
    return observation.ip


async def _child(database: Path, session_id: str) -> None:
    """Independent process: reload the row and credentials, observe once, print a hash."""

    settings = Settings()
    repository = SQLiteSessionRepository(database)
    try:
        run = await repository.get_active_run()
        session = await repository.get(session_id)
        assert run is not None and session is not None
        ip = await _observer_ip(SessionProxyResolver.for_run(run, settings), session)
    finally:
        await repository.close()
    print(json.dumps({"hash": _hash(ip, os.environ[SALT_ENV])}))


async def run_live(checkpoints: list[int]) -> dict[str, Any]:
    salt = secrets.token_hex(16)
    settings = Settings()
    run = _run(settings)
    resolver = SessionProxyResolver.for_run(run, settings)
    report: dict[str, Any] = {
        "kind": "live_iproyal_continuity",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "configuration": {
            "provider": "iproyal",
            "country": run.proxy_country,
            "lifetime": run.proxy_lifetime,
            "browser_backend": "patchright",
            "ip_endpoint": DEFAULT_PROXY_IP_ENDPOINT,
            "sessions": SESSIONS,
        },
        "evidence_boundary": (
            "Real IPRoyal Residential and ipify only; no Queue-it traffic. Only the "
            "intervals listed under continuity were measured."
        ),
    }
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="phase9-live-") as temp:
        database = Path(temp) / "live.sqlite3"
        repository = SQLiteSessionRepository(database)
        await repository.create_run(run)
        sessions: list[QueueSession] = []
        for index in range(SESSIONS):
            session = QueueSession(
                session_id=f"live-{index}",
                queue_id=None,
                transfer_url="",
                mode=SessionMode.TRANSFER_ONLY,
                browser_backend=BrowserBackendName.PATCHRIGHT,
                proxy_session_id=generate_proxy_session_id(),
                status=QueueStatus.PARKED,
                state_path=Path(temp) / f"live-{index}.json",
            )
            await repository.create(session)
            sessions.append(session)
        try:
            resolver.resolve(sessions[0])
        except ProxyResolutionError as exc:
            await repository.close()
            report["outcome"] = "NOT RUN"
            report["reason"] = exc.failure.value
            return report

        manager = _manager()
        await manager.start()
        per_session: list[dict[str, Any]] = []
        baselines: dict[str, str | None] = {}
        try:
            for session in sessions:
                browser_ip = await _browser_ip(manager, resolver, session)
                observer_ip = await _observer_ip(resolver, session)
                if observer_ip is not None:
                    await repository.record_proxy_ip(
                        session.session_id, ip=observer_ip, observed_at=datetime.now(UTC)
                    )
                baselines[session.session_id] = observer_ip
                per_session.append(
                    {
                        "browser_and_observer_same_exit": _affinity(browser_ip, observer_ip),
                        "observed": observer_ip is not None,
                    }
                )
            first = sessions[0]
            baseline = baselines[first.session_id]
            fresh = await _browser_ip(manager, resolver, first)
            await manager.shutdown()
            manager = _manager()
            await manager.start()
            restarted = await _browser_ip(manager, resolver, first)
        finally:
            await manager.shutdown()
        persisted = await repository.get(first.session_id)
        await repository.close()

        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", _MODULE, "--child", str(database), first.session_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, SALT_ENV: salt},
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=120)
        text = stdout.decode().strip()
        child_hash = (
            json.loads(text.splitlines()[-1]).get("hash")
            if process.returncode == 0 and text
            else None
        )
        base_hash = _hash(baseline, salt)
        continuity: dict[str, str] = {
            "fresh_context": _affinity(baseline, fresh),
            "browser_process_restart": _affinity(baseline, restarted),
            "python_application_restart": (
                "UNKNOWN" if child_hash is None or base_hash is None
                else "SAME" if child_hash == base_hash else "CHANGED"
            ),
        }
        checkpoint_start = time.monotonic()
        repository = SQLiteSessionRepository(database)
        for minutes in sorted(checkpoints):
            # Nothing is held open between checkpoints: no context, process, or socket.
            await asyncio.sleep(max(0.0, checkpoint_start + minutes * 60 - time.monotonic()))
            current = await _observer_ip(resolver, first)
            continuity[f"T+{minutes}m_disconnected"] = _affinity(baseline, current)
        await repository.close()

    distinct = len({value for value in baselines.values() if value is not None})
    report.update(
        {
            "outcome": "COMPLETED",
            "duration_seconds": round(time.perf_counter() - started, 1),
            "distinct_provider_session_ids": len({s.proxy_session_id for s in sessions}),
            "per_session": per_session,
            "distinct_exit_ips_observed": distinct,
            "persisted_provider_id_unchanged": persisted is not None
            and persisted.proxy_session_id == sessions[0].proxy_session_id,
            "persisted_ip_recorded": persisted is not None and persisted.proxy_ip is not None,
            "continuity": continuity,
            "intervals_not_run": [
                f"T+{minutes}m" for minutes in (60, 90, 115) if minutes not in checkpoints
            ],
        }
    )
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Gated live IPRoyal continuity revalidation")
    parser.add_argument("--confirm-live-iproyal", action="store_true")
    parser.add_argument("--checkpoints", default="", help="Comma-separated minutes, e.g. 5,30")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--child", nargs=2, metavar=("DATABASE", "SESSION_ID"))
    args = parser.parse_args(argv)
    if args.child:
        asyncio.run(_child(Path(args.child[0]), args.child[1]))
        return
    checkpoints = [int(item) for item in args.checkpoints.split(",") if item.strip()]
    if os.environ.get(LIVE_GATE) != "1" or not args.confirm_live_iproyal:
        report: dict[str, Any] = {
            "kind": "live_iproyal_continuity",
            "outcome": "NOT RUN",
            "reason": f"requires {LIVE_GATE}=1 and --confirm-live-iproyal",
        }
    else:
        report = asyncio.run(run_live(checkpoints))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"outcome": report["outcome"], "report": str(args.report)}))


if __name__ == "__main__":
    main()
