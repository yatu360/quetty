# Phase 4 Acceptance Report

**Date:** 2026-09-27  
**Target:** `TARGET_QUEUE_IDS=10000`  
**Overall result:** **PARTIAL — 24 PASS, 0 FAIL, 16 UNKNOWN**  
**Authorised 10,000-session Queue-it acquisition:** **NOT RUN / UNKNOWN**

## Executive Summary

Phase 4 proves that the single-machine implementation can persist and schedule 10,000
synthetic session rows while keeping browser work bounded. SQLite, local atomic JSON
state, bounded claims, fixed workers, lease recovery, restart behavior, installed-
Chrome crash recovery, and identity-preserving failure handling all have controlled
local evidence.

It does **not** prove that 10,000 unique Queue-it identities were acquired. No
authorised staging configuration or real 10,000-session population existed, so real
creation throughput, total acquisition time, duplicate/failure incidence, transfer and
`storage_state` reliability, live lifecycle behavior, sustained Chrome resources, and
the useful Queue-it monitoring cadence remain UNKNOWN.

The strongest scale results are:

- two synthetic all-due 10,000-row sweeps completed in 76.595 and 92.241 seconds at
  130.56 and 108.41 checks/s;
- an adaptive synthetic schedule peaked at 2,885 due rows and 26.599 seconds oldest
  overdue, then drained to zero;
- a separate installed-Chrome/local-simulator recovery run resumed 4,934 browser checks
  in 394.224 seconds at 12.52 checks/s with a peak of 20 contexts;
- all 15 controlled recovery scenarios passed, including real process kills, database
  and state interruptions, repeated Chrome replacement, and lease-expiry takeover.

These measurements establish the application mechanics, not Queue-it service behavior.

## Final Architecture

The final implementation remains single-machine:

```text
Creation controller / due-session scheduler
                    |
                    v
          local SQLite repository
           (identity + progress + leases)
                    |
                    v
       bounded local asyncio work queues
                    |
                    v
             fixed local workers
                    |
                    v
              BrowserManager
                    |
                    v
       at most 2 installed Chrome processes
                    |
                    v
      at most 50 isolated BrowserContexts
                    |
                    v
       authorised Queue-it staging (not run)
```

Browser state is separate from SQLite and uses `FileSystemStateStore`:

```text
HYBRID QueueSession
       |
       +-- official transfer URL in SQLite
       +-- atomic local JSON storage_state file
```

There is no PostgreSQL backend, object store, message broker, or distributed worker
deployment. Persisted sessions remain parked; only bounded due work enters the browser
pool.

## Tested Configuration

The Phase 4 harnesses used related but distinct profiles:

| Setting | Phase 4 bounded profile | Actually exercised |
|---|---:|---:|
| `TARGET_QUEUE_IDS` | 10,000 | 10,000 synthetic rows; real target NOT RUN |
| `SESSION_MODE` | HYBRID | HYBRID in recovery/storage; synthetic monitor used synthetic transfer-only rows |
| `CHROME_PROCESS_COUNT` | 2 | 2 in recovery/local-simulator run |
| `MAX_CONTEXTS_PER_BROWSER` | 25 | 25 configured |
| `MAX_ACTIVE_CONTEXTS` | 50 | 20 observed peak in recovery run |
| `CREATION_WORKERS` | 10 | 10 in recovery interruption/resume scenario |
| Creation queue | 10 | 10 |
| `MONITOR_WORKERS` | 20 | 20 |
| Monitoring queue | 50 | 50 observed peak |
| Scheduler claim batch | 50 | 50 |
| Normal configured lease | 120 s | 15 s recovery profile; 10 s killed-worker scenario |
| Database | SQLite | SQLite |
| State backend | local JSON filesystem | `FileSystemStateStore` |
| Distributed workers | none | none |

The checked-in `.env.example` remains a conservative 1,000-target default with one
creation and one monitoring worker. The table above is the demonstrated Phase 4
harness profile, not a change to that default and not a universal optimum.

## 10,000-Session Population

No real Queue-it 10,000-identity population was created.

Two controlled populations were used:

1. The monitoring benchmark persisted 10,000 synthetic unique IDs and processed all
   10,000 twice. These were generated test identities, not Queue-it IDs.
2. The recovery benchmark persisted 10,000 rows: 9,900 initially carried synthetic
   unique IDs and 100 were intentional failed-creation audit rows without an ID. After
   deliberate corruption and identity-mismatch scenarios, the successful-ID count was
   9,860 because 40 identity-bearing rows were intentionally terminal `FAILED`; their
   expected IDs remained stored and were not replaced.

