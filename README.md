# Queue Load Test

An authorised Queue-it staging test system with bounded browser orchestration and a
local operator dashboard.

This phase intentionally implements only:

- typed configuration and startup validation
- Queue-it session/progress domain models and lifecycle validation
- defensive, browser-independent Queue-it progress parsing
- SQLite session persistence with lightweight work leases
- atomic local JSON browser-state persistence
- shared managed-browser processes and isolated BrowserContext resource management
- live, defensive Queue-it DOM state extraction after JavaScript execution
- supported Queue-it transfer-link capture with explicit identity-mismatch handling
- bounded session creation until the configured unique Queue ID target is reached
- identity-safe TRANSFER_ONLY restoration with HYBRID storage-state fallback
- bounded parked-session monitoring with SQLite leases and adaptive polling
- browser-verified admission, terminal failure classification, and bounded recovery
- signal-aware graceful shutdown that preserves persisted journeys
- structured JSON logging with sensitive transfer URLs excluded
- low-cardinality Prometheus metrics and a lightweight status endpoint
- a lightweight FastAPI/Jinja2/HTMX local operator UI
- unit tests for configuration, domain behavior, parsing, persistence, and the UI

It does not implement PostgreSQL, a client-side application framework, or later
post-admission workflows.

## Install

**Backend policy (Phase 7 accepted, 2026-09-28):** Patchright is the default backend for
new runs. Standard Chrome is the supported fallback. Camoufox is retained as a
dormant/experimental backend: it is still installed and selectable, but it is not
recommended and Phase 7 did not certify it. See `docs/phase7_acceptance.md`.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"           # also pins patchright==1.63.0
python -m playwright install chrome           # installed Chrome used by Patchright and Chrome
queue-load-test-patchright-preflight --context-cycles 5   # default backend readiness
# Optional, only for the retained experimental Camoufox backend:
# camoufox fetch official/stable/152.0.4-beta.30 && queue-load-test-camoufox-preflight
```

The Phase 7 dependency set is Python 3.12+, Patchright 1.63.0, Camoufox 0.5.6,
Playwright 1.62.0, and Camoufox browser build `official/stable/152.0.4-beta.30`.
Patchright uses its own `patchright.*` namespace and driver, so it coexists with the
retained Playwright/Camoufox pins. Its probe launches already-installed Google Chrome
with `channel="chrome"`; no Patchright Chromium download is required and runtime code
does not install a browser. See `docs/phase7_patchright_readiness.md` and
`docs/phase7_patchright_backend_integration.md`. Disposable-context Queue identity and
the complete controlled runtime/dashboard result are in
`docs/phase7_patchright_identity_strategy.md` and
`docs/phase7_patchright_runtime_integration.md`.

The Patchright preflight also runs automatically before a **new** Patchright run is
created. If Chrome or the exact Patchright package is missing, no run is persisted and
the setup page names the remedy and the `BROWSER_BACKEND=chrome` fallback.

The Camoufox preflight launches one Camoufox process, opens one context, loads only an
in-memory `data:` page, and verifies complete cleanup. It sends no Queue-it or staging traffic.
It also runs automatically before a **new** Camoufox run is created. If it fails, no run
is persisted and the setup page shows the exact next step, such as the
`camoufox fetch` command. The newer beta.31 build is visible upstream, but beta.30 is
pinned: upstream recorded Playwright 1.61/1.62 compatibility for it, and this project
validated it. See `docs/phase6_operational_migration.md` for the upgrade, validation,
and rollback procedure.

### Browser backend

`BROWSER_BACKEND=patchright|chrome|camoufox` selects the backend for **new** runs.
Patchright is the default:
- **Recorded per run and session.** The backend is persisted on the immutable run and
  on every session. An existing run always restarts with the backend it was created
  with; changing `BROWSER_BACKEND` never migrates it. To switch backends, use **Stop &
  Reset Run** and start a new run.
- **Legacy runs are Chrome.** Runs and sessions from before Phase 6 have no recorded
  provenance and are treated as Chrome.
- **No cross-engine fallback.** Cross-backend storage-state restoration is refused
  before any browser work.
- **Build provenance.** Camoufox runs record the pinned build; a changed Camoufox build
  is shown and logged (`run_browser_build_changed`). Patchright runs record and display
  the installed Chrome version observed by their successful new-run preflight.

**Patchright (default):** used when `BROWSER_BACKEND` is unset or `patchright`. Setup
runs the exact-package local preflight before persisting anything. Patchright uses its
own async controller beneath the existing `BrowserManager`; the same bounded slots,
context capacity, shared headed/headless budget, deadlines, replacement, and cleanup
rules apply. Patchright uses ordinary shared-process contexts with no Camoufox-style
serialization. Existing runs never rerun new-run preflight and always retain their
persisted backend.

**Chrome fallback:** set `BROWSER_BACKEND=chrome` before creating a run. Chrome uses
installed Google Chrome (`channel="chrome"`) through standard Playwright and passed the
Phase 7 fallback regression.

**Camoufox (retained, not certified):** `BROWSER_BACKEND=camoufox` still creates and
reopens Camoufox runs, and existing Camoufox runs restart as Camoufox. Phase 7 tests
and benchmarks exclude it, and it is kept only for possible future investigation. Its
Phase 6 evidence and limits below are historical.

Queue ID is the authoritative persisted identity. Transfer URL and same-backend HYBRID
storage state restore it in a fresh, disposable context. Fresh Camoufox contexts may
present different fingerprint characteristics, and that is expected. Fingerprint
continuity is neither available in 0.5.6 nor required, and no fingerprint data is
persisted. No backend adds proxy rotation, CAPTCHA solving, WAF-specific
behavior, or traffic interception.

### Monitoring strategy

First-run setup persists an immutable strategy independently from the browser backend:

- **Headed Window Strategy** (`headed_window`) is the existing automatic restore,
  live-page inspection, persistence, and park path. The name does not change automatic
  `HEADLESS` behavior; Manual Open remains the explicit headed operator window.
- **Direct Monitoring Strategy** (`direct`) is a Phase 8 run-provenance choice for
  browser-established sessions with direct visitor-status checking where available and
  browser fallback. In Prompt 1 it uses the existing browser fallback for every check;
  no direct Queue-it request is implemented yet.

`MONITORING_STRATEGY` controls only the default selected for a new setup. Existing runs
restart with `run_config.monitoring_strategy`; changing the environment cannot migrate
them. Legacy runs use `headed_window`, and switching requires Stop & Reset Run. See
`docs/phase8_monitoring_strategy.md`.

Direct runs can opt into Prompt 2 browser-network evidence with
`STATUS_DISCOVERY_ENABLED=true`. This does not enable direct monitoring: it surrounds
the existing browser restore with a bounded observer and writes raw, potentially
sensitive evidence to `.status-discovery/`. The directory and every artifact are
permission-restricted and the directory contains its own ignore-all `.gitignore`.
Never copy these artifacts into reports or commits. An authorised staging scope also
requires `STATUS_DISCOVERY_CONFIRM_AUTHORIZED_STAGING=true`. See
`docs/phase8_status_discovery.md` for limits and the current all-UNKNOWN Queue-it
findings.

Camoufox 0.5.6 runs one live context per managed process. This means:
- automatic concurrency is bounded by `CHROME_PROCESS_COUNT` (at most 4; the name is
  historical and applies to every backend);
- `MONITOR_WORKERS` beyond that number wait for a free process;
- the headed manual pool gives each open window its own process, up to
  `MAX_MANUAL_OPEN_SESSIONS`, while Chrome shares one headed process;
- a connected Camoufox process is restarted automatically when 3 navigations in a row
  time out, when a context close misses its deadline, or when a browser call ignores
  repeated cancellation;
- every browser call is bounded by re-cancellation (Playwright 1.62 can otherwise wait
  forever for a wedged browser), so no Camoufox operation can block shutdown
  indefinitely.

The local recovery and capacity benchmark, run against the simulator only, is:

```powershell
queue-load-test-phase6-camoufox-benchmark --headed --output docs/results/phase6_camoufox_benchmark_result.json
```

Results and limits are in `docs/phase6_camoufox_benchmark.md`.

Phase 7 dependency/readiness, identity, full runtime/dashboard, and benchmark findings
are recorded in the `docs/phase7_patchright_*.md` reports. The Patchright recovery,
concurrency, capacity, and resource benchmark (Chrome control, simulator only) is:

```powershell
queue-load-test-phase7-patchright-benchmark --headed --output docs/results/phase7_patchright_benchmark_result.json
```

Results are in `docs/phase7_patchright_benchmark.md`. The final acceptance run
(Patchright preflight and headed application workflow, the Prompt 5
restoration/recovery scenarios, and the Chrome fallback regression) is:

```powershell
queue-load-test-phase7-acceptance --output docs/results/phase7_acceptance_result.json
```

The decision and matrix are in `docs/phase7_acceptance.md`.

The application is configured through environment variables. Start from:

```powershell
Copy-Item .env.example .env
```

`STAGING_URL` and `TARGET_QUEUE_IDS` remain available to the gated benchmark harnesses.
The operator UI instead takes the actual target and requested count from its first-run
setup and persists them as one immutable current run.

## Local Operator UI

Start the server after configuring bounded browser, database, and state settings:

```powershell
queue-load-test-ui
```

Open `http://127.0.0.1:8000`. `UI_HOST` defaults to `127.0.0.1`, `UI_PORT` defaults to
`8000`, and `MAX_MANUAL_REQUESTED_SESSIONS` defaults to the currently supported 10,000
session safety ceiling. `MAX_MANUAL_OPEN_SESSIONS` defaults to 5 and bounds visible
operator contexts; `MANUAL_OPEN_LEASE_SECONDS` defaults to 30 seconds.
Browser-backed mutations use `OPERATOR_WORKERS` (default 2) and a bounded
`OPERATOR_QUEUE_CAPACITY` (default 10); arbitrary HTTP requests never create arbitrary
background tasks.

