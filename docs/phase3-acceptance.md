# Phase 3 Acceptance Report

**Date:** 2026-09-26

**Phase result:** PARTIAL

**Acceptance matrix:** 20 PASS, 1 FAIL, 11 UNKNOWN

Phase 3 proves that the single-host SQLite, local-state, bounded-scheduler, and recovery
mechanics can handle controlled populations of approximately 1,000 synthetic sessions.
It does not prove that 1,000 real Queue-it identities can be acquired, restored, and
monitored at the required cadence. No authorised Phase 3 Queue-it staging run occurred.
Values below are kept within the scope of the workload that produced them.

## Tested Configuration

The following configurations were actually exercised:

- Repository: 1,000 synthetic rows, 600 eligible due rows, 50-row claims, 50-item
  scheduler queue, and 20 latency samples. Two independent SQLite connections also made
  disjoint claims.
- Browser capacity: installed Google Chrome against an in-memory `data:` page. The
  sequential cases were 50 contexts / 2 Chrome processes, 75 / 3, and 100 / 4, always
  with 25 contexts per process, 10 allocation workers, a two-second hold, and 0.25-second
  resource sampling.
- Acquisition correctness: synthetic identities with 20 fixed workers and a 20-item
  queue reached exactly 1,000 successes. Other controlled cases covered one duplicate,
  three transient failed attempts, one exhausted outcome, one permanent failure, two
  retries, a 613-to-1,000 resume, 999-to-1,000 completion, and interruption/resume.
  This was not a timed browser or Queue-it acquisition benchmark.
- Monitoring: 1,000 due synthetic sessions, 20 fixed workers, 50-item queue, 50-row
  claims, 120-second leases, 1 ms synthetic async handler delay, 1 ms scheduler tick,
  and two synchronized sweeps followed by one deterministic jittered pass.
- Storage: 1,000 HYBRID rows and 1,000 deterministic 1,249-byte JSON state fixtures on
  dedicated local paths. State save, load, atomic replacement, 100 deletes, consistency
  scans, and restart reconstruction were measured.
- Recovery: 1,000 mixed synthetic rows, 960 valid unique Queue IDs and associated state
  files, five repository reopen cycles, 50 active leases, 50 expired leases, five
  scheduler workers, and a 50-item queue.
- Phase 3 staging acquisition, browser-capacity staging, restore reliability, and
  browser-backed monitoring: NOT RUN. Their results are `UNKNOWN` in this report.

The intended but unexecuted staging acquisition profile was `TARGET_QUEUE_IDS=1000`,
HYBRID mode, two Chrome processes, 25 contexts per process, and a 50-context global
ceiling. The default one creation worker would not have demanded all 50 contexts.

## Environment

Known facts are limited to the recorded local runs:

- Browser-capacity host: macOS, 15 logical CPUs, 25,769,803,776 bytes RAM, installed
  Google Chrome through Playwright `channel="chrome"`.
- Storage host: macOS 26.5.1 ARM64 local filesystem, Python 3.12 environment, SQLite
  3.50.4.
- The repository benchmark document records a local macOS/Python 3.14.7 development
  host. No additional machine specification was captured for that run.
- Monitoring was browser-free. Its Chrome process count and active BrowserContext count
  were intentionally zero.
- No authorised Queue-it staging environment, event, or target-host measurement is
  represented by these results.

## 1,000-Session Population

Two different controlled populations establish different facts and must not be
combined into a claim about real Queue-it sessions:

- Storage benchmark: 1,000 persisted HYBRID rows and 1,000 state files.
- Monitoring benchmark: 1,000 parked synthetic sessions, all made due for each sweep.
- Recovery benchmark: 1,000 persisted rows, 960 valid unique synthetic Queue IDs, and
  960 state associations. Statuses were 850 `PARKED`, 50 `CONNECTION_LOST`, 30
  `ADMITTED`, 30 `EXPIRED`, and 40 `FAILED`.
- Acquisition correctness tests reached exactly 1,000 synthetic unique IDs, but the
  authorised 1,000-ID acquisition benchmark was not run.
- Peak live contexts were 100 only in the separate short browser-capacity case. The
  acquisition peak is `UNKNOWN`; the synthetic monitoring peak was zero. Thus the
  persisted populations did not require one live context per session.

## Creation Performance

Real creation performance is `UNKNOWN` because the gated acquisition benchmark was not
run. There are no measured Queue-it sessions/s, p50/p95 creation latency, total
acquisition duration, resource profile, navigation reliability, or active-context peak.

