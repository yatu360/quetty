# Phase 4 Readiness and Capacity Model

**Date:** 2026-09-26

**Target under study:** `TARGET_QUEUE_IDS=10000`

**Decision:** `UNKNOWN` for a 10,000-session run; Phase 4 configuration is validated,
but the required browser-backed baseline is absent.

This is a planning model for 10,000 **persisted** identities with a small bounded set
of active Chrome contexts. It does not call for 10,000 contexts or Chrome processes.
The source of truth is [Phase 3 acceptance](phase3-acceptance.md) and the current
repository. Phase 3 had no authorised Queue-it staging acquisition, restoration,
monitoring, or restart run.

## 1. Phase 3 Measured Baseline

| Workload | Configuration and measurement | Scope |
|---|---|---|
| Acquisition | Exactly 1,000 synthetic unique IDs with 20 fixed workers and a 20-item queue; mixed scripted case: 1,005 attempts, 1,000 successes, one duplicate, two failed outcomes, two retries | Correctness only; real creation duration, sessions/s, and active-context peak `UNKNOWN` |
| Monitoring | 1,000 due rows, 20 fixed workers, 50-item queue, 50-row claims, 1 ms synthetic handler delay; 944.98/947.26 checks/s, 1.0582/1.0557 s full sweeps, 11.05/11.38 ms average handler duration | Browser-free SQLite/scheduler/lease path; real checks/s and sweep duration `UNKNOWN` |
| Backlog and jitter | Both 1,000-row backlogs drained to zero; queue peak 50, worker peak 20, lease conflicts zero. Staggered pass had 1,000 distinct due times over about 10 s, at most 128 in one one-second bucket | Local simulated scheduling only |
| SQLite | 1,000 rows; due count p50/p95 0.098/0.126 ms, claim 50 p50/p95 0.555/0.673 ms, update 0.365/0.386 ms, release 0.279/2.321 ms; indexed query without temporary sort | One local host, 1,000-row workload |
| State storage | 1,000 fixed 1,249-byte JSON files; save/load/replace p95 0.263/0.063/0.291 ms; 417,792-byte SQLite DB and 1,249,000-byte state directory | Synthetic fixed-size state, sequential file operations |
| Chrome | 50/75/100 contexts on 2/3/4 installed Chrome processes, 25 per process; two-second holds against an in-memory page, zero recorded crashes, navigation or context failures | Sustained Queue-it reliability `UNKNOWN` |
| Resources | Synthetic monitoring app CPU 85.21% average / 114.3% peak, RSS 59.75/60.88 MB. Browser-case Chrome CPU average 153.1/206.5/293.2%; summed RSS peak 17.80/22.14/27.92 GB at 50/75/100 | Chrome RSS can double-count shared pages; 75 and 100 crossed the conservative 80% host-RAM indicator |
| Restore | No real transfer, `storage_state`, or HYBRID fallback success/failure rates | `UNKNOWN` |
| Recovery | Five local restarts kept 1,000 rows, 960 unique IDs and their state associations; startup summary p95 0.907 ms; 50 expired leases recovered while 50 active leases stayed owned | Synthetic, single-host recovery; real Queue-it/Chrome continuity `UNKNOWN` |

The default runtime profile remains `TARGET_QUEUE_IDS=1000`, two Chrome processes,
25 contexts per process, 50 global contexts, one creation worker, and one monitoring
worker. The 20-worker synthetic sweep is a benchmark configuration, not that default.

## 2. Estimated 10,000-Session Creation Duration

For a continuously busy bounded creation pool, the ideal rate is:

`ideal unique IDs/s ≈ concurrent creation contexts / average successful creation duration (s)`

For 10,000 new successful IDs, the corresponding ideal time would be:

`10,000 × average successful creation duration / concurrent creation contexts`.

The successful duration must include context acquisition, network/navigation, Queue-it
JavaScript, official transfer extraction, state save, SQLite commit, cleanup, and the
cost of retries and duplicates needed to obtain one *unique* ID. The real Phase 3
average and achieved unique IDs/s were never measured. Thus the Phase 4 creation
duration is **`UNKNOWN`**, with no numeric estimate justified by measured Phase 3
creation values. The short in-memory navigation p95 is not a creation duration.

For capacity planning, let `d_create` be the future observed seconds per successful
unique ID and `c_create` the concurrently busy creation contexts. The ideal expression
is `10,000 × d_create / c_create`; measured utilization and failures can only increase
the wall time. With the current one-worker default, `c_create ≤ 1`; even the tested
50-context ceiling does not make `c_create = 50`. Creation and monitoring workers share
the 50-context ceiling, so any future concurrent profile must budget both.

## 3. Estimated 10,000-Session Monitoring Sweep

The ideal context-rate relation is:

`ideal checks/s ≈ concurrent monitoring contexts / average end-to-end check duration (s)`