On a new empty database, the UI shows setup before any dashboard. Enter an authorised
absolute HTTP(S) staging URL and the requested session count. Start persists the run,
then delegates acquisition to the existing fixed worker pool and bounded queue. It does
not create population-sized tasks, browser processes, or contexts. A database containing
sessions but no run configuration is rejected because those identities cannot safely
be associated with a newly entered target.

The target URL is the authorised *protected* staging page, the page that sends
visitors into the queue. The lifecycle evaluator records a visitor as `ADMITTED` when
normal navigation reaches that URL, so entering the Queue-it waiting-room URL itself
would make every observation look admitted. The setup page states this.

Restarting `queue-load-test-ui` loads the same persisted run, skips setup, counts valid
persisted Queue IDs, and resumes only the remaining acquisition deficit. Existing
identities are never silently retargeted. New sessions acquire their Queue ID in a visible
browser (`CREATION_HEADLESS=false`) and are then monitored headlessly (`HEADLESS=true`);
set `CREATION_HEADLESS=true` to acquire headlessly too.

To start over, use **Stop & Reset Run** on the dashboard. After you confirm, it runs the normal ordered shutdown (creation, monitoring,
operator work, headed browsers, then the automatic browsers). It then deletes the run, every
session and its progress, and all saved browser state, and resets the pause and population
controls. The UI returns to setup, and a restart also opens on setup. Nothing is cancelled
at Queue-it. If the database wipe fails, nothing is deleted and the existing run restarts.

