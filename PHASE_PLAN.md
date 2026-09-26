# Project Phase Plan

## Global Goal

The durable architecture is:

`TARGET_QUEUE_IDS=N → bounded creation workers → isolated Chrome contexts → Queue-it identities → persist → park → due-session scheduler → bounded monitoring workers → restore → inspect live progression → persist → park again`

The persisted population may grow much larger than the live browser population. Scaling
must come from measured throughput, bounded concurrency, reliable identity restoration,
and efficient parked-session scheduling—not from keeping every visitor live.

## Phase 1 — Core Validation

**Status: implementation completed through Prompt 12; acceptance PARTIAL.**

Phase 1 targeted 10 Queue IDs with HYBRID mode, one Chrome process, and at most five
active contexts. It implemented and locally validated configuration, domain/lifecycle
rules, SQLite and atomic JSON persistence, shared-Chrome context management, defensive
live DOM extraction, supported transfer identity capture, bounded acquisition,
identity-safe restoration, parked monitoring, adaptive polling, recovery/shutdown,
observability, and a controlled acceptance harness.

The deterministic local 10-session acceptance report is PASS for all twelve mechanisms.
No authorised real-staging run is present, so all real Queue-it acceptance questions are
still UNKNOWN: identity independence, PRE_QUEUE behavior and continuity, official
transfer extraction/restoration, storage-state restoration, progress/update cadence,
late lifecycle states, admission, and restart recovery with real journeys. Real latency,
restore-rate, CPU/RAM, crash, failure, and mismatch measurements are also absent.

Before increasing the target, Phase 2 Prompt 1 must review this evidence gap and either
run the gated Phase 1 staging harness or explicitly document why scaling readiness can
proceed without it.

## Phase 2 — 100 Sessions

**Status: in progress. Prompts 1–5 are complete for local configuration, multi-browser,
acquisition, bounded monitoring, and restore-benchmark tooling. Prompt 6 is next. No
100-session staging run or performance tuning has started.**

Target profile:

- `TARGET_QUEUE_IDS=100`
- `MAX_ACTIVE_CONTEXTS≈25`
- `CHROME_PROCESS_COUNT=1–2`

Goals:

- Validate scaling from 10 to 100 persisted sessions.
- Benchmark session-creation and monitoring throughput.
- Measure transfer and storage-state restore reliability.
- Measure CPU/RAM and browser stability.
- Validate allocation across more than one browser process where justified.
- Identify the point at which context/worker concurrency saturates.
- Preserve the parked-session architecture and identity invariants.

### Prompt 1 — Phase 2 Configuration and Scaling Readiness

**Status: completed on 2026-09-26.**

Objective: define an explicit, validated 100-session profile and decide whether Phase 1
staging evidence is sufficient to begin scale testing.

Expected outputs:

- Phase 2 configuration defaults/overrides and validation rules.
- A readiness checklist based on the Phase 1 acceptance report and any new authorised
  10-session staging run.
- Defined measurement fields and acceptance thresholds for reliability, resource use,
  context leakage, and monitoring cadence.
- Documentation of the selected one- or two-process starting profile.

Exit criteria:

- The 100-session configuration is internally consistent and covered by tests.
- Any unresolved Phase 1 staging evidence is either collected or explicitly blocks the
  100-session traffic run.
- No Phase 2 traffic is sent before the authorised target/profile is confirmed.

Implemented evidence: defaults now describe 100 targets, one Chrome process, 25
per-browser/global contexts, HYBRID mode, and conservative fixed worker counts. A
two-process 13-per-browser/25-global profile validates successfully. Contradictory
capacity and worker combinations are rejected. Tests prove 100-target creation retains
fixed worker concurrency and a 100-session persisted population fills only a bounded
25-item monitoring queue. Real-staging readiness remains unresolved and still gates
traffic execution, not Prompt 2's local BrowserManager work.

### Prompt 2 — Multi-Browser BrowserManager

**Status: completed on 2026-09-26 with fake-based process failure tests; real Chrome
crash behavior remains unverified.**

