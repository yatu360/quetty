# Phase 3 Queue ID Acquisition Benchmark

## Current Result

**NOT RUN** as of 2026-09-26. The authorised Queue-it staging configuration and both
execution gates were unavailable. Creation throughput, real duplicate and failure
rates, Queue-it navigation reliability, CPU/RAM during acquisition, and the observed
active-context peak therefore remain **UNKNOWN**.

## Bounded Profile

The harness accepts the conservative Phase 3 browser candidate exercised locally in
Prompt 3:

- `TARGET_QUEUE_IDS=1000`
- `SESSION_MODE=HYBRID`
- `CHROME_PROCESS_COUNT=2`
- `MAX_CONTEXTS_PER_BROWSER=25`
- `MAX_ACTIVE_CONTEXTS=50`
- bounded `CREATION_WORKERS` and `CREATION_QUEUE_CAPACITY`
- SQLite persistence and the existing unique nullable `queue_id` constraint

The 50-context value is a ceiling, not a demand or a staging-safe claim. The default
single creation worker does not consume all 50 slots. The acquisition controller uses
a fixed worker pool and never creates one task or BrowserContext per persisted session.

Each successful visitor is persisted and parked before its context closes. Duplicate
Queue IDs create an observable failed attempt without modifying the previously stored
session or counting toward the target. The controller keeps in-flight work no larger
than the remaining target deficit, so one controller does not overshoot merely because
several workers finish near 1,000.

## Restart and Shutdown

The current successful unique-ID count is read from SQLite when the controller starts.
For example, a database containing 613 successful IDs causes the next run to request
only the remaining 387. A target already at or above 1,000 schedules no creation work
and does not start Chrome in the benchmark harness.

On a graceful stop, no new work is scheduled and already-issued bounded work drains.
If the runtime shutdown timeout cancels acquisition, all fixed worker tasks are
cancelled without blocking on full queues. Any session committed before cancellation
remains authoritative and is discovered on restart.

## Report

The machine-readable JSON and terminal summary include:

- initial and final successful unique-ID counts;
- creation attempts, completed work items, newly acquired IDs, duplicates, failures,
  and retries;
- wall duration, summed creation duration, sessions/s, and p50/p95 latency;
- navigation failures, context creation failures, browser crashes, cleanup failures,
  and peak active contexts;
- sampled application/Chrome CPU and RSS, managed/observed processes, active contexts,
  and bounded queue depth.

The report contains no Queue IDs, session IDs, transfer URLs, or browser state. Its
default filename is ignored by Git.

## Authorised Command

Use the intended persistent SQLite database and matching state directory when resuming.
Set both explicit gates only for an authorised staging event:

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE3_ACQUISITION_BENCHMARK = "1"
queue-load-test-phase3-acquisition --confirm-authorized-staging --sample-interval-seconds 5 --creation-timeout-seconds 14400 --report phase3-acquisition-benchmark.json
```

The resulting measurements apply only to that host, event, and configuration. They do
not establish Phase 4 capacity or justify 10,000-session extrapolation.