The dashboard shows run/acquisition/monitoring aggregates and a database-paginated
50-row session view. Search supports `session_id` and `queue_id`; filters cover Queue-it
lifecycle and the separate browser ownership view. HTMX refreshes only the summary and
visible page every two seconds. Dashboard requests never allocate a browser context.
Queue-it lifecycle comes directly from persisted evaluator output and is not inferred
from Queue ID or progress presence.

**Pause Monitoring** persists one global automatic-monitoring control flag. A pause
stops new scheduler claims and browser checks; a check already in flight may finish and
persist normally, while claimed-but-not-started work is released without being checked.
The dashboard and due-backlog count continue refreshing. Queue IDs, transfer data,
state, progress, lifecycle status, and `next_check_at` are not changed by the pause.
Acquisition continues independently. **Resume Monitoring** clears the flag and the
bounded scheduler claims normally due rows without resetting timestamps or enqueuing
the full backlog. The state survives restart and repeated pause/resume requests are
idempotent.

This global control is not Queue-it's `PAUSED` lifecycle observation. It affects only
automatic scheduling; headed sessions and manual refresh actions remain separate
controls.

Each persisted row has **Open**. The action restores the expected Queue ID through the
existing transfer-first/HYBRID identity checks and retains a context in one shared
visible browser pool. The historical internal value `OPEN_IN_CHROME` is retained for
database compatibility; the UI presents it as **OPEN IN BROWSER**. It is browser
ownership, not a `QueueStatus`; Queue-it status remains independently visible. An identity mismatch
or restore failure leaves the expected Queue ID unchanged, closes browser resources, and
shows only a sanitized error.