The recovery population's final status distribution was:

| Status | Count |
|---|---:|
| PRE_QUEUE | 2,367 |
| ACTIVE_QUEUE | 3,627 |
| PARKED | 1,108 |
| SERVICED_SOON | 2,368 |
| ADMITTED | 200 |
| CONNECTION_LOST | 90 |
| EXPIRED | 100 |
| FAILED | 140 |
| **Total** | **10,000** |

These are local simulator/synthetic states. They are not evidence that Queue-it
produced this distribution.

## Creation Results

The authorised target acquisition did not run. Therefore the following real results
are UNKNOWN:

- final unique Queue-it ID count;
- total attempts, successes, duplicates, transient/permanent failures, and retries;
- total acquisition duration and sessions/s;
- creation p50/p95 latency; and
- sustained acquisition CPU/RAM and Chrome stability.

The controlled recovery scenario interrupted local-simulator creation with ten items in
flight. Thirty IDs committed before interruption; restart created exactly the remaining
70 in 5.052 seconds, for a derived local-simulator resume rate of about 13.86/s. The
metrics recorded 110 attempts across cancellation and resume. This validates bounded
resume semantics but is not the Phase 4 acquisition result.

## Monitoring Results

### Synthetic scheduler and SQLite

| Metric | Sweep 1 | Sweep 2 |
|---|---:|---:|
| Sessions checked | 10,000 | 10,000 |
| Failures / lease conflicts | 0 / 0 | 0 / 0 |
| Full sweep duration | 76.595 s | 92.241 s |
| Throughput | 130.56/s | 108.41/s |
| Average check duration | 79.03 ms | 94.93 ms |
| Check p50 / p95 | 75.20 / 106.90 ms | 92.05 / 124.63 ms |
| Queue / worker peak | 50 / 20 | 50 / 20 |
| Backlog start -> end | 10,000 -> 0 | 10,000 -> 0 |

The adaptive synthetic scenario processed 10,000/10,000 at 107.31 processing checks/s.
Its due-time window was 184.996 seconds, maximum backlog was 2,885, maximum oldest-
overdue age was 26.599 seconds, and ending backlog/age were zero. It had 9,801 distinct
exact due times and a largest one-second bucket of 439.

### Installed Chrome and local simulator

After restart, 4,934 due sessions were browser-checked in 394.224 seconds at 12.52
checks/s. Average check and restore duration were 1.572 and 1.518 seconds. Active
contexts peaked at 20, the queue at 50, and the backlog drained to zero.

This is the best browser-backed application measurement, but the page was the local
simulator rather than Queue-it and it was not a complete 10,000-session browser sweep.
The sustainable real monitoring cadence remains UNKNOWN.

## Restore Reliability

No representative transfer or storage-state reliability sample was run against
Queue-it, so success rates are UNKNOWN.

Controlled fault evidence exists:

- 40 sessions whose simulated transfer returned 503 all recovered through the
  `storage_state` fallback with matching identities;
- 20 corrupt state files became explicit permanent `FAILED` rows;
- 20 unavailable state files became retryable `CONNECTION_LOST` rows;
- 20/20 deliberately injected identity mismatches were detected, the expected ID was
  retained, and no replacement was created.

The recovery harness accumulated 368 transfer-restore failure attempts and 310 state-
restore failure attempts (678 total). These are retry/fault-injection counters, not
natural reliability rates and must not be presented as such.

## Browser Stability

- Five deliberate Chrome `SIGKILL` events were detected and the affected process slot
  was replaced 5/5 times. Restart p50/p95/max was 1.16/1.49/1.53 seconds.
- Sixteen active contexts were lost across those kills. The healthy Chrome process
  continued serving work.
- Twenty invalid stored-state sessions caused 63 real `new_context` failures and
  remained retryable `CONNECTION_LOST` with IDs unchanged.
- The targeted navigation scenario produced 60 timeouts across 50 sessions. The full
  fault-injection run recorded 483 navigation failures cumulatively, including failures
  resulting from Chrome kills and retries.
- Shutdown returned active contexts, managed Chrome processes, and scheduler-owned
  leases to zero.

These are installed-Chrome/local-simulator observations. Natural Queue-it crash,
context, and navigation failure counts remain UNKNOWN.

## Persistence

### Database

SQLite was the final backend. PostgreSQL was not implemented because no measured
single-host database bottleneck or multi-node requirement justified migration.