Objective: validate and, only where tests show a gap, harden least-loaded allocation and
failed-process recovery with one to two Chrome processes and about 25 global contexts.

Expected outputs:

- Deterministic multi-browser allocation/capacity tests.
- Failure/restart tests with contexts distributed across processes.
- Metrics/status coverage for per-process load and aggregate capacity without
  high-cardinality labels.

Exit criteria:

- No allocation exceeds per-browser or global limits.
- A failed process can be replaced without changing persisted Queue IDs.
- All contexts are released after success, error, cancellation, and shutdown.

### Prompt 3 — Scale Queue ID Acquisition to 100 Sessions

**Status: completed on 2026-09-26 for controller correctness and observability. The
authorised 100-session staging acquisition/benchmark remains not run.**

Objective: run bounded creation until 100 successful unique Queue IDs exist and measure
creation throughput under controlled concurrency.

Expected outputs:

- An opt-in 100-session acquisition harness using existing creation workers and SQLite.
- Creation latency/throughput, duplicate, transient/permanent failure, and identity
  mismatch results.
- Proof that failed/duplicate attempts do not satisfy the target and scheduling stops at
  the target.

Exit criteria:

- Exactly 100 successful unique persisted Queue IDs, or an honest FAIL/UNKNOWN report.
- Active contexts remain within the configured limit with no leaks.
- Sensitive transfer/state data is not emitted in normal logs or benchmark results.

Implemented evidence: deterministic tests cover targets 1, 10, and 100, bounded worker
and queue concurrency, temporary/permanent failures, duplicate isolation, startup with
an already-satisfied target, restart from a partially populated repository, and a
99/100 near-target case that schedules only one new attempt. Prometheus exposes
low-cardinality creation attempts, acquisitions, duplicate/failure classifications,
in-flight work, queue depth, duration, and current-run rate. No staging throughput or
resource claim is made.

### Prompt 4 — Phase 2 Monitoring Throughput

**Status: completed on 2026-09-26 for bounded scheduling correctness and telemetry.
Real-environment throughput and full-sweep timing remain unmeasured.**

Objective: measure due-session claim, restore, inspect, persist, release, and re-park
throughput across 100 persisted sessions.

Expected outputs:

- Full/partial sweep timing and scheduler-backlog measurements.
- Queue-depth, lease, fairness, stale-update, and checks-per-second evidence.
- Tests that one session cannot be checked concurrently by multiple workers.

Exit criteria:

- Queues remain bounded and leases are released/expire safely.
- The measured sweep/check cadence satisfies the thresholds defined in Prompt 1.
- No starvation, accidental task explosion, or simultaneous duplicate checks are seen.

Implemented evidence: synthetic tests with approximately 100 SQLite sessions verify
due/future filtering, a bounded claim batch, fixed worker tasks, slow-worker
backpressure, atomic lease exclusion/expiry, success and exception release paths,
adaptive intervals/jitter, re-parking, shutdown lease cleanup, local lease renewal
without duplicate checks, and a tick-paced idle loop. Prometheus now exposes active
monitoring workers, queue depth, due backlog, claims, and lease conflicts alongside
existing check/restore metrics. No real Chrome monitoring sweep or throughput result
is claimed.

### Prompt 5 — HYBRID Restore Reliability Benchmark

**Status: benchmark implementation completed on 2026-09-26; authorised staging run is
NOT RUN and all real reliability results remain UNKNOWN.**

Objective: quantify official transfer restoration and storage-state fallback behavior for
the 100-session population.

Expected outputs:

- Transfer attempts/successes, storage attempts/successes, failures by sanitized class,
  and identity mismatch counts.
- Evidence that expected identities remain immutable after mismatch/failure.
- Separate results for transfer-first and storage fallback behavior.

Exit criteria:

- Restore reliability meets the threshold defined in Prompt 1 or the phase is marked
  blocked with evidence.
- Zero silent identity replacements or identity corruption.
- Permanent failures terminate; transient failures stay bounded.