Controlled correctness evidence recorded:

| Scenario | Attempts/work | Successes | Failures | Duplicates |
|---|---:|---:|---:|---:|
| Exact synthetic target | 1,000 completed work items | 1,000 | 0 | 0 |
| Mixed synthetic case | 1,005 attempts / 1,003 work items | 1,000 | 2 outcomes | 1 |
| Resume from 613 | 387 new successes required | 387 | 0 | 0 |

The mixed case included three transient failed attempts, two retries, one exhausted
failure outcome, and one permanent failure. These scripted sleeps and identities were
deliberately excluded from throughput reporting. Therefore sessions/s, p50/p95 latency,
and total acquisition duration remain `UNKNOWN`.

## Monitoring Performance

The browser-free synthetic handler measured the scheduler, indexed SQLite query,
bounded claim, update, and lease-release path:

| Measure | Sweep 1 | Sweep 2 |
|---|---:|---:|
| Checked / successful / failed | 1,000 / 1,000 / 0 | 1,000 / 1,000 / 0 |
| Checks/s | 944.98 | 947.26 |
| Average check latency | 11.05 ms | 11.38 ms |
| p50 check latency | 13.25 ms | 13.14 ms |
| p95 check latency | 14.64 ms | 14.33 ms |
| Full sweep duration | 1.0582 s | 1.0557 s |
| Backlog start / end | 1,000 / 0 | 1,000 / 0 |
| Queue peak / worker peak | 50 / 20 | 50 / 20 |
| Lease conflicts | 0 | 0 |

The sampled due backlog averaged 262.64 and peaked at 950 after the first bounded
claim. Both synchronized sweeps returned to zero backlog, so backlog did not accumulate
for this handler. The jittered pass processed all 1,000 rows over a simulated ten-second
due window; all exact due timestamps were distinct, the largest one-second bucket was
128, and every checkpoint ended with zero due backlog.

Real Queue-it checks/s, restore/navigation latency, and full browser-backed sweep time
are `UNKNOWN`. The approximately 946 checks/s and 1.06-second sweep must not be used as
Queue-it capacity figures.

## Browser Capacity

All cases were short local installed-Chrome runs against an in-memory page:

| Contexts | Processes | Reliability | p95 create / acquire / navigation | Resource evidence |
|---:|---:|---|---|---|
| 50 | 2 | UNKNOWN | 0.100 / 0.384 / 0.377 s | 13.38/17.80 GB Chrome RSS average/peak; no pressure flag |
| 75 | 3 | UNKNOWN | 0.118 / 0.304 / 0.511 s | 16.75/22.14 GB; conservative host-RAM pressure flag |
| 100 | 4 | UNKNOWN | 0.112 / 0.262 / 0.635 s | 20.64/27.92 GB; conservative host-RAM pressure flag |

Every case reached its configured count, kept exactly 25 contexts per process, recorded
zero context creation, navigation, browser crash, cleanup, or allocation-stall failures,
and returned active capacity to zero. Reliability remains `UNKNOWN` for every level
because a two-second in-memory-page run is insufficient evidence for sustained Queue-it
operation. At 75 and 100, summed peak application-plus-Chrome RSS also crossed the
configured 80%-of-host indicator.
Summed process RSS can double-count shared pages, so this is a pressure indicator rather
than unique physical memory. Navigation p95 also rose from 0.377 seconds at 50 to 0.511
at 75 and 0.635 at 100. No adjacent 1.5x latency threshold or CPU-capacity symptom was
triggered. Sustained Queue-it reliability at every level remains `UNKNOWN`.

## Restore Reliability

No Phase 3 real transfer or `storage_state` restore benchmark was run:

- transfer success/failure and latency: `UNKNOWN`;
- `storage_state` success/failure and latency: `UNKNOWN`;
- production HYBRID fallback frequency and success: `UNKNOWN`;
- real identity mismatch incidence: `UNKNOWN`.

Controlled failure tests prove that transfer and state failures are reported against
the existing session, fallback paths are bounded, and an injected mismatch does not
replace the expected Queue ID. They do not supply real restore reliability rates.

## Persistence Performance

