# Phase 4 Resilience, Recovery, and Observability at 10,000 Sessions

**Date:** 2026-09-27  
**Deployment model:** single machine, SQLite, local state storage  
**Host:** Apple M5 Pro, 15 cores, 24 GiB, macOS 26.5.1, Python 3.14.7, Google Chrome 153  
**Authorised Queue-it run:** **NOT RUN / UNKNOWN**

## Outcome

`queue-load-test-phase4-recovery` ran 15 controlled failure scenarios over a
10,000-session persisted population. **All 15 executed scenarios PASS** as local
controlled evidence. Distributed worker crash, distributed node restart, and real
Queue-it recovery were **not executed** and remain **UNKNOWN**.

The evidence is local. The harness uses real SQLite, 9,900 real atomic state files,
real `SIGKILL`ed worker and Chrome processes, and installed Google Chrome (two managed
processes, 50-context ceiling, 20 monitoring workers). Chrome navigates only to
`LocalQueueSimulator`, a local page server that mimics the page structure the project's
own extractors read. It is not Queue-it. A PASS here proves the application's recovery
mechanics. It does not prove how Queue-it behaves after a restore, a crash, or a delay.

The aggregate machine-readable result is
[`results/phase4_recovery_result.json`](results/phase4_recovery_result.json). It contains
no Queue IDs, session IDs, transfer URLs, or browser state.