Manual ownership is a persisted, renewable lease that automatic claims exclude in the
same SQLite transaction. A session already being checked reports busy, repeated opens
in the same UI process are idempotent, and manual contexts share the global
`MAX_ACTIVE_CONTEXTS` budget with automatic work. When the configured manual or global
budget is full, the UI reports **Browser capacity currently unavailable**.

Closing the page/window, using **Close**, losing the headed browser process, or shutting
down the application releases ownership. When the page is still inspectable, closure
uses the normal lifecycle evaluator, saves progress and a fresh HYBRID `storage_state`,
and computes the normal next check. If the browser has already gone away, the last persisted
state is preserved and automatic monitoring can recover later. After a crash, restart
clears all persisted headed ownership (see below); no browser-only state can keep a
session open forever.

The remaining per-row controls reuse those same domain services. **Refresh Now** claims
the session, restores its expected identity, runs one normal evaluator/monitor pass,
persists progress and state, and parks it again. It is intentionally available while
automatic monitoring is globally paused, but reports busy during an automatic check,
another refresh, or **Open**. **Delete** means “stop managing this visitor”:
after browser confirmation it removes the session, progress, leases, transfer identity,
and local HYBRID state; it never calls a Queue-it cancellation API. An open headed
session must be closed before deletion.

**Replace** first acquires and persists one valid independent visitor through the
existing creation path, then deletes the selected visitor. Acquisition failure or a
duplicate keeps the old visitor intact. **+ New Session** uses that same bounded creator
to add exactly one extra managed visitor. Neither operation hand-builds transfer URLs.
The persisted `requested_sessions` remains the initial acquisition target: manual Add
may make the valid managed count larger, Delete may make it smaller, and neither causes
automatic refill. A small persisted population adjustment preserves those semantics
across application restart; Replace leaves it unchanged.

Accordingly, the dashboard distinguishes **Requested Sessions**, **Valid Managed
Sessions**, and **Remaining To Initial Target** (`max(0, requested - valid)`). Operator
action status is retained in the application-owned worker manager and appears as
requested, running, success, or failed across HTMX partial refreshes. Errors shown in
the browser are sanitized.

### Reliability, Restart, and Shutdown

See `docs/phase5_ui_reliability.md` for the full audit, fencing matrix, fault tests, and
10,000-row measurements.

- **One UI process per database.** Startup takes an OS lock on `<database>.lock`
  (released automatically on any exit, including `SIGKILL`). A second
  `queue-load-test-ui` on the same database refuses to start. Do not point harness CLIs
  at a database a UI is using.
- **Restart/resume.** A valid persisted run skips setup. Acquisition resumes only the
  deficit to `requested_sessions + operator adjustment`, and a complete target creates
  nothing. Existing identities are never discarded or retargeted.
- **Stale browser ownership.** Because the lock proves no other UI process is alive,
  startup clears every persisted `OPEN_IN_CHROME` owner and automatic/operator lease in
  one transaction, without assuming the old Chrome still exists. Queue IDs, lifecycle,
  progress, and schedules are unchanged, and those sessions are immediately eligible
  for Open, Refresh, and monitoring. While running, an *expired* lease can be taken over
  by a fenced claimer; a live one never is.
- **Monitoring pause persistence.** RUNNING/PAUSED survives restart. A paused restart
  makes no automatic claims or checks, but acquisition still fills any deficit.