| Measurement | Result |
|---|---:|
| Repository benchmark database | 450,560 bytes for 1,000 mixed rows |
| Storage benchmark database | 417,792 bytes for 1,000 HYBRID rows |
| Due-count p50 / p95 | 0.098 / 0.126 ms |
| Claim-50 p50 / p95 | 0.555 / 0.673 ms |
| Update p50 / p95 | 0.365 / 0.386 ms |
| Lease-release p50 / p95 | 0.279 / 2.321 ms |
| Scheduler iteration p50 / p95 | 0.668 / 0.800 ms |
| State files / total bytes | 1,000 / 1,249,000 |
| State save p50 / p95 | 0.207 / 0.263 ms |
| State load p50 / p95 | 0.052 / 0.063 ms |
| Atomic replace p50 / p95 | 0.227 / 0.291 ms |
| Restart consistency scan | 61.355 ms, zero findings |

`EXPLAIN QUERY PLAN` used `idx_queue_sessions_due` and did not report a temporary sort.
Two SQLite connections claimed disjoint batches. Synthetic state files were all exactly
1,249 bytes and are not representative measurements of real Chrome/Queue-it
`storage_state` documents.

## Resource Usage

The synthetic monitoring process averaged 85.21% CPU and peaked at 114.3%, where 100%
is one logical core. Application RSS averaged 59,745,834 bytes and peaked at 60,882,944
bytes. It launched no Chrome processes.

The local browser cases observed:

| Contexts | App CPU avg/peak | Chrome CPU avg/peak | App RSS avg/peak | Chrome RSS avg/peak |
|---:|---:|---:|---:|---:|
| 50 | 14.83% / 96.7% | 153.1% / 601.9% | 63.28 / 64.39 MB | 13.38 / 17.80 GB |
| 75 | 18.76% / 94.0% | 206.5% / 490.0% | 51.70 / 66.14 MB | 16.75 / 22.14 GB |
| 100 | 19.75% / 94.3% | 293.2% / 553.1% | 43.54 / 47.63 MB | 20.64 / 27.92 GB |

No crash, navigation, context-creation, or cleanup failure occurred in these short
in-memory-page cases. Their incidence during real acquisition or monitoring is
`UNKNOWN`, as is sustained Chrome resource use.

## Recovery

Five local repository restarts preserved all stable identity fields, all 960 valid
Queue IDs/state associations, and all 100 terminal states. Initialize-plus-summary
latency was 0.834 ms average, 0.827 ms p50, 0.907 ms p95, and 0.925 ms maximum.

After restart, a five-worker scheduler recovered exactly the 50 expired leases in one
bounded batch, processed 50 checks, reduced due work from 800 to 750, and left all 50
unexpired leases untouched. Queue peak equalled its bound of 50. Controlled tests also
prove interrupted acquisition resumes from the committed count (including 613 to
1,000), interrupted monitoring releases or eventually expires leases, and injected
browser/restore/identity failures do not silently replace identity.

These are synthetic recovery results. Real Queue-it transfer continuity, real Chrome
crash recovery, hard host restart, and the failure window between external identity
acquisition and SQLite commit remain `UNKNOWN`.

## SQLite Decision

**PASS for current Phase 3 local scale.** SQLite remains adequate for the measured
single-host 1,000-row workload. Indexed due queries, 50-row claims, individual updates,
restart aggregation, and two-connection lease exclusion were fast and correct. There is
no measured evidence that PostgreSQL is required now.

This is not a Phase 4 endorsement. SQLite still has a one-writer model, repository
instances serialize through one async lock, and each session update/release commits
individually. PostgreSQL should be investigated during Phase 4 readiness only if the
10,000-session capacity model, multi-process write contention, required cadence, or
multi-node ownership demands it.

## Distribution Decision

**UNKNOWN.** One machine comfortably handled the synthetic SQLite/scheduler cadence,
but no browser-backed Queue-it acquisition or monitoring cadence was measured. There is
no positive measured evidence that distributed workers are needed, yet there is also
insufficient evidence that one host can satisfy the real 10,000-session cadence. No
distributed design should be implemented until Phase 4 Prompt 1 quantifies the required
rates and compares them with real browser-backed measurements.

## Acceptance Matrix