The isolated 10,000-row benchmark produced a 4,386,816-byte database. On macOS, due
count p95 was 0.999 ms, claim-50 p95 0.691 ms, update p95 0.390 ms, release p95 0.317
ms, and scheduler-iteration p95 1.667 ms. A Windows/NTFS recheck measured 4.601,
4.898, 3.971, 2.973, and 9.263 ms respectively in its slower second run. Both used the
ordered due index without a temporary sort.

The all-due sweep's end-to-end repository-call latency was higher because calls waited
behind concurrent durable writes; this does not contradict the isolated benchmark.
Sustained real Queue-it write contention remains UNKNOWN.

### State storage

`FileSystemStateStore` remained the backend. At 10,000 synthetic files:

- logical data was 14,160,000 bytes; allocated disk was 40,960,000 bytes;
- save p50/p95 was 0.179/0.252-0.253 ms;
- load p50/p95 was 0.062-0.063/0.075-0.076 ms;
- twenty-way saves measured about 7,600/s;
- full consistency audits took about 0.41-0.45 seconds and found zero issues in clean
  runs.

The recovery run audited 9,900 files in 0.393 seconds. Its only final findings were the
20 deliberately corrupted files. State envelopes detected mismatched identities and
digest corruption. Real Playwright state size and churn remain UNKNOWN.

## Resource Usage

The 10,000-row synthetic monitoring run on Windows measured the Python process at:

- CPU average/peak: **59.00% / 81.8%**;
- RSS average/peak: **68.08 MB / 76.54 MB** (64.93/73.00 MiB);
- active-context peak: 0 because this run used a synthetic handler.

The recovery run used two installed Chrome processes and peaked at 20 contexts, but no
comparable aggregate Chrome CPU/RAM result was recorded. Open file descriptors were not
reported on the Windows monitoring host. Sustained real-page CPU/RAM and descriptor
growth remain UNKNOWN.

## Distribution

Final execution was single-machine. Distribution was deferred because measurements did
not reveal a SQLite or local-state bottleneck and no authorised single-host Queue-it run
demonstrated a cadence shortfall. This is an evidence-based decision not to add
complexity; it is **not** proof that one host meets an undefined live cadence.

There were no distributed workers, PostgreSQL `SKIP LOCKED` claims, shared state store,
or node-loss test. Distributed failure recovery is UNKNOWN. Local separate-process
worker crash/restart did succeed through SQLite lease expiry.

## Failure Recovery

- Five repository reopen cycles preserved all 10,000 rows, the identity digest, and
  terminal states. Startup summary p95 was 6.9 ms and first bounded claim p95 7.4 ms.
- Interrupted local-simulator creation preserved 30 committed IDs and created only the
  70-ID deficit after restart, without overshoot.
- Forced monitoring shutdown handled 20 active plus 48 queued items and left zero
  contexts, Chrome processes, or scheduler-owned leases.
- All 200 expired leases were recovered in 0.137 seconds; 200 live leases were not
  stolen.
- A killed worker held 28 leases. A new worker recovered all of them 10.157 seconds
  after the kill with a ten-second lease, then completed 850 checks.
- A three-second database outage caused 215 injected failures; the scheduler survived,
  reconnected, and resumed 0.109 seconds after the outage ended.
- A three-second state outage caused 31 save and 60 load failures without failing an
  identity; verified observations survived refresh failures.
- Five Chrome kills were recovered without orphan processes. Queue IDs stayed unchanged.

Real Queue-it restart, transfer continuity after a delay, node loss, power loss, disk
exhaustion, and filesystem corruption remain UNKNOWN.

## Acceptance Matrix