For a browser-backed sweep, that duration must include context wait, transfer or state
restore, navigation, live DOM inspection, state refresh, persistence, lease release,
and retries. Neither its average nor an observed browser-backed checks/s exists, so
the real 10,000-session sweep duration is **`UNKNOWN`**.

The measured synthetic sweep illustrates only the scheduler/SQLite component. For 20
busy workers and the measured 11.05/11.38 ms average synthetic handler duration, the
ideal worker formula gives about 1,810/1,758 checks/s. Actual measured end-to-end
synthetic throughput was 944.98/947.26 checks/s. If that measured rate remained
unchanged at 10,000 rows, `10,000 / measured checks/s` would be **10.58/10.56 s**.
This is a linear theoretical projection, not a 10,000-row measurement. With an explicit
50% planning utilization assumption, the same synthetic calculation becomes
**21.16/21.11 s**. The 50% factor is a scenario margin, not a measured safe operating
margin, and cannot turn the result into a real Queue-it sweep estimate.

Cadence depends on lifecycle mix. Illustrative demand for 10,000 sessions is 333
checks/s if all are due every 30 s, 167 checks/s every 60 s, or 33 checks/s every
300 s. These are workload requirements, not achieved rates. The configured adaptive
intervals vary by lifecycle state, and `TURN_STARTED`/`READY` may be due immediately;
an all-due burst still requires a measured backlog drain and fairness budget. Queue
depth is bounded, so any excess stays visible as due backlog rather than becoming
unbounded work.

The planning helpers in `queue_load_test.capacity` calculate the ideal context rate
and rate-based duration with an explicit utilization input. They reject zero,
negative, and non-finite inputs. They do not produce a browser estimate when the
required measured duration or rate is missing.

## 4. Known Resource Constraints and Bottleneck Audit

| Component | Assessment | Evidence and Phase 4 implication |
|---|:---:|---|
| Chrome capacity | UNKNOWN | 50/75/100 contexts completed short local cases; sustained Queue-it stability is unmeasured. 50 was the least resource-pressured case, not an approved operating point. |
| CPU | UNKNOWN | Browser-case average Chrome CPU rose from 153.1% to 293.2% across 50–100 contexts; no real workload or sustained headroom measurement. |
| RAM | FAIL at 75/100 pressure gate | Summed peak app-plus-Chrome RSS crossed the benchmark's conservative 80% host-RAM indicator at 75 and 100; shared pages may be double-counted. |
| SQLite writes and contention | UNKNOWN | Fast at 1,000 rows, but one writer, one async lock per repository, and individual create/update/release commits remain. Concurrent 10,000-row load is unmeasured. |
| Scheduler queries | PASS at 1,000; UNKNOWN at 10,000 | Partial ordered index and sub-ms due count/claim at 1,000. Query cost and due-count frequency at 10,000 need measurement. |
| Lease handling | PASS at 1,000; UNKNOWN at 10,000 | Two connections claimed disjoint rows; 50 expired leases recovered. Multi-process contention and long real checks remain untested. |
| Filesystem state operations | UNKNOWN | Sequential synthetic save/load/replace was fast; actual state sizes, concurrent refresh/fsync, and 10,000-file scans are unmeasured. |
| Monitoring cadence | UNKNOWN | 1,000-row synthetic backlog drained. Real check rate, lifecycle mix, and update cadence were not observed. |
| Restoration latency | UNKNOWN | Neither official transfer nor state fallback success/latency was measured. |
| Network latency | UNKNOWN | In-memory browser navigation provides no network baseline. |
| Queue-it page execution | UNKNOWN | No Phase 3 Queue-it page timing or crash/failure sample. |
| Akamai processing | UNKNOWN | No separate observable timing or failure measurement; do not infer an internal processing rate. |
| Logging volume | UNKNOWN | Per-session structured events can scale with attempts/checks; sink throughput and backpressure were not measured. |
| Metrics cardinality | PASS for current schema | Prometheus labels are aggregate/low-cardinality with no session or Queue ID labels. Scrape/retention cost at Phase 4 rate remains unmeasured. |

## 5. Persistence and Scheduler Assessment

SQLite is adequate for the *measured* single-host Phase 3 population (`PASS`). The
partial due index, short transactional claim, bounded queue, and aggregate startup
summary avoid a task or hot-path directory scan per persisted row. `StateConsistencyChecker`
does perform an explicit full scan, which took 61.355 ms for 1,000 fixture files after
restart; it is not in normal startup. The local state store uses fsync and atomic
replacement for each HYBRID save.

Linear size arithmetic would suggest roughly 4.18 MB of database and 12.49 MB of
state JSON for 10,000 *same-size synthetic rows*. Those are illustrative byte counts,
not a measured Phase 4 disk forecast: real state files may be larger, indexes and
SQLite journaling add space, and updates cause churn. At a 30-second all-session
cadence, about 333 completed checks/s would imply at least 333 session updates and
333 lease releases/s, plus claims and possible state writes. This is a demand model;
no corresponding sustained SQLite write benchmark has run.