| Question | Status | Evidence | Notes |
|---|:---:|---|---|
| 1. Can the system persist approximately 1,000 Queue-it sessions? | PASS | 1,000 HYBRID rows/files persisted; 1,000 mixed rows survived restart. | Synthetic identities/state only. |
| 2. Can it acquire 1,000 unique Queue IDs reliably? | UNKNOWN | Controller reached 1,000 scripted unique IDs. | Authorised 1,000-ID Queue-it run was not run. |
| 3. Are most sessions parked rather than kept in live contexts? | PASS | Recovery population had 850/1,000 `PARKED`; monitoring used 1,000 parked rows and zero contexts. | Real Queue-it population remains untested. |
| 4. Is live BrowserContext count bounded? | PASS | Fixed limits held at 50, 75, and 100; cleanup returned count to zero. | Separate short local browser workload. |
| 5. Can 50 active contexts operate reliably? | UNKNOWN | 50/50 local contexts completed with no recorded failures or RAM-pressure flag. | Two seconds against an in-memory page is insufficient reliability evidence. |
| 6. Can 75 active contexts operate reliably? | UNKNOWN | 75/75 completed, but summed peak RSS crossed the configured 80% host indicator. | Staging and sustained reliability were not observed. |
| 7. Can 100 active contexts operate reliably? | UNKNOWN | 100/100 completed, but summed peak RSS crossed the configured indicator and p95 navigation reached 0.635 s. | Staging and sustained reliability were not observed. |
| 8. At what tested levels did latency or failures worsen? | PASS | Navigation p95 rose at 75 and 100; no failures occurred. | RSS pressure appeared at 75 and 100. |
| 9. Can the scheduler efficiently handle 1,000 persisted sessions? | PASS | Two ~1.06 s synthetic sweeps; fixed 20 workers and 50-item queue. | Browser work excluded. |
| 10. Does `next_check_at` querying remain efficient? | PASS | Indexed due-count p50 0.098 ms; no temporary sort. | Local 1,000-row SQLite result. |
| 11. Does bounded leasing prevent simultaneous duplicate checks? | PASS | Disjoint two-connection claims, zero sweep conflicts, active leases excluded. | SQLite single-host semantics only. |
| 12. What checks/sec were observed? | PASS | 944.98 and 947.26 synthetic checks/s. | Real Queue-it checks/s is unknown. |
| 13. What was the full monitoring sweep duration? | PASS | 1.0582 and 1.0557 seconds. | Synthetic handler only. |
| 14. Does scheduler backlog remain controlled? | PASS | Both 1,000-row sweeps and every staggered checkpoint drained to zero. | Peak sampled backlog was 950 after the first bounded claim. |
| 15. Does adaptive polling/jitter prevent unnecessary synchronization? | PASS | 1,000 distinct timestamps over ~10 seconds; largest one-second bucket 128. | Deterministic simulated scheduling evidence. |
| 16. What transfer restore reliability was observed? | UNKNOWN | No Phase 3 staging restore run. | Controlled error behavior supplies no rate. |
| 17. What `storage_state` restore reliability was observed? | UNKNOWN | No Phase 3 staging restore run. | Controlled error behavior supplies no rate. |
| 18. Were identity mismatches observed? | UNKNOWN | Real mismatch incidence was not measured. | Injected mismatch tests preserved expected identity. |
| 19. What CPU levels were observed? | PASS | Monitoring app 85.21%/114.3%; browser-case CPU is reported above. | Workload-specific, not staging capacity. |
| 20. What RAM levels were observed? | PASS | Monitoring app ~59.75/60.88 MB; browser RSS increased through 17.80/22.14/27.92 GB peaks. | Summed Chrome RSS may double-count shared pages. |
| 21. Were Chrome crashes observed? | UNKNOWN | Zero in short local capacity cases. | Real/sustained Queue-it crash incidence was not observed. |
| 22. Were navigation failures observed? | UNKNOWN | Zero against the in-memory page. | Real Queue-it navigation failure incidence was not observed. |
| 23. Does SQLite remain adequate at 1,000 sessions? | PASS | Sub-millisecond p50 core operations, correct leases/restart, small footprint. | Single-host local scope. |
| 24. Do local state files remain manageable at 1,000 sessions? | PASS | 1,000 files, fast save/load/replace, zero consistency findings. | Actual Queue-it state sizes and concurrent refresh are unknown. |
| 25. What disk usage was observed? | PASS | 417,792-byte storage DB plus 1,249,000-byte fixture state directory. | Repository benchmark DB separately measured 450,560 bytes. |
| 26. Does restart/recovery preserve the 1,000-session population? | PASS | Five restarts retained 1,000 rows, 960 Queue IDs, state associations, and terminal states. | Synthetic population. |
| 27. Are expired leases safely recovered after restart? | PASS | Exactly 50 expired leases recovered; 50 live leases untouched. | One bounded synthetic batch. |
| 28. Can interrupted creation resume toward 1,000? | PASS | 613-to-1,000 and five-then-resume cases completed without overshoot. | Controlled identities. |
| 29. Are there unresolved identity-integrity problems? | UNKNOWN | No corruption in controlled tests, but no real 1,000-ID acquire/restore/restart run. | Real identity integrity is not established. |
| 30. Is there measured evidence PostgreSQL is now needed? | PASS | No; current SQLite measurements are adequate at 1,000 local rows. | Reassess from Phase 4 capacity requirements. |
| 31. Is there measured evidence distributed workers are now needed? | UNKNOWN | Synthetic cadence fits one host, but real browser cadence is absent. | Evidence is insufficient for either necessity or one-host sufficiency. |
| 32. Are there blockers before Phase 4? | FAIL | Missing real acquisition, restore, monitoring, sustained browser, and recovery evidence; 75/100 RAM pressure. | Phase 4 target execution is blocked pending readiness work. |

