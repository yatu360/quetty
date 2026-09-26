# Phase 4 10,000-Session Acquisition

## Current Result

**Authorised staging acquisition: NOT RUN. Result: UNKNOWN.**

No `.env` containing the authorised staging URL exists in this checkout, and neither
`RUN_STAGING_TESTS=1` nor `RUN_PHASE4_ACQUISITION_BENCHMARK=1` was configured. No
Queue-it page was opened and no staging traffic was sent. Therefore the repository has
no legitimate measurements for final Queue ID count, acquisition duration, throughput,
duplicate/failure incidence, Queue-it navigation, real state persistence, or sustained
browser resources at the 10,000 target.

The deployment remains **single machine**: local SQLite, local JSON browser state, one
bounded `BrowserManager`, two or fewer Chrome processes, and fixed local creation
workers. Distributed execution remains deferred by
`docs/phase4_distributed_worker_decision.md`.

## Implemented Harness

`queue-load-test-phase4-acquisition` provides two modes:

1. `--preflight-only` validates local readiness without navigating to the staging URL.
2. `--confirm-authorized-staging`, together with both environment gates, runs or safely
   resumes the bounded acquisition.

The accepted Phase 4 profile is intentionally capped at:

- `TARGET_QUEUE_IDS=10000`
- `SESSION_MODE=HYBRID`
- one or two installed Google Chrome processes;
- at most 25 contexts per Chrome process;
- at most 50 active contexts globally;
- at most 10 creation workers; and
- at most 10 queued creation items.

Lower bounded values are allowed. Values above these limits fail before browser launch.
The controller continues from the successful unique Queue ID count already in SQLite,
contracts in-flight work as the target approaches, and queries SQLite authoritatively
before declaring completion. Failed and duplicate outcomes do not advance the target.

## Preflight

Before staging navigation, the harness checks and records:

- a non-placeholder configured staging URL;
- validated Phase 4 process/context/worker/queue limits;
- SQLite initialization, reachability, current target, and current successful-ID count;
- active and expired lease counts;
- an atomic state write/delete probe;
- at least the configured minimum free disk space (default 1 GiB);
- installed Google Chrome launch through `channel="chrome"`, creation and cleanup of one
  fresh context, and a return to zero active contexts;
- Prometheus registry availability; and
- configured shutdown timeout plus repository recovery-summary availability.

Active or expired leases produce a warning rather than silently disappearing. A failed
capacity, URL, database, state, disk, browser, metrics, or recovery check prevents the
run and leaves a machine-readable preflight report.

A local no-navigation harness validation ran on 2026-09-27 using a temporary SQLite
database/state directory and the test-only hostname `queue.staging.test`. It passed all
local mechanics, launched two Chrome processes, created and closed one context, returned
to zero contexts, observed zero existing IDs and zero leases, and reported
16,465,584,128 free bytes. This is **not** an authorised staging preflight or Queue-it
result because the URL was not contacted.

## Run Measurements

The JSON run report contains only aggregate data and never transfer URLs or Queue IDs:

- initial/final successful unique-ID counts and bounded overshoot;
- attempts, completed work items, successes, duplicates, transient/permanent failures,
  retry count, and state persistence failures;
- wall and aggregate creation duration, sessions/s, and creation p50/p95;
- context acquisition and navigation latency summaries;
- browser crashes, context creation/cleanup failures, navigation failures, and active-
  context peak;
- fixed-interval application/Chrome CPU and RAM samples;
- per-local-worker completed/success/unexpected-failure counts and throughput;
- deployment model, lease conflicts/recoveries, timeout/interruption state; and
- post-run active-context, active/expired-lease, and state-consistency aggregates.

After normal completion, timeout, or a supported signal, the controller stops
replenishing work and drains only its bounded in-flight work within the configured
shutdown timeout. Successfully committed sessions remain in SQLite for the next run.
Post-run verification recounts unique IDs, checks that live contexts returned to zero,
reads lease totals, and performs the report-only state consistency scan.

## Commands

Use dedicated, persistent database and state paths. Do not use temporary paths for a
real acquisition because resume depends on them.

Local preflight only (no staging navigation):

```powershell
$env:STAGING_URL = "https://<authorised-staging-host>/<journey>"
$env:TARGET_QUEUE_IDS = "10000"
$env:SESSION_MODE = "HYBRID"
$env:CHROME_PROCESS_COUNT = "2"
$env:MAX_CONTEXTS_PER_BROWSER = "25"
$env:MAX_ACTIVE_CONTEXTS = "50"
$env:CREATION_WORKERS = "10"
$env:CREATION_QUEUE_CAPACITY = "10"
$env:DATABASE_URL = "sqlite:///phase4-sessions.sqlite3"
$env:STATE_DIRECTORY = ".phase4-browser-state"
queue-load-test-phase4-acquisition --preflight-only `
  --preflight-report phase4-acquisition-preflight.json
```

Authorised run or resume:

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE4_ACQUISITION_BENCHMARK = "1"
queue-load-test-phase4-acquisition --confirm-authorized-staging `
  --creation-timeout-seconds 86400 `
  --sample-interval-seconds 5 `
  --preflight-report phase4-acquisition-preflight.json `
  --report phase4-acquisition-benchmark.json
```

Re-running the same command with the same database/state paths resumes from the
persisted successful count. It does not discard the existing population.

## Acceptance Fields

| Result | Status |
|---|:---:|
| Real 10,000-session acquisition ran | **NO / NOT RUN** |
| Final unique Queue ID count | **UNKNOWN** |
| Total acquisition duration | **UNKNOWN** |
| Sessions per second | **UNKNOWN** |
| Duplicate count | **UNKNOWN** |
| Failure and retry counts | **UNKNOWN** |
| CPU/RAM and active-context peak | **UNKNOWN** |
| Browser crashes/navigation failures | **UNKNOWN** |
| State consistency for real population | **UNKNOWN** |
| Distribution observations | **N/A — single-machine deployment** |

## Next Task

**Phase 4 Prompt 6 — 10,000-Session Monitoring and Sweep Benchmark.** A real full-
population sweep remains blocked until an authorised 10,000-session population exists;
the monitoring harness must report that limitation rather than fabricate results.