| # | Requirement | Status | Evidence | Notes |
|---:|---|:---:|---|---|
| 1 | 10,000 unique Queue-it IDs persisted | UNKNOWN | Real acquisition NOT RUN | 10,000 synthetic IDs and 9,900 recovery IDs are not Queue-it evidence. |
| 2 | BrowserContexts remained bounded | PASS | Recovery peak 20; configured ceiling 50 | Shutdown returned to zero. |
| 3 | Chrome processes remained bounded | PASS | Two managed processes | Five killed slots were replaced, not multiplied. |
| 4 | One Chrome process per visitor avoided | PASS | 10,000 persisted rows; two Chrome processes | Parked architecture retained. |
| 5 | Real creation throughput measured | UNKNOWN | Staging acquisition NOT RUN | Local-simulator resume derived about 13.86/s only. |
| 6 | Real 10,000 acquisition duration measured | UNKNOWN | Staging acquisition NOT RUN | No final acquisition report exists. |
| 7 | Real creation failure/retry count measured | UNKNOWN | Staging acquisition NOT RUN | Controlled cancellation metrics are not natural incidence. |
| 8 | Real duplicate Queue ID count measured | UNKNOWN | Staging acquisition NOT RUN | Uniqueness behavior is tested, incidence is unknown. |
| 9 | Real Queue-it monitoring throughput measured | UNKNOWN | Queue-it monitoring NOT RUN | Synthetic 108.41-130.56/s; local simulator 12.52/s. |
| 10 | 10,000-row full sweep measured | PASS | 76.595/92.241 s | Synthetic scheduler/SQLite handler only. |
| 11 | Adaptive backlog measured | PASS | Peak 2,885; ended zero | Deterministic synthetic lifecycle mix. |
| 12 | Oldest-overdue behavior measured | PASS | Peak 26.599 s; ended zero | Synthetic adaptive schedule. |
| 13 | Required live monitoring cadence sustainable | UNKNOWN | No Queue-it run or numeric SLO | Must not infer from synthetic rates. |
| 14 | Transfer restore reliability measured | UNKNOWN | Representative staging sample NOT RUN | Injected 503 fallback behavior passed. |
| 15 | `storage_state` restore reliability measured | UNKNOWN | Representative staging sample NOT RUN | Controlled corrupt/unavailable behavior passed. |
| 16 | Real identity mismatch incidence known | UNKNOWN | 20 mismatches were deliberately injected | Detection and preservation passed locally. |
| 17 | Application CPU measured | PASS | 59.00% average, 81.8% peak | Synthetic monitoring process, not Chrome/Queue-it. |
| 18 | Application RAM measured | PASS | 68.08/76.54 MB average/peak | Synthetic monitoring process. |
| 19 | Browser crash recovery measured | PASS | 5/5 deliberate kills recovered | Local simulator; natural staging crash rate unknown. |
| 20 | Navigation failure handling measured | PASS | 60 targeted timeouts; 483 cumulative failures | Fault injection/local simulator. |
| 21 | Restore failure handling measured | PASS | 368 transfer + 310 state failure attempts | Deliberate faults/retries, not reliability rates. |
| 22 | Scheduler remained bounded | PASS | Fixed 20 workers; claims 50 | No population-sized task set. |
| 23 | Work queues remained bounded | PASS | Queue capacity/peak 50 | Backlog stayed observable outside the queue. |
| 24 | Leasing prevented simultaneous duplicate processing | PASS | Disjoint claims, owner fencing, zero sweep conflicts | Expired takeover is explicit. |
| 25 | Restart preserved persisted population | PASS | Five 10,000-row reopen cycles | Identity digest and terminal states unchanged. |
| 26 | Interrupted creation resumed correctly | PASS | 30 committed + 70 after restart | Local simulator; no overshoot. |
| 27 | Interrupted monitoring resumed correctly | PASS | 4,934 checks after restart; backlog zero | Installed Chrome/local simulator. |
| 28 | Expired leases recovered correctly | PASS | 200/200 in 0.137 s; live 200 untouched | Killed-worker 28 recovered after lease expiry. |
| 29 | State consistency and audit behavior valid | PASS | Clean 10,000-file audits zero findings | Recovery audit reported only 20 deliberate corruptions. |
| 30 | Database backend established | PASS | SQLite | PostgreSQL not implemented. |
| 31 | PostgreSQL migration decision evidence-based | PASS | No measured SQLite trigger | Future live/multi-node evidence may reopen it. |
| 32 | State backend established | PASS | Local atomic JSON filesystem | State envelope includes identity and digest. |
| 33 | Shared/object storage decision evidence-based | PASS | Single-machine selected; local benchmark adequate | Shared backend itself is untested. |
| 34 | Final execution model established | PASS | Single machine | No distributed deployment exists. |
| 35 | Distributed execution required by cadence | UNKNOWN | No live cadence shortfall measured | Distribution was deferred, not proven unnecessary forever. |
| 36 | Distributed worker/node recovery | UNKNOWN | No multi-node system | Local separate-process recovery passed. |
| 37 | Real Queue-it lifecycle behavior validated | UNKNOWN | No authorised full lifecycle run | TURN_STARTED/READY and vendor timing remain unobserved at scale. |
| 38 | Real identity integrity established | UNKNOWN | Local digest/mismatch protection passed | Real transfer continuity and mismatch incidence unobserved. |
| 39 | No unresolved scalability problem remains | UNKNOWN | Live throughput, soak, Chrome resources, and SLO absent | Synthetic/local mechanisms do not close these gaps. |
| 40 | Original end-to-end Phase 4 goal satisfied | UNKNOWN | Bounded 10,000-row architecture proven; Queue-it target NOT RUN | The two halves were not proven together. |

