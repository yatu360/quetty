# Phase 2 Acceptance Report

**Date:** 2026-09-26  
**Decision:** **PARTIAL — Phase 3 is blocked pending authorised Phase 2 evidence.**  
**Status totals:** 3 PASS, 2 FAIL, 15 UNKNOWN.

This report uses checked-in reports, tests, metrics definitions, Git history, and the
complete workspace artifact inventory as evidence. No Phase 2 benchmark output,
staging result, metrics snapshot, SQLite population, or browser-state work directory is
present. Harness implementation and unit tests are not treated as staging results.

## Configuration

The target profile is `TARGET_QUEUE_IDS=100`, `MAX_ACTIVE_CONTEXTS=25`,
`SESSION_MODE=HYBRID`, and either one Chrome process with
`MAX_CONTEXTS_PER_BROWSER=25` or two with `MAX_CONTEXTS_PER_BROWSER=13`.

Local deterministic tests exercised:

- 100 HYBRID records with 10 fixed creation workers and a queue capacity of 10;
- 100 parked records with five workers and a 25-item monitoring queue (80 due);
- 100 due records with two workers and a two-item monitoring queue;
- fake BrowserManager profiles of one process/25 contexts and two processes/13 contexts
  per process/25 globally.

No authorised staging configuration was executed, so neither target browser profile is
a measured operating point.

## Test Environment

Known facts only: the project targets Python 3.12, Playwright's asynchronous API,
installed Google Chrome, SQLite, and local JSON storage state. The normal suite includes
local controlled-browser integration tests and excludes four explicitly gated staging
harnesses by default. No intended deployment-host specification or staging event result
is recorded.

## Functional Results

Deterministic tests created exactly 100 unique scripted Queue IDs with concurrency fixed
at 10, continued from 90 persisted IDs, and scheduled only one attempt at 99/100. A
separate 100-row scheduler test kept 75 rows unclaimed while its bounded queue held 25.
A saturated test kept only two active checks plus two queued leases for 100 due rows.
These are sufficient evidence for local bounded architecture, persistence, and fixed
task counts, but not for reliable acquisition or identity continuity against Queue-it.

## Creation Benchmark

**UNKNOWN.** The authorised acquisition benchmark was not run. No measured sessions/s,
creation latency, elapsed acquisition time, duplicate rate, or staging failure rate
exists. A synthetic unit test asserts a positive calculated rate, but its scripted
1 ms sleep is not a benchmark and no value is retained.

## Monitoring Benchmark

**UNKNOWN.** No real checks/s, check latency, sweep time, or backlog drain rate exists.
Local tests show bounded behavior: 25 of 80 due rows were claimed into a 25-item queue,
leaving a reported due backlog of 55; with blocked two-worker/two-queue processing,
four rows were owned and 96 remained due. This proves backpressure and visibility, not
sustainable backlog control.

## Restore Reliability

The Phase 2 restore harness was **NOT RUN**.

| Measure | Result |
|---|---:|
| Transfer attempts / successes / failures | UNKNOWN / UNKNOWN / UNKNOWN |
| Transfer success rate | UNKNOWN |
| `storage_state` attempts / successes / failures | UNKNOWN / UNKNOWN / UNKNOWN |
| `storage_state` success rate | UNKNOWN |
| HYBRID fallback attempts and outcomes | UNKNOWN |
| Identity mismatches | UNKNOWN |

Unit tests verify accounting, fallback rules, and expected-identity immutability, but do
not establish real Queue-it reliability.

## Browser Stability

Browser crashes, context creation failures, and navigation failures are all
**UNKNOWN** for the target run. Fake-based tests verify that failed context creation
releases capacity, a disconnected process can be replaced, a healthy second process
remains usable, and shutdown closes owned contexts. No real Chrome crash or recovery
was observed under Phase 2 load.

## Resource Usage

The resource benchmark was **NOT RUN**. Application/Chrome CPU average and peak, RAM
average and peak, and observed active-context peak are all **UNKNOWN**. The configured
cap is 25; it is not an observed peak. No host-headroom conclusion is possible.

## Concurrency Matrix

No concurrency level was benchmarked. The harness defines 5, 10, 15, 20, and 25-context
cases, but definition and unit testing are not execution evidence; consequently this
report includes no tested-level result rows and no saturation point.

## One Browser vs Two Browsers

**UNKNOWN.** Fake allocation tests show that 25 contexts distribute 13/12 across two
slots and that the global cap remains 25. There are no objective throughput, latency,
CPU, RAM, failure, or crash measurements comparing one real Chrome process with two.

## Acceptance Matrix