- **Conflicting actions.** Each session has persisted, fenced ownership: an automatic
  check, one operator action, or one headed window. Incompatible requests on the same
  session are rejected, and a repeated identical request returns the original action.
  Different sessions run concurrently up to `OPERATOR_WORKERS`. Buttons are disabled
  while their request is in flight and carry a per-render token, so a double-click or
  resubmission does not repeat Add/Replace/Delete.
- **Auto-refresh.** Polls are read-only GETs. A click aborts an in-flight poll, and a
  poll waits behind a mutation. Action feedback lives outside the polled region, so a
  refresh never erases it.
- **Browser crash.** A Chrome or Patchright-controlled Chrome crash during Open or
  while open releases ownership, returns the context budget, and keeps the last
  persisted observation and Queue ID. A crashed headed pool is repaired lazily on the
  next Open.
- **Failures.** A database, route, or template failure returns a sanitized message
  (HTTP 503) into the feedback line and never reaches the scheduler, creation, or
  browser workers.
- **Shutdown (Ctrl+C / SIGTERM).** Uvicorn owns the signals. Shutdown runs in order:
  1. refuse new mutations;
  2. stop creation and automatic claims;
  3. drain automatic checks;
  4. finish or cancel operator work within `SHUTDOWN_TIMEOUT_SECONDS`;
  5. persist a final observation for headed sessions, close their contexts, and release
     ownership;
  6. close Chrome and SQLite.

  Nothing is deleted or replaced.
- **10,000-row dashboard.** `queue-load-test-phase5-ui-benchmark` measured first page
  0.22 ms p95, last page 1.22 ms, search 1.9–2.1 ms, status filter 0.63 ms, and
  summary aggregates 6.6 ms, with 2–4 SQL statements per request. Polling wrote zero
  rows; pause/resume write one row each. This is local synthetic SQLite evidence only,
  not Queue-it throughput.

HTMX 2.0.4 is vendored under `web/static/`; the UI makes no CDN requests.

### Phase 5 Acceptance Workflow

`queue-load-test-phase5-workflow [--headed] [--output result.json]` runs the whole
operator workflow on the real application, with installed Chrome, against
`LocalQueueSimulator` on 127.0.0.1. It covers:

- setup validation and bounded acquisition;
- dashboard, search, filters, and auto-refresh;
- pause/resume;
- headed Open, scheduler skip, Close, and identity mismatch;
- Refresh while paused, Add (duplicate submit), Replace, and Delete;
- graceful shutdown, restart recovery, and partial-acquisition resume.

It needs the `benchmark` extra (`psutil`) and never contacts Queue-it. Results are local
mechanism evidence only; see `docs/phase5_acceptance.md`.

The Phase 7 extension runs the same application workflow with manual/automatic process
kills, restart ownership recovery, backend-provenance fencing, and post-reset backend
change. Patchright uses a real headed manual window by default; Chrome is the control:

```powershell
queue-load-test-phase7-patchright-runtime --output docs/results/phase7_patchright_runtime_result.json
```

It sends traffic only to the local simulator. Camoufox is excluded from this Phase 7
workflow matrix.

The local UI may display session and Queue IDs. It never selects or renders transfer
URLs, storage-state paths/content, cookies, or secrets. Those values remain sensitive;
do not expose the UI beyond a trusted local machine or share its SQLite/state files.

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
session IDs are never labels. The only labels are the fixed `bucket` progress ranges,
the fixed `operation` names on `repository_errors_total`, and histogram `le`, so the
series count does not grow with the persisted population. `/metrics` keeps serving the
last values (and counts a `status` repository error) while the database is unavailable.

JSON log messages and field values have every absolute URL replaced with
`<redacted-url>`, including third-party messages such as Playwright navigation errors.
`configure_structured_logging(run_id=...)` adds a per-run correlation ID to every line.

An importable Grafana dashboard for the Phase 4 metrics is in
[`docs/dashboards/queue_load_test_phase4.json`](docs/dashboards/queue_load_test_phase4.json).
The project does not ship a Grafana or Prometheus deployment; point an existing
Prometheus at `/metrics` and import the file, or read `/status` directly.

## Test

