# Queue Load Test

Phase 1 foundation and Phase 2 scaling-ready configuration for an authorised Queue-it
staging test system.

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