## 6. Browser Capacity and Single-Machine Viability

The current configuration cap is four Chrome processes, 25 contexts per process, and
100 global contexts. It bounds resources but does not establish a safe 100-context
operating point. The browser manager's global lock covers `new_context()`, which may
serialize acquisitions as worker count rises. The Phase 3 p95 acquire/lock-wait times
at 50 contexts were 0.384/0.320 s in the short local run. The 75/100 cases also showed
RAM pressure. Phase 4 should start with the least resource-pressured 50-context
candidate only after real authorisation and baseline evidence; its sustained safe
capacity is still `UNKNOWN`.

One machine handled the 1,000-row synthetic scheduler and local storage workloads.
Whether one machine can acquire and monitor 10,000 real Queue-it identities within a
defined cadence is **`UNKNOWN`**. A decision needs a real end-to-end creation rate,
check duration/rate, active-context peak, CPU/RAM trajectory, restore reliability,
failure incidence, and backlog recovery over a sustained interval on the intended
host.

## 7. PostgreSQL Decision Gate

**Current result: `UNKNOWN` for Phase 4 migration need; no measured trigger to migrate
now.** Investigate PostgreSQL in Phase 4 Prompt 2 if a measured 10,000-row or sustained
browser-backed workload shows that indexed due queries, claim/update/release latency,
write contention, lease correctness, or recovery time misses the defined cadence or
failure budget. Multi-node ownership would also require a deliberate backend and
leasing design. Keep the `SessionRepository` boundary; do not migrate on a tenfold
population assumption alone.

## 8. Shared State and Distributed-Worker Decision Gates

**Shared/object state storage: `UNKNOWN` need.** Local state succeeded for 1,000
synthetic files. Investigate shared storage if measured file size/churn, recovery
objectives, or a validated multi-node design cannot be met with local files. Its
atomicity, permissions, and identity association would need explicit tests.

**Distributed workers: `UNKNOWN` need.** Add them only after real measurements show
one machine cannot meet the required acquisition window, monitoring cadence, or
recovery objective. Multiple creation controllers currently lack a transactional
shared target reservation; SQLite claims provide local atomic leasing but are not a
complete distributed ownership protocol. Prove these prerequisites before any
multi-node run.

## 9. Configuration and Readiness Gates

`TARGET_QUEUE_IDS=10000` validates through the existing `Settings` type. The
current `.env.example` deliberately stays at 1,000. The configurable knobs are
`CHROME_PROCESS_COUNT`, `MAX_CONTEXTS_PER_BROWSER`, `MAX_ACTIVE_CONTEXTS`,
`CREATION_WORKERS`, `CREATION_QUEUE_CAPACITY`, `MONITOR_WORKERS`,
`MONITOR_CLAIM_BATCH_SIZE` (scheduler batch size), `MONITOR_QUEUE_CAPACITY` (monitor
queue size), and `MONITOR_LEASE_SECONDS` (lease length). Configuration validation
still enforces aggregate browser capacity, workers within the global context limit,
bounded queues, and claim batches no larger than the monitoring queue. A validated
10,000-target configuration was tested with 2 Chrome processes, 25 contexts each,
50 global contexts, 10 creation workers, 20 monitor workers, 10 creation queue slots,
50 monitor queue slots, 50-row claims, and 180-second leases. It was **validation
only**; no Chrome process or staging session was started.

| Gate before a 10,000-session target run | Status | Required evidence |
|---|:---:|---|
| Bounded 10,000-target configuration | PASS | Validated existing settings and resource contradictions. |
| Real unique-ID creation throughput and identity integrity | UNKNOWN | Authorised measured acquisition, including retries and duration. |
| Real restore/check throughput and cadence | UNKNOWN | End-to-end browser-backed checks, transfer/state outcomes, sweep/backlog data. |
| Safe sustained browser profile | UNKNOWN | Real pages, duration, memory/CPU headroom, crashes, navigation failures. |
| 10,000-row database and state performance | UNKNOWN | Dedicated non-staging load plus real state-size/churn data. |
| Process recovery and lease catch-up | UNKNOWN | Real restart/interrupt continuity and recovery against the target cadence. |
| One machine sufficient | UNKNOWN | Compare measured sustained capacity with agreed cadence and recovery objective. |
| PostgreSQL required | UNKNOWN | Trigger only on measured database or ownership limitation. |
| Distributed workers required | UNKNOWN | Trigger only on measured single-host shortfall. |

The next task is **Phase 4 Prompt 2 — PostgreSQL and Leasing Readiness**. This prompt
does not start the 10,000-session staging run.