```powershell
python -m pytest
```

The ordinary suite runs against a local Queue-it-shaped simulator. It includes:
- the extended Patchright runtime workflow and Chrome/Camoufox historical regressions;
- a 20-cycle Camoufox park/reopen and process/repository restart run;
- Camoufox recovery scenarios: browser kills, multi-slot failure, and restoration
  faults. It does not contact staging. Tests
marked `staging` are excluded by default even if staging configuration is
present.

To run the authorised staging harness, use a dedicated SQLite database and
state directory, then opt in with both the environment gate and confirmation
flag:

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:PHASE1_OBSERVE_SECONDS = "600"
$env:TARGET_QUEUE_IDS = "10"
$env:CHROME_PROCESS_COUNT = "1"
$env:MAX_CONTEXTS_PER_BROWSER = "5"
$env:MAX_ACTIVE_CONTEXTS = "5"
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

## Current Phase 3 Readiness Defaults

- `TARGET_QUEUE_IDS=1000`
- `SESSION_MODE=HYBRID`
- `CHROME_PROCESS_COUNT=2`
- `MAX_CONTEXTS_PER_BROWSER=25`
- `MAX_ACTIVE_CONTEXTS=50`
- `CREATION_WORKERS=1`
- `CREATION_QUEUE_CAPACITY=5`
- `IDENTITY_REPLACEMENT_LIMIT=0` — acquired Queue IDs that later become `FAILED` (for
  example after an identity mismatch) are not refilled with new identities beyond this
  many replacements; creation stops and sets `queue_identity_replacement_blocked`
- `MONITOR_WORKERS=1`
- `MONITOR_QUEUE_CAPACITY=5`
- `MONITOR_CLAIM_BATCH_SIZE=5`
- `QUEUE_POLL_SECONDS=30`
- adaptive pre-queue, active-queue, serviced-soon, and turn-started intervals
- SQLite via `DATABASE_URL`
- local browser state files in `STATE_DIRECTORY`
- browser-based Queue-it extraction and bounded polling orchestration

The Phase 3 defaults are a conservative readiness profile: the 50-context ceiling is
available for benchmarking, but the default one creation and one monitoring worker do
not attempt to consume it. Structurally valid benchmark profiles use 2/3/4 Chrome
processes with 25 contexts each for global limits of 50/75/100. Those limits are
configuration candidates, not measured safe operating points. `MAX_ACTIVE_CONTEXTS`
is capped at 100 for this phase. Creation and monitoring worker counts must fit within
the global limit; both work queues are explicitly bounded and cannot exceed it.

Phase 2 harnesses retain their exact 100-session gates. Override the Phase 3 defaults
with the documented Phase 2 values when reproducing those benchmarks.

## Phase 3 Synthetic Repository Benchmark

The local benchmark seeds 1,000 mixed synthetic sessions and measures the indexed due
query, bounded transactional claim, update, lease release, and complete scheduler
iteration without Queue-it or browser traffic:

```powershell
queue-load-test-phase3-repository --sessions 1000 --batch-size 50 --samples 20
```

The optional `--database` must name a dedicated empty SQLite file. Use `--report` for
aggregate JSON output. See
[the Phase 3 repository benchmark](docs/phase3-repository-benchmark.md) for the current
local measurements, query plan, synthetic population, and SQLite decision.

## Phase 3 Browser Capacity Benchmark

The controlled browser harness runs the proportional 50/2, 75/3, and 100/4
context/process cases sequentially using installed Google Chrome. Local mode navigates
an in-memory page and needs no staging access:

```powershell
queue-load-test-phase3-browser-capacity --hold-seconds 2 --sample-interval-seconds 0.25 --report phase3-browser-capacity-benchmark.json
```

Queue-it navigation additionally requires `RUN_STAGING_TESTS=1`,
`RUN_PHASE3_BROWSER_BENCHMARK=1`, `--staging`, and
`--confirm-authorized-staging`. The machine-readable report is git-ignored. See
[the Phase 3 browser-capacity benchmark](docs/phase3-browser-capacity-benchmark.md) for
the verified local comparison, resource-measurement caveats, and current staging
`NOT RUN` result. Completing a case does not make its context count a recommended
operating point.