Running the scenarios found **six real defects**. All are fixed and covered by tests
(see [Defects found and fixed](#defects-found-and-fixed)). The first full attempt at
1,000 sessions failed on two of them: hung workers and leaked contexts.

## Population

| Group | Sessions | Setup |
|---|---:|---|
| Expired leases (crashed owner) | 200 | lease expired 1 s before start |
| Live leases (another owner) | 200 | lease valid for 2 h; must never be stolen |
| Healthy due | 4,798 | PRE_QUEUE / ACTIVE_QUEUE / PARKED / SERVICED_SOON mix |
| Fault groups | 170 | see scenario table |
| Future (parked) | 4,232 | not due; 1,000 reactivated for the worker-crash scenario |
| ADMITTED / EXPIRED | 200 / 100 | terminal; must be preserved |
| Failed creation (no Queue ID) | 100 | terminal audit rows |
| **Total / valid Queue IDs** | **10,000 / 9,900** | |

Seeding 10,000 rows with a new bulk `create_many` transaction plus 9,900 state files
took 1.49 s. The full run took 563 s.

## Scenario Results

| # | Scenario | Outcome | Key evidence |
|---:|---|:---:|---|
| 1 | Shutdown during creation | **PASS** | Forced cancel with 10 creations in flight took 0.040 s; 0 contexts left; 30 committed IDs survived; resume created exactly the 70 remaining; 0 orphaned or missing state files |
| 2 | Shutdown during monitoring | **PASS** | 20 in-flight + 48 queued at shutdown; forced cancel after 0.5 s; scheduler stopped in 0.616 s, Chrome in 0.086 s; 0 contexts, 0 Chrome processes, 0 leases left |
| 3 | Restart with 10,000 persisted sessions | **PASS** | 5 reopen cycles, identity digest and terminal states unchanged; startup p50/p95 6.5/6.9 ms; first 50-row claim p95 7.4 ms |
| 3b | Restart and resume monitoring | **PASS** | 4,864 due after restart; 4,934 browser checks in 394 s (12.5 checks/s); backlog drained to 0 |
| 4–5 | Chrome process crash, repeated (5 × SIGKILL) | **PASS** | 5/5 detected and replaced; restart p50/p95/max 1.16/1.49/1.53 s; 16 contexts lost; the healthy process kept serving; monitoring completed |
| 6 | Context creation failure | **PASS** | 20 sessions whose stored state Chrome rejects: 63 real `new_context` failures; all `CONNECTION_LOST`, Queue IDs unchanged |
| 7 | Navigation failure / timeout | **PASS** | 40 empty-response + 10 slow sessions; 60 navigation timeouts; all `CONNECTION_LOST` (retryable), Queue IDs unchanged |
| 8 | Transfer restore failure | **PASS** | 40 sessions with transfer returning 503 all recovered via `storage_state` fallback with verified identity |
| 9 | `storage_state` restore failure | **PASS** | 20 corrupt files → `FAILED` (permanent, observable); 20 unreadable files → `CONNECTION_LOST` (retryable) |
| 10 | Identity mismatch | **PASS** | 20/20 detected and counted; `FAILED` with the **expected** Queue ID retained; no replacement created |
| 11 | Database connection interruption | **PASS** | Handle severed + 3 s outage (215 injected failures); scheduler survived 6 failed ticks, reconnected once, resumed in 0.109 s; 70 stranded leases re-claimed after expiry |
| 12 | State-storage interruption | **PASS** | 3 s save/load outage: 31 save and 60 load failures; no session failed; observations kept (`STATE_REFRESH_FAILED` without extra navigation) |
| 13 | Expired leases | **PASS** | 200/200 recovered in 0.137 s through bounded claims; 200 live leases untouched |
| 14 | Worker crash | **PASS (single machine)** | Separate process with Chrome `SIGKILL`ed after 150 checks: 28 leases held (bound ≤ 30), `PRAGMA integrity_check` ok, **0 orphan Chrome processes** |
| 15 | Worker restart | **PASS (single machine)** | A restarted worker recovered all 28 leases 10.16 s after the kill (lease = 10 s) and finished the other 850 checks |
| — | Distributed worker crash / node restart | **UNKNOWN** | Not applicable: Phase 4 is single-machine ([decision](phase4_distributed_worker_decision.md)); no multi-node system exists |
| — | Real Queue-it recovery | **UNKNOWN** | No authorised staging configuration |

## Invariants Verified Across the Run

- **Queue IDs unchanged.** A digest over `session_id`, `queue_id`, and `transfer_url`
  for all 10,000 original rows was identical before and after every scenario.
- **Successful count survives restart.** 9,900 valid IDs survived every reopen. After the
  sweep there were 9,860: exactly the 40 deliberate permanent failures fewer.
- **Target acquisition resumes correctly.** The replacement limit was 0, and 40
  identities had been lost. The controller therefore targeted 9,960 rather than 10,000,
  created only the 100 identities never acquired, stopped, and set
  `queue_identity_replacement_blocked = 1`. There was no overshoot.
- **No mass replacement.** `FAILED` rows carrying a Queue ID were exactly the 20 corrupt
  and 20 mismatch sessions. Chrome crashes, database and state outages, and navigation
  failures failed no identity.
- **Terminal states preserved.** All 200 ADMITTED, 100 EXPIRED, and 100 failed-creation
  rows were unchanged.
- **Failed sessions stay observable.** Each failure keeps a sanitized `last_error`
  (`restore:STATE_CORRUPT`, `restore:IDENTITY_MISMATCH`, …) and is counted in
  Prometheus.
- **Auditable.** The full state audit of 9,900 files took 0.39 s. Its only findings were
  the 20 deliberately corrupted files.
- **No context/lease leaks.** After every shutdown, active contexts, Chrome processes, and
  scheduler-owned leases were zero. Only the 200 untouched live leases remained.

## Recovery Characteristics

| Measurement | Value |
|---|---:|
| Startup recovery (initialize + aggregate summary), p95 | 6.9 ms |
| Startup to first 50-row claim, p95 | 7.4 ms |
| Chrome start (2 processes) | 0.53 s |
| Due backlog after restart | 4,864 |
| Sessions resumed with real browser checks | 4,934 |
| Resume sweep throughput (20 workers, local simulator) | 12.5 checks/s |
| Average check / restore duration (20-way concurrency) | 1.57 / 1.52 s |
| Sequential single restore (profiled) | ≈ 0.15 s |
| Chrome process restart p50 / max | 1.16 / 1.53 s |
| Expired leases recovered (200) | 0.137 s |
| Killed worker's leases recovered, from kill | 10.16 s (lease 10 s) |
| Database: first successful schedule after outage | 0.109 s |
| Full 9,900-file state audit | 0.39 s |

With a hard kill, recovery is bounded by `MONITOR_LEASE_SECONDS`, which defaults to
120 s. The harness uses 10–15 s leases. The 12.5 checks/s rate is local simulator
throughput on this host, not a Queue-it check rate.

## Defects Found and Fixed

1. **Hung browser calls.** After a Chrome process was killed, `BrowserContext.new_page()`
   could stay pending forever. This wedged workers and their leases. Restorer and creator
   attempts now run under deadlines. BrowserManager bounds `new_context` (called while
   holding its global lock), context/browser close, and restart cleanup. A new counter
   records these: `browser_operation_timeouts_total`.
2. **Context capacity leak on forced shutdown.** `close_context` waited for the allocation
   lock before discarding a context. A cancellation arriving during that wait leaked the
   context permanently: 10 of 50 contexts in every forced-shutdown repro. Capacity is now
   released synchronously, and the Chrome close is shielded and bounded.
3. **Transient state outage became a permanent failure.** An unreadable state file (an
   I/O or permission error) was classified as `STATE_CORRUPT`, which marks the session
   `FAILED`. That removed the identity from the valid count and let creation replace it.
   It is now the retryable `STATE_UNAVAILABLE`.
4. **Refresh failure discarded a verified observation.** When a verified transfer restore
   failed only while saving refreshed state, HYBRID fell back to a second `storage_state`
   navigation, and the monitor retried both. That tripled browser load during a storage
   outage. The observation is now kept, with no fallback and no retry. The failure is
   counted separately (`state_refresh_failures_total`), not as a transfer failure.
5. **SQLite and state writes under cancellation.** A cancelled repository operation
   released the connection lock while its thread still used the shared connection. A
   cancelled state save could leave an unreferenced file. A cancelled create could also
   commit and then have its state deleted, which would strand a valid identity. All three
   now finish their thread work before propagating cancellation, and creation cleanup
   checks whether the row was committed.
6. **Scheduler and shutdown did not survive database errors.** A single failed
   `schedule_due` ended the monitoring loop, and a failed lease release aborted shutdown
   before Chrome was closed. The loop now backs off and continues. Every shutdown step is
   isolated. A broken SQLite handle is discarded and reopened; lock contention does not
   trigger a reconnect.

Also added:

- **Mass-replacement guard:** `IDENTITY_REPLACEMENT_LIMIT` (default 0).
- **Owner-aware lease recovery accounting:** a scheduler re-claiming its own expired
  lease is counted separately from a takeover from a stopped owner.

## Observability

- New aggregate gauges and counters:
  - Target and population: `queue_ids_valid`, `queue_ids_remaining`, `queue_ids_lost`,
    `queue_sessions_persisted`, and lifecycle gauges for every status, including
    `connection_lost`, `paused`, `checking`, and `new`.
  - Leases: `monitoring_leases_active`, `monitoring_leases_expired`,
    `monitoring_lease_recoveries_total`.
  - Throughput: `monitoring_checks_per_second`,
    `monitoring_check_duration_average_seconds`, worker configuration gauges.
  - Failures: `repository_errors_total{operation}`, `state_refresh_failures_total`,
    `browser_restart_duration_seconds`, `browser_restart_failures_total`,
    `browser_contexts_lost_total`, `browser_operation_timeouts_total`.
  - Startup: `startup_recovery_duration_seconds`, `startup_expired_leases`,
    `startup_due_backlog`.
  - Acquisition: `queue_identity_replacement_blocked`.
- Progress distribution is exposed as `queue_sessions_progress_bucket{bucket}`, with
  seven fixed ranges from one grouped SQL query. No per-session series exists.
- Cardinality at 10,000 sessions: 242 series. The only labels are `bucket`, `operation`,
  and `le`. No Queue ID, session ID, or transfer URL appeared in the exposition. A unit
  test proves the series count is identical for 5 and 3,000 sessions.
- `/status` now shows every requested dashboard field, from target/acquired/remaining
  through progress buckets. `/metrics` keeps serving during a database outage.
- A Grafana dashboard definition is in
  [`dashboards/queue_load_test_phase4.json`](dashboards/queue_load_test_phase4.json).
  A test checks that every expression references an exported metric. No Grafana or
  Prometheus stack is deployed.
- Structured logs:
  - Every absolute URL is redacted in messages and field values, including third-party
    messages.
  - Each line has a per-run `run_id`. New aggregate fields: `operation`, `count`,
    `recovered_leases`, `lost_contexts`, `lost_queue_ids`.
  - The 10,000-session run wrote 16,496 lines (4.97 MB). No line contained a transfer URL
    or the local host address. Queue IDs remain a deliberate correlation field.

## Reproduce

```powershell
python -m pip install -e ".[test,benchmark]"
queue-load-test-phase4-recovery --report phase4-recovery-benchmark.json
```

It creates a temporary work directory unless `--work-directory` names an empty one. It
never reads `.env` and sends no network traffic beyond `127.0.0.1`.

## Unresolved Reliability Risks

- **Real Queue-it behavior is UNKNOWN.** Transfer continuity after a crash or delay,
  real `storage_state` sizes, restore rates, and real mismatch incidence were not
  observed.
- A HYBRID navigation failure is reported as `STATE_CONTEXT_FAILED` once the storage
  fallback also fails. The distinction is visible only through the
  `navigation_failures_total` and `browser_context_creation_failures_total` metrics.
- Hard-kill recovery waits for lease expiry: up to 120 s at the default
  `MONITOR_LEASE_SECONDS`. At the measured 12.5 checks/s, 10,000 browser checks would
  take about 13 minutes on this host. Live cadence sufficiency remains UNKNOWN.
- If an attempt deadline fires exactly while a successful commit finishes, the retry
  reuses the same `session_id` and fails on the primary key. This is observable, not
  silent, but it wastes one attempt.
- Distributed ownership, node loss, and PostgreSQL behavior are not implemented or
  tested.
- Power loss, disk exhaustion, and filesystem corruption were not exercised.
- Log volume is about 3.3 lines per check (≈ 500 bytes/line). A 10,000-session sweep
  produces about 5 MB. Sink backpressure is unmeasured.
