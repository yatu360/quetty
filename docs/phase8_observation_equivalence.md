# Phase 8 Prompt 4 — Direct-vs-Browser Observation Equivalence

**Date:** 2026-09-29

**Implementation status:** COMPLETE as a gated shadow-comparison mechanism

**Queue-it staging:** NOT RUN

**Conclusion:** **UNKNOWN / NOT READY FOR PRODUCTION DIRECT MONITORING**

## Decision

No authorised Queue-it discovery artifact, successful genuine replay, or reviewed
response schema exists in this workspace. Therefore direct/browser semantic equivalence
has not been demonstrated. Deterministic fixtures validate the normalization and
comparison machinery only.

**Phase 8 Prompt 5 — Production Direct Monitoring with Browser Fallback is not the next
task and must not begin.** The blocker is authorised, multi-session Prompt 2–4 evidence
covering the lifecycle states the staging event actually exposes.

## One lifecycle model

`MonitoringObservation` is the source-neutral domain input. It contains source
provenance, authoritative identity evidence, optional `QueueProgress`, explicit page
signals, redirect presence, poll timing, and schema absence/addition diagnostics.

Both experimental sources normalize into it:

1. the existing browser restorer maps its verified `SessionRestoreResult` and live DOM
   `QueueProgress` into a `browser_dom` observation;
2. an exact successful Prompt 3 response maps through an evidence-defined schema into
   a `direct_response` observation.

Both call the existing `evaluate_queue_status` rules through
`evaluate_monitoring_observation`. There is no direct-only QueueStatus evaluator.
Progress percentage alone remains insufficient. Connection loss, expiry, verified
admission, turn started, first in line, serviced soon, paused, pre-queue, active queue,
and multi-field active evidence retain their established priority.

The production `QueueSessionMonitor` now constructs the common browser observation
before evaluation and otherwise retains its previous restore, retry, progress-change,
polling, persistence, and lease behavior. Runtime strategy selection was not changed:
both Headed Window and Direct strategies still use that browser monitor.

## Evidence-defined direct schema

The direct parser contains no Queue-it response property names. A protected schema
document maps semantic fields to JSON paths only after those paths have been reviewed
from genuine authorised Prompt 2/3 evidence. Queue ID is the sole mandatory mapping.
Supported optional semantics are progress, queue number, users ahead, last update,
pause, expected service time, estimated wait, first-in-line, serviced-soon,
turn-started, connection loss, pre-/active-queue markers, expiry, redirect destination,
and poll timing.

Absent properties remain `None` and are reported as missing. Unknown response leaves
are recorded as schema additions without copying their values into aggregate reports.
Unexpected types are schema failures. Missing or ambiguous Queue ID and contradictory
Queue ID are hard direct failures; the expected Queue ID is never replaced.

A redirect is not admission merely because it exists. The redirect must match the
persisted run's configured protected destination through the existing
`AdmissionDetector`. Direct boolean fields cannot independently redefine admission.

## Comparison rules

The harness records per field:

- exact agreement;
- acceptable drift (configurable progress, users-ahead, and timestamp tolerances);
- absent from either or both sources;
- mismatch;
- unexpected schema addition;
- hard failure.

Identity ambiguity/mismatch and any lifecycle disagreement are hard failures. They are
never reconciled by choosing one source. Progress, redirect, and admission differences
remain explicit comparison results. Timing tolerances do not change lifecycle truth.

## Shadow harness

`queue-load-test-phase8-equivalence` consumes:

- an authorised protected manifest containing 1–50 unique persisted session cases;
- exact Prompt 2 artifacts and exchange sequence numbers for those sessions;
- one reviewed, authorised response-schema document;
- the sessions' existing protected browser state and Prompt 3 continuation cookies.

For each fenced session it performs the direct request first and the existing browser
restore/DOM inspection second, then normalizes and compares them. Automatic monitoring
must already be paused. Each session receives an operator lease, and the persisted run
must use Direct Monitoring Strategy. The browser backend comes from immutable run
provenance.

Traffic requires all three gates:

```text
RUN_STAGING_TESTS=1
RUN_PHASE8_EQUIVALENCE=1
--confirm-authorized-staging
```

The protected report uses case numbers rather than session/Queue IDs and contains
statuses, comparison classifications, bounded numeric deltas, lifecycle coverage, and
hard-failure counts. It excludes exact URLs, response bodies, cookies, tokens,
Authorization, storage state, transfer URLs, session IDs, and Queue IDs. It is written
mode 0600 beneath a mode-0700 ignore-all directory.

## Field findings

No genuine Queue-it comparison was run:

| Field | Result | Basis |
|---|---|---|
| Queue ID | **UNKNOWN** | No authorised direct response/browser pair. |
| Progress | **UNKNOWN** | Local drift tests are mechanism-only. |
| Queue number | **UNKNOWN** | No genuine response mapping. |
| Users ahead | **UNKNOWN** | No genuine response mapping. |
| lastUpdated | **UNKNOWN** | No genuine response mapping. |
| Pause state | **UNKNOWN** | No genuine paused-stage comparison. |
| Expected service time | **UNKNOWN** | No genuine response mapping. |
| Redirect/admission | **UNKNOWN** | No genuine admission comparison. |
| Lifecycle indicators | **UNKNOWN** | No reviewed Queue-it schema. |
| Poll timing hints | **UNKNOWN** | No genuine response mapping. |

## Lifecycle findings

Unavailable stages are not marked PASS:

| Lifecycle | Result | Basis |
|---|---|---|
| PRE_QUEUE | **UNKNOWN** | No authorised staging pair. |
| ACTIVE_QUEUE | **UNKNOWN** | No authorised staging pair. |
| Paused queue | **UNKNOWN** | No authorised staging pair. |
| SERVICED_SOON | **UNKNOWN** | No authorised staging pair. |
| TURN_STARTED | **UNKNOWN** | No authorised staging pair. |
| Admission/redirect | **UNKNOWN** | No authorised staging pair. |

## Optional customer API

No Queue-it customer API credentials or uploaded Swagger evidence were present in the
workspace, so the optional third validation source was not implemented or used. It is
not required by the shadow model. If later authorised, it must remain separately
credentialed and may validate only documented customer-API fields; it cannot replace
visitor monitoring or prove undocumented equivalence.

## Local validation (not Queue-it evidence)

Deterministic tests cover matching observations, optional field absence, schema
removal/addition, progress drift, timestamp drift, identity ambiguity/mismatch,
lifecycle disagreement, unknown fields and types, verified and unrelated redirects,
browser restore normalization, direct-then-browser order, protected manifest/report
handling, and staging gates. Existing lifecycle and monitoring regressions confirm that
progress-only observations still evaluate to CHECKING and the browser path retains its
previous semantics.

- Focused equivalence/replay/lifecycle/monitoring/UI suite: **132 passed**.
- Full non-staging suite: **603 passed, 4 staging deselected**.
- Ruff: **PASS**.
- Strict mypy: **PASS** (92 source files).
- Queue-it staging: **NOT RUN / UNKNOWN**.

## Blocker / next task

Run Prompt 2 discovery, Prompt 3 replay sufficiency, and this Prompt 4 shadow harness
against an authorised staging event. Require repeated identity-safe comparisons across
multiple sessions and every lifecycle stage actually observable in that event. Record
unavailable stages as UNKNOWN and any identity/lifecycle contradiction as a hard
failure. Production Direct monitoring remains disabled until that evidence is
sufficient.