## Phase 3 Queue ID Acquisition Benchmark

The gated acquisition harness creates or resumes toward 1,000 successful unique Queue
IDs using the existing fixed creation workers, bounded queue, shared Chrome processes,
and parked-session persistence. It accepts the conservative 50-context, two-process
Phase 3 profile; this remains a ceiling rather than a staging-safe concurrency claim.

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE3_ACQUISITION_BENCHMARK = "1"
queue-load-test-phase3-acquisition --confirm-authorized-staging --sample-interval-seconds 5 --creation-timeout-seconds 14400 --report phase3-acquisition-benchmark.json
```

An existing partial database is resumed rather than reset. Only successful unique IDs
count, duplicates remain observable failed attempts, and the report contains aggregates
without Queue IDs or transfer URLs. See
[the Phase 3 acquisition benchmark](docs/phase3-acquisition-benchmark.md) for the exact
profile, shutdown behavior, report fields, and current `NOT RUN` result.

## Phase 3 Synthetic Monitoring Sweep

The local monitoring benchmark seeds 1,000 synthetic parked sessions, runs repeated
all-due sweeps through SQLite leases and the bounded scheduler, then processes a
deterministic jittered schedule. It does not start Chrome or contact Queue-it:

```powershell
queue-load-test-phase3-monitoring --database phase3-monitoring-synthetic.sqlite3 --report phase3-monitoring-benchmark.json --workers 20 --queue-capacity 50 --batch-size 50 --sweeps 2
```

Use a dedicated empty database for every run. The report defines full sweep duration,
retains backlog and queue observations, and marks all browser/restore fields UNKNOWN.
See [the Phase 3 monitoring benchmark](docs/phase3-monitoring-benchmark.md) for the
verified local results and their limits.

## Phase 3 Synthetic Persistence Benchmark

The local storage benchmark creates an empty 1,000-session SQLite database and 1,000
synthetic HYBRID browser-state files, then measures save, load, atomic replacement,
delete, consistency scanning, and restart reconstruction:

```powershell
queue-load-test-phase3-storage --database phase3-storage-synthetic/sessions.sqlite3 --state-directory phase3-storage-synthetic/state --sessions 1000 --report phase3-storage-benchmark.json
```

Both paths must be dedicated and empty. To audit an existing database without deleting
or repairing anything, run:

```powershell
queue-load-test-state-check --database queue_load_test.sqlite3 --state-directory .browser-state
```

The checker reports missing, orphaned, corrupt, unreadable, other-session (mismatched),
duplicate/conflicting, insecure-permission, and stale temporary files, and counts
legacy pre-envelope state files. See
[the Phase 3 storage benchmark](docs/phase3-storage-benchmark.md) for measured results
and the explicit cleanup policy, and
[Phase 4 state storage readiness](docs/phase4_state_storage_readiness.md) for the
10,000-file results and the state document format.

## Phase 3 Synthetic Recovery Benchmark

The recovery benchmark seeds 1,000 mixed persisted sessions, performs repeated process-
level repository reopen cycles, verifies a stable identity digest and terminal states,
then resumes a bounded scheduler batch from expired leases:

```powershell
queue-load-test-phase3-recovery --database phase3-recovery-synthetic/sessions.sqlite3 --state-directory phase3-recovery-synthetic/state --restarts 5 --report phase3-recovery-benchmark.json
```

Phase 4 10,000-ID acquisition is separately gated and resumable. Run its no-navigation
preflight first, then enable both staging gates only for the authorised environment:

```powershell
queue-load-test-phase4-acquisition --preflight-only --preflight-report phase4-acquisition-preflight.json
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE4_ACQUISITION_BENCHMARK = "1"
queue-load-test-phase4-acquisition --confirm-authorized-staging --report phase4-acquisition-benchmark.json
```

See `docs/phase4_acquisition.md` for the required bounded profile and persistent paths.

The Phase 4 monitoring harness separately runs repeated deliberate 10,000-row sweeps
and an adaptive/jittered synthetic schedule through the bounded scheduler. It does not
launch Chrome or claim Queue-it restore throughput:

```powershell
queue-load-test-phase4-monitoring --database phase4-monitoring-synthetic.sqlite3 --report phase4-monitoring-benchmark.json --population 10000 --workers 20 --queue-capacity 50 --batch-size 50 --sweeps 2 --sample-interval-seconds 1
```

See [`docs/phase4_monitoring.md`](docs/phase4_monitoring.md) for measured local results
and the real-staging UNKNOWN boundary.

The Phase 4 controlled recovery harness runs 15 failure scenarios over a 10,000-session
population using real SQLite, real state files, `SIGKILL`-ed worker and Chrome processes,
and installed Chrome driven against a **local** Queue-it-like simulator. It sends no
staging traffic and needs the `benchmark` extra (`psutil`):

```powershell
queue-load-test-phase4-recovery --report phase4-recovery-benchmark.json
```

See [`docs/phase4_recovery.md`](docs/phase4_recovery.md) for the measured outcomes and
the boundary between local evidence and real Queue-it recovery, which remains UNKNOWN.

Normal runtime startup uses one aggregate SQLite recovery query and does not scan all
state files. Missing/corrupt state counts require the explicit state consistency scan.
See [the Phase 3 recovery benchmark](docs/phase3-recovery.md) for scenario outcomes and
the boundary between synthetic PASS results and real-staging UNKNOWN results.

## Phase 2 HYBRID Restore Benchmark

The restore benchmark operates on existing HYBRID sessions in the configured SQLite
database. It can probe the official transfer URL, storage state, or the production
transfer-first/storage-fallback path. It is sequential and reuses `BrowserManager`, so
the sample population does not become a matching number of tasks or live contexts.

The benchmark requires both explicit staging gates and confirmation:

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE2_RESTORE_BENCHMARK = "1"
queue-load-test-phase2-restore --confirm-authorized-staging --sample-size 100 --mode all --report phase2-restore-benchmark.json
```