Implemented evidence: the opt-in runner supports transfer-only, storage-state-only,
and production HYBRID modes over a configurable sample up to the full 100-session
population. JSON and aggregate text reports cover identities, sanitized failures,
fallbacks, duration percentiles, and per-mechanism reliability without transfer URLs or
browser state. Unit tests validate the reporting math and safety gates. No real restore
rate or latency result is claimed.

### Prompt 6 — Resource and Stability Benchmarking

Objective: measure controller/Chrome CPU, RAM, crash behavior, context churn, SQLite
activity, and state-file footprint during acquisition and monitoring.

Expected outputs:

- Time-series or interval summaries for CPU/RAM, process/context counts, crashes,
  navigation failures, and disk usage.
- A repeatable run manifest containing configuration and duration but no sensitive
  session values.
- Recovery evidence following controlled browser failure and process restart.

Exit criteria:

- Resource headroom is measured on the intended host and judged against the thresholds
  defined in Prompt 1.
- Context/process counts return to baseline after the run.
- Restart preserves persisted journeys and leases recover safely.

### Prompt 7 — Phase 2 Concurrency Tuning Harness

Objective: compare a small matrix of worker, context, and Chrome-process settings without
changing architectural semantics.

Expected outputs:

- Reproducible profiles and side-by-side acquisition/check throughput, latency,
  failure-rate, and resource results.
- An evidence-based recommended Phase 2 operating point.
- Identified saturation signals and safe upper bounds.

Exit criteria:

- The selected profile is justified by measured results, not assumptions.
- Higher concurrency is rejected when it increases failures or resource pressure without
  useful throughput.
- All tested profiles retain bounded queues and identity safety.

### Prompt 8 — Phase 2 Acceptance Report

Objective: consolidate the authorised 100-session evidence and decide whether Phase 3
may begin.

Expected outputs:

- PASS/FAIL/UNKNOWN answers for acquisition, identity continuity, restore reliability,
  monitoring cadence, stability, resource headroom, recovery, and data safety.
- Exact configuration, run duration, measurement summaries, and unresolved assumptions.
- Updated `PROJECT_CONTEXT.md` and `CHANGELOG_AI.md`.

Exit criteria:

- The report is reproducible and contains no sensitive transfer URLs/state.
- All Phase 2 scaling gates are explicitly PASS or the next phase remains blocked.
- Results are not generalized beyond 100 sessions.

## Phase 3 — 1,000 Sessions

**Status: planned, not started.**

Target profile:

- `TARGET_QUEUE_IDS=1000`
- `MAX_ACTIVE_CONTEXTS≈50–100`, selected from Phase 2 evidence rather than assumed.

Goals:

- Keep the vast majority of sessions parked.
- Validate scheduling over a much larger persisted population.
- Test database query/index performance and worker/lease fairness.
- Benchmark full monitoring sweep time and scheduler backlog.
- Determine whether SQLite remains acceptable or PostgreSQL is justified.
- Validate state-file/disk scalability and restart recovery.

Planned prompt breakdown:

1. **Phase 3 readiness and 1,000-session profile** — review Phase 2 gates, establish
   authorised limits, cadence requirements, and run manifests.
2. **SQLite query/index benchmark** — measure due scans, claims, updates, counts, and
   database growth at representative populations without changing backend prematurely.
3. **1,000-session acquisition run** — acquire unique identities with bounded creation
   and record throughput/failure/resource data.
4. **Large-population scheduler fairness** — measure due ordering, lease contention,
   starvation, queue backpressure, and backlog behavior.
5. **Monitoring sweep benchmark** — measure full and priority-state sweep times under
   adaptive polling and bounded contexts.
6. **State-file and restart scale test** — measure filesystem footprint, atomic writes,
   corrupt/missing-state isolation, and recovery after restart.
7. **SQLite versus PostgreSQL decision** — use measured latency, locking, recovery, and
   operational requirements to decide whether migration is justified; do not migrate by
   default.
8. **Phase 3 acceptance report** — record PASS/FAIL/UNKNOWN gates and the evidence-based
   Phase 4 architecture recommendation.

## Phase 4 — 10,000 Sessions

**Status: planned, not started.**

Target: `TARGET_QUEUE_IDS=10000` with browser capacity remaining explicitly bounded.

Goals:

- Validate the full target scale and an explicit creation/check throughput model.
- Determine whether one machine meets required acquisition and monitoring cadence.
- Migrate to PostgreSQL only if prior benchmarks require it.
- Add distributed workers only if one-machine evidence requires them.
- Use shared/object state storage only if distribution requires it.
- Use PostgreSQL worker leasing (`FOR UPDATE SKIP LOCKED` or equivalent) if PostgreSQL is
  selected.
- Provide production-style Prometheus/Grafana monitoring and recovery from worker/node
  loss.

Planned prompt breakdown:

1. **Phase 4 readiness and capacity model** — translate Phase 3 measurements into
   required sessions/minute, checks/minute, browser capacity, storage, and failure
   budgets.
2. **Persistence architecture implementation (conditional)** — implement PostgreSQL
   behind `SessionRepository` only if the Phase 3 decision requires it; include schema,
   migrations, locking, and recovery tests.
3. **Shared state storage implementation (conditional)** — introduce an object/shared
   `StateStore` only when multiple nodes require it, retaining atomic/version-safe writes
   and sensitive-data controls.
4. **Distributed worker leasing (conditional)** — add creation/monitor workers and
   ownership/heartbeat behavior only after backend prerequisites and failure semantics
   are proven.
5. **10,000-session acquisition benchmark** — run bounded acquisition with throughput,
   reliability, and resource evidence.
6. **10,000-session monitoring/backlog benchmark** — validate adaptive cadence,
   prioritization, sweep time, queue depth, fairness, and degraded-mode behavior.
7. **Worker/node loss and disaster recovery** — exercise lease expiry, process/node
   loss, restart, state-store faults, and identity preservation.
8. **Production observability and Phase 4 acceptance** — finalize dashboards/alerts and
   produce the full PASS/FAIL/UNKNOWN scale report.

## Scaling Gates

### Phase 1 → Phase 2

- Run or explicitly resolve the missing authorised 10-session staging evidence.
- Demonstrate 10 unique identities with zero silent identity replacements.
- Verify transfer and storage restoration against the staging journey.
- Confirm PRE_QUEUE/ACTIVE_QUEUE continuity and expected admission destination.
- Confirm context count never exceeds five and returns to zero after work.
- Capture baseline creation, restore, check, CPU/RAM, crash, and failure measurements.
- Keep bounded workers/queues and pass all ordinary tests.

### Phase 2 → Phase 3

- Acquire and persist 100 unique identities with zero identity corruption.
- Meet the reliability and resource thresholds defined in Phase 2 Prompt 1.
- Demonstrate bounded context/process counts and no leak after sustained monitoring.
- Measure monitoring sweep time and show it satisfies the required adaptive cadence.
- Show leases prevent simultaneous checks and that backpressure does not cause
  starvation.
- Record CPU/RAM/disk headroom, failure rates, and browser stability on the intended
  host.
- Resolve all Phase 2 FAIL results; retain UNKNOWN only with an explicit decision that it
  is safe to proceed.

### Phase 3 → Phase 4

- Acquire/recover a 1,000-session population without identity corruption.
- Establish full-sweep/backlog behavior and prove it meets required service cadence.
- Demonstrate acceptable database claim/update latency and state-file performance.
- Complete an evidence-based SQLite/PostgreSQL decision.
- Quantify one-machine throughput and resource headroom sufficiently to model 10,000
  sessions.
- If distribution is required, prove repository leasing and shared-state prerequisites
  at smaller scale before the 10,000-session run.
- Resolve recovery, worker fairness, and node/process failure risks.

## Explicit Non-Goals

- No bot evasion, stealth automation, fingerprint spoofing, or automation hiding.
- No undocumented/private Queue-it APIs or manually constructed internal URLs.
- No uncontrolled browser, context, task, or worker explosion.
- No assumption that every persisted session stays live in a browser.
- No premature PostgreSQL, distributed worker, shared-storage, or orchestration
  complexity without benchmark evidence.
- No production traffic or unauthorised target environments.
- No claim that results at 10, 100, or 1,000 sessions automatically generalize to the
  next scale.