## Known Issues

- The 1,000-ID authorised Queue-it acquisition benchmark was not run; uniqueness,
  creation throughput, latency, duplicates, failures, resource use, and duration are
  unknown for real identities.
- Transfer-first, `storage_state`, and HYBRID fallback reliability were not measured at
  Phase 3 scale. Real identity-mismatch incidence is unknown.
- The measured checks/s and full sweep duration exclude browser context acquisition,
  transfer/state restore, navigation, DOM inspection, and state refresh.
- No sustained Queue-it browser-capacity case exists. The short 75- and 100-context
  cases crossed the conservative summed-RSS pressure indicator.
- Actual Queue-it `storage_state` size distribution, concurrent state refresh, 10,000
  files, disk-full behavior, abrupt power loss, and filesystem corruption are untested.
- Real Chrome crash, navigation failure, host restart, and hard-kill recovery remain
  unknown. Hard kills retain leases until expiry.
- SQLite retains one-writer and per-session-commit constraints. Multi-process write
  contention and multi-node ownership were not tested.
- Target accounting is single-controller. Multiple acquisition controllers do not
  transactionally reserve a shared remaining-target budget.
- A crash between external Queue-it identity acquisition and local commit remains an
  untested loss window.
- Required monitoring cadence and failure budgets for 10,000 sessions have not yet been
  defined, so one-machine sufficiency cannot be decided.

## Phase 4 Readiness

Phase 4 Prompt 1 may perform readiness and capacity modelling, but the 10,000-session
traffic run is not ready. Moving from 1,000 to 10,000 is not a tenfold configuration
change.

Before attempting the Phase 4 target:

- Establish real browser-backed baseline measurements, preferably beginning with the
  unresolved authorised Phase 2/3 staging gates: creation sessions/s and latency,
  transfer/state restore rates, real check latency, full-sweep duration, failure rates,
  CPU/RAM, and recovery continuity.
- Define the 10,000-session monitoring cadence by lifecycle state and calculate required
  checks/s, queue/backlog budget, worker count, context demand, and acquisition window.
- Model SQLite write volume, due-count/claim/update contention, sweep time, and restart
  aggregation at 10,000 rows. Benchmark before choosing PostgreSQL; investigate it if
  the required write concurrency or multi-node lease ownership exceeds SQLite's model.
- Measure actual state-file sizes and refresh churn, then test 10,000-directory scans,
  disk capacity/IOPS, backup, corruption detection, and recovery time. Shared/object
  state is conditional on distribution, not automatic.
- Treat 50 contexts as the least resource-pressured tested candidate starting ceiling,
  not an accepted reliable operating point. Revisit
  browser-process count, context allocation serialization, resource headroom, and
  sustained stability with real pages. Do not carry 75 or 100 forward as accepted
  operating points.
- Quantify creation throughput and interruption recovery before planning a 10,000-ID
  acquisition duration. Preserve deficit-aware, bounded creation and uniqueness.
- Exercise process termination and browser failure during real monitoring and verify
  identity preservation, lease expiry/recovery, backlog catch-up, and state integrity.
- Decide on multiple nodes only after the capacity model shows one host cannot meet the
  required cadence or recovery objective. If distribution is required, prove database
  leasing and shared-state prerequisites at smaller scale first.

Phase 3 therefore closes as **PARTIAL**. Phase 4 Prompt 1 is the next planning task; no
Phase 4 implementation or 10,000-session run is included here.