`--mode` also accepts `transfer_only`, `storage_state_only`, or `hybrid`. The JSON report
contains per-invocation identity and sanitized failure evidence plus aggregate success,
mismatch, fallback, duration, percentile, and mechanism reliability statistics. It
never contains transfer URLs or browser state. Because the report does contain Queue
IDs as requested for identity auditing, treat it as sensitive local test evidence; the
default report filename is git-ignored. The terminal summary contains aggregates only.

## Phase 2 Resource and Stability Benchmark

The explicitly gated resource harness creates a 100-session HYBRID population in a
dedicated empty SQLite database, then exercises bounded parked-session monitoring. It
produces comparison-friendly JSON plus an aggregate terminal summary. Install the
optional process sampler for application and Chrome CPU/RAM observations:

```powershell
python -m pip install -e ".[test,benchmark]"
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE2_RESOURCE_BENCHMARK = "1"
queue-load-test-phase2-resources --confirm-authorized-staging --monitoring-seconds 600 --sample-interval-seconds 5 --report phase2-resource-benchmark.json
```

Use a separate empty database and state directory for each one- versus two-Chrome run.
The harness accepts only `TARGET_QUEUE_IDS=100`, `SESSION_MODE=HYBRID`, up to 25 active
contexts, one or two Chrome processes, and SQLite. The report excludes
session/Queue IDs, transfer URLs, and browser state. See
[`docs/phase2-resource-benchmark.md`](docs/phase2-resource-benchmark.md) for the exact
comparison procedure and current evidence status.

## Phase 2 Concurrency Tuning Harness

The tuning harness runs isolated cases sequentially and compares bounded concurrency
levels without selecting an optimal configuration. It supports generated 5/10/15/20/25
context matrices or a JSON manifest that explicitly sets Chrome processes, global and
per-browser capacity, creation/monitor workers, queue capacity, and claim batch size.

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE2_CONCURRENCY_BENCHMARK = "1"
queue-load-test-phase2-tuning --confirm-authorized-staging --matrix-file benchmarks/phase2-concurrency-matrix.example.json --report phase2-concurrency-benchmark.json
```

The aggregate output contains comparison rows and objective saturation flags, not a
winner. See [the concurrency benchmark guide](docs/phase2-concurrency-benchmark.md).
