# Queue Load Test

Phase 1 foundation for an authorised Queue-it staging test system.

This phase intentionally implements only:

- typed configuration and startup validation
- Queue-it session/progress domain models and lifecycle validation
- defensive, browser-independent Queue-it progress parsing
- SQLite session persistence with lightweight work leases
- atomic local JSON browser-state persistence
- shared Google Chrome process and isolated BrowserContext resource management
- live, defensive Queue-it DOM state extraction after JavaScript execution
- supported Queue-it transfer-link capture with explicit identity-mismatch handling
- bounded session creation until the configured unique Queue ID target is reached
- identity-safe TRANSFER_ONLY restoration with HYBRID storage-state fallback
- bounded parked-session monitoring with SQLite leases and adaptive polling
- browser-verified admission, terminal failure classification, and bounded recovery
- signal-aware graceful shutdown that preserves persisted journeys
- structured JSON logging with sensitive transfer URLs excluded
- low-cardinality Prometheus metrics and a lightweight status endpoint
- unit tests for configuration, domain behavior, parsing, and persistence

It does not implement PostgreSQL, a full frontend dashboard, or later
post-admission workflows.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m playwright install chrome
```

The browser manager uses Playwright's async API to launch the installed Google
Chrome build with `channel="chrome"`. It shares each Chrome process across
isolated browser contexts and does not implement stealth or automation hiding.

The application is configured through environment variables. Start from:

```powershell
Copy-Item .env.example .env
```

The SQLite database, transfer URLs, and browser-state files contain sensitive
session data. The default local files are git-ignored and should not be logged
or shared. Queue IDs and transfer URLs are also excluded from model result
representations.

HYBRID restoration prefers the official Queue-it transfer URL and supplies saved
Playwright storage state only as a fallback. Storage state is not a complete
browser snapshot: it does not preserve JavaScript memory, timers, WebSockets,
the execution stack, every form of session storage, every browser store, or live
service-worker state.

`TURN_STARTED` records Queue-it entering its service/redirect stage. A session
is only marked `ADMITTED` after normal browser navigation reaches the configured
staging destination. The application does not extract or manipulate Queue-it
admission tokens.

## Observability

Application logs can be configured as structured JSON with
`configure_structured_logging()`. Operational events include stable context
such as session, worker, browser, attempt, status, duration, restore method, and
error type when available. Transfer URLs are intentionally excluded from normal
event logs.

`ObservabilityHttpServer` exposes a compact text summary at `/status` and
Prometheus exposition at `/metrics` on `PROMETHEUS_PORT` (default `9090`). The
metrics use only aggregate values and bounded histogram buckets; Queue IDs and
session IDs are never labels.

## Test

```powershell
python -m pytest
```

The ordinary suite includes a controlled 10-session Chrome integration run
against a local Queue-it-shaped simulator. It does not contact staging. Tests
marked `staging` are excluded by default even if staging configuration is
present.

To run the authorised staging harness, use a dedicated SQLite database and
state directory, then opt in with both the environment gate and confirmation
flag:

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:PHASE1_OBSERVE_SECONDS = "600"
queue-load-test-phase1 --confirm-authorized-staging --observe-seconds 600 --report phase1-acceptance.json
```

The harness refuses profiles that differ from 10 Queue IDs, HYBRID mode, one
Chrome process, five contexts per browser, a five-context global limit, and
SQLite. Its JSON report contains aggregate timing and result counts, not Queue
IDs or transfer URLs. A longer observation window may be required to encounter
every real Queue-it lifecycle state.
Session acquisition is bounded by a configurable 600-second harness timeout so
a broken staging journey still produces a finite acceptance result.

The same run can be invoked through pytest when deliberately requested:

```powershell
python -m pytest -o addopts="" -m staging tests/staging
```

See [the Phase 1 acceptance report](docs/phase1-acceptance-report.md) for the
controlled evidence and the staging assumptions that remain unknown.

## Initial Phase 1 Defaults

- `TARGET_QUEUE_IDS=10`
- `SESSION_MODE=HYBRID`
- `CHROME_PROCESS_COUNT=1`
- `MAX_CONTEXTS_PER_BROWSER=5`
- `MAX_ACTIVE_CONTEXTS=5`
- `QUEUE_POLL_SECONDS=30`
- adaptive pre-queue, active-queue, serviced-soon, and turn-started intervals
- SQLite via `DATABASE_URL`
- local browser state files in `STATE_DIRECTORY`
- browser-based Queue-it extraction and bounded polling orchestration