| Question | Status | Evidence | Notes |
|---|:---:|---|---|
| 1. Can the system acquire 100 unique Queue IDs reliably? | UNKNOWN | Scripted local test reached exactly 100; staging acquisition not run. | Scripted IDs do not prove Queue-it reliability. |
| 2. Does creation remain bounded by Chrome/context capacity? | PASS | Fixed creation workers/queue; BrowserManager enforces per-process and global limits, including a 25-context cap. | Capacity evidence is deterministic/local. |
| 3. Can 100 sessions remain parked without 100 live contexts? | PASS | 100 SQLite rows persisted; scheduler claimed at most 25, and another test owned only four. | Establishes the parked-session architecture locally. |
| 4. Does the scheduler avoid unbounded task creation? | PASS | Worker task count stayed fixed at two; queues and claim batches were bounded. | Population size did not determine task count. |
| 5. What creation throughput was observed? | UNKNOWN | No authorised benchmark output. | No sessions/s or latency result. |
| 6. What monitoring throughput was observed? | UNKNOWN | No authorised benchmark output. | No checks/s or sweep timing. |
| 7. What transfer restore success rate was observed? | UNKNOWN | Restore benchmark not run. | Attempts and outcomes absent. |
| 8. What `storage_state` success rate was observed? | UNKNOWN | Restore benchmark not run. | Attempts and outcomes absent. |
| 9. Were identity mismatches observed? | UNKNOWN | No Phase 2 restore/acquisition result. | Tests cover detection, not real incidence. |
| 10. What CPU usage was observed? | UNKNOWN | Resource benchmark not run. | Average and peak absent. |
| 11. What RAM usage was observed? | UNKNOWN | Resource benchmark not run. | Average and peak absent. |
| 12. Were Chrome crashes observed? | UNKNOWN | No Phase 2 browser run. | Fake disconnect recovery is not incidence data. |
| 13. Were context creation failures observed? | UNKNOWN | No Phase 2 browser run. | Failure-path tests are not incidence data. |
| 14. Were navigation failures observed? | UNKNOWN | No Phase 2 browser run. | Failure-path tests are not incidence data. |
| 15. How did one Chrome process compare with two? | UNKNOWN | Comparative runs not executed. | Allocation correctness only. |
| 16. Where did latency/failures begin to worsen? | UNKNOWN | Concurrency matrix not run. | No tested saturation level. |
| 17. Did scheduler backlog remain controlled? | UNKNOWN | Backpressure is proven; real drain rate and sustained backlog are unmeasured. | Synthetic blocked backlog reached 96 by design. |
| 18. Are correctness or identity issues unresolved? | UNKNOWN | Local invariants pass; real identity continuity and mismatch incidence are unverified. | No known local corruption, but staging evidence is absent. |
| 19. Are there blockers before Phase 3? | FAIL | Required acquisition, restore, throughput, stability, resource, and saturation gates lack staging evidence. | Phase 3 must not start on current evidence. |
| 20. Is the architecture ready to test 1,000 sessions? | FAIL | Phase 2 has no measured operating point or host headroom. | The design is scale-oriented, but readiness is not established. |

## Known Issues

- No authorised 100-session acquisition, monitoring, restore, resource, or concurrency
  run exists.
- Real Queue-it identity continuity, mismatch incidence, transfer reliability,
  `storage_state` reliability, and fallback behavior remain unknown.
- Creation/check throughput, latency, backlog drain, CPU/RAM headroom, and real Chrome
  stability remain unknown.
- One-versus-two-process performance and the safe concurrency ceiling are unknown.
- The general `queue-load-test` CLI validates configuration but does not assemble the
  full runtime unless one is injected programmatically.
- SQLite due-query behavior is tested at 100 rows but not benchmarked under real work;
  cross-process lease renewal and distributed target coordination remain out of scope.

## Phase 3 Readiness

Passed Phase 2 gates are limited to local architectural properties: creation/context
capacity is bounded, 100 persisted sessions can remain parked, and monitoring uses a
fixed worker pool plus bounded queue and claims. No functional Phase 2 staging or
performance gate passed. Fifteen questions remain UNKNOWN, and the two readiness
questions FAIL.

Before **Phase 3 Prompt 1**, run the authorised Phase 2 acquisition/resource benchmark
for equivalent one- and two-browser profiles, the restore benchmark, and the approved
concurrency subset. Record exact configuration, creation/check rates and latency,
backlog behavior, transfer/storage/fallback outcomes, identity mismatches, CPU/RAM,
active-context peak, and browser/context/navigation failures. Until that evidence is
reviewed, selecting approximately 50–100 active contexts for
`TARGET_QUEUE_IDS=1000` would be assumption rather than an evidence-based decision.