**Matrix total: 24 PASS, 0 FAIL, 16 UNKNOWN.**

## Remaining Unknowns

- Acquisition of 10,000 real unique Queue-it IDs, including duration, throughput,
  duplicate rate, failure rate, and retry behavior.
- Representative transfer and `storage_state` success rates and fallback frequency.
- Real PRE_QUEUE identity continuity, last-update cadence, SERVICED_SOON,
  TURN_STARTED, READY, ADMITTED, and expiration behavior at Phase 4 scale.
- Real identity mismatch incidence and transfer continuity after Chrome/process delay.
- Full-population Queue-it monitoring throughput, backlog, oldest-overdue behavior, and
  sustainable cadence against an agreed SLO.
- Sustained Chrome CPU/RAM, context acquisition waits, descriptor growth, crash rate,
  navigation failure rate, and long-duration leakage.
- PostgreSQL, shared/object storage, distributed claims, cross-node state fencing, and
  node-loss recovery because none was selected or implemented.

## Known Issues

- Hard-kill recovery waits for lease expiry: up to the configured 120 seconds outside
  the shorter recovery harness profile.
- A deadline that fires exactly as a successful create commit finishes can cause the
  retry to collide with the same primary key. It is observable but wastes an attempt.
- After HYBRID fallback fails, navigation and context causes converge on
  `STATE_CONTEXT_FAILED`; aggregate navigation/context metrics retain the distinction.
- Local state files are last-writer-wins; database ownership is fenced, but distributed
  state-write fencing does not exist.
- SQLite is single-writer. The local workloads passed, but sustained real browser-write
  contention and multiple application processes are unmeasured.
- Recovery logging produced about 5 MB per 10,000-session sweep; sink backpressure is
  unmeasured.
- Power loss, disk-full behavior, filesystem corruption, and long soak behavior were
  not tested.
- Strict mypy currently fails on Windows at two recovery-harness references to
  `signal.SIGKILL`, which is available on the macOS/Linux execution path but absent from
  Windows type stubs. Runtime tests pass; this portability/type-check issue remains to
  be resolved without weakening the real process-kill scenario.

## Original Goal Assessment

The repository demonstrates this half of the goal:

```text
10,000 persisted synthetic session rows
+ fixed workers and bounded queues
+ at most two managed Chrome processes
+ at most 20 contexts observed (50 configured ceiling)
+ restart and lease recovery
```

It does not demonstrate:

```text
10,000 acquired Queue-it identities
+ representative Queue-it restore reliability
+ required live monitoring cadence
```

Therefore the original Phase 4 target architecture is **partially demonstrated**, and
the end-to-end result remains **UNKNOWN**, not PASS.

## Recommended Operational Configuration

The following is the largest coherent profile exercised by the recovery harness; it is
not claimed to be universally optimal:

```text
TARGET_QUEUE_IDS=10000
SESSION_MODE=HYBRID
CHROME_PROCESS_COUNT=2
MAX_CONTEXTS_PER_BROWSER=25
MAX_ACTIVE_CONTEXTS=50
CREATION_WORKERS=10
CREATION_QUEUE_CAPACITY=10
MONITOR_WORKERS=20
MONITOR_QUEUE_CAPACITY=50
MONITOR_CLAIM_BATCH_SIZE=50
DATABASE_URL=sqlite:///...
STATE_DIRECTORY=<local protected filesystem>
IDENTITY_REPLACEMENT_LIMIT=0
```

Use the normal 120-second lease until representative restore/check p95 and p99 support
a deliberate alternative. Expect actual context use to be limited by worker counts;
the recovery run peaked at 20 despite a ceiling of 50. Run preflight against dedicated
persistent paths, preserve the database/state directory for resume, and keep identity
replacement disabled unless an operator explicitly accepts that policy.

This profile should enter an authorised controlled staging run before being treated as
an operating recommendation for Queue-it.

## Future Work

Evidence-based next work is limited to closing the UNKNOWN results:

1. Run the gated acquisition against the authorised staging journey and retain the
   persistent population.
2. Benchmark representative transfer-only, storage-only, and HYBRID restoration without
   exposing sensitive URLs or identities.
3. Observe full lifecycle transitions and protected-destination admission.
4. Run a sustained browser-backed monitoring soak with an explicit backlog/cadence SLO
   and measure Chrome CPU/RAM, context waits, failures, and leakage.
5. Reopen PostgreSQL, shared storage, or distributed workers only if those measurements
   show a single-host database, storage, or cadence shortfall.
6. Exercise power-loss/disk-full behavior and measure log-sink backpressure if this
   system moves from controlled testing toward continuous operation.
