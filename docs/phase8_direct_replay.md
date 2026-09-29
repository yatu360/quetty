# Phase 8 Prompt 3 — Direct Request Replay and State Sufficiency Experiment

**Date:** 2026-09-29

**Implementation status:** COMPLETE as a gated experiment/harness

**Queue-it staging:** NOT RUN
**Conclusion:** **UNKNOWN**

## Conclusion

There is no genuine Queue-it Prompt 2 artifact or authorised target in this workspace.
The only available captures are local, deliberately non-Queue-it mechanism tests.
Consequently no Queue-it request was replayed and none of the storage-sufficiency
outcomes A, B, C, or D is established.

The implementation is ready to gather that evidence, but it is not evidence itself.
Phase 8 Prompt 4 is not ready to begin. The blocker is an authorised Queue-it staging
run with genuine Prompt 2 status-request candidates from multiple legitimate sessions.

## Experimental boundary

`direct_replay` consumes one exact exchange selected by sequence from a protected
Prompt 2 artifact. It validates the artifact's sensitive format and authorised scope,
session ID, authoritative persisted Queue ID, status-candidate classification, exact
HTTP URL/method/body, and non-truncated request body. It never supplies a path, host,
query value, method, body, or identity that the browser did not capture.

The opt-in `queue-load-test-phase8-direct-replay` harness additionally requires:

- an existing Direct Monitoring Strategy run and matching session row;
- persisted monitoring paused and no automatic/manual owner on that session;
- that session's integrity-checked browser `storage_state` document;
- `RUN_STAGING_TESTS=1`, `RUN_PHASE8_DIRECT_REPLAY=1`, and
  `--confirm-authorized-staging`;
- at least three and at most twenty repetitions per comparison profile.

The client has an explicit timeout, at most two HTTP connections, one in-flight request
per session, a bounded response body, redirects disabled, and no retry that can invent
or reacquire identity. Network, timeout, HTTP, redirect, schema, rejected-state, and
identity failures are separate results. A named response `queueId` that contradicts
the persisted Queue ID is an identity failure. Absence of a response Queue ID is not
misreported as identity confirmation.

Normal automatic monitoring is unchanged. Both monitoring strategies still use the
existing browser-backed monitor, and Manual Open remains independent.

## State and minimisation experiment

Initial cookies come only from the session's persisted Playwright/Patchright
`storage_state`. A captured `Cookie` header is never copied. Response cookies are
retained by the bounded HTTP client and atomically saved in a separate mode-0600,
session-and-recipe-bound document beneath a mode-0700, ignore-all directory. A later
harness process can reload that protected continuation state. The experiment never
rewrites browser state, the database, Queue ID, transfer URL, or browser provenance.

Two header profiles are repeated:

- `full_derived` removes browser transport headers (`Host`, `Content-Length`,
  connection/encoding headers, `Cookie`, and `Sec-*`) but retains other captured
  application headers, including protected values in memory;
- `minimal` retains only captured `Accept` and `Content-Type` values.

Each persisted cookie may also be omitted independently for at least three requests,
up to a fixed candidate limit. A cookie is classified `required` only when the baseline
succeeds and every repeated omission produces state/identity failure. Repeated success
without it is evidence that it was not required in that tested scope; it is not a
production-removal decision. Mixed outcomes remain unknown.

## Queue-it questions

No authorised Queue-it replay was run, so the current findings are:

| Question | Finding | Basis |
|---|---|---|
| Is existing `storage_state` sufficient? | **UNKNOWN** | No genuine request replay. |
| Which cookies are required? | **UNKNOWN** | No authorised repeated ablation. |
| Are additional session values required? | **UNKNOWN** | No genuine request replay. |
| Are event values reusable across sessions? | **UNKNOWN** | No multi-session Queue-it evidence. |
| Which headers are semantically required? | **UNKNOWN** | No repeated Queue-it profile comparison. |
| Does the body change between polls? | **UNKNOWN** | No genuine repeated capture. |
| Do query values rotate? | **UNKNOWN** | No genuine repeated capture. |
| Does the response update cookies/tokens? | **UNKNOWN** | No genuine response. |
| Does replay alter the still-open browser? | **UNKNOWN** | Browser-open case not run. |
| Does replay work after context close? | **UNKNOWN** | No genuine replay. |
| Does replay work after application restart? | **UNKNOWN** | No genuine cross-process application case. |
| Does it retain the authoritative Queue ID? | **UNKNOWN** | No genuine response identity evidence. |

## State-classification table

The protected harness report applies the required vocabulary conservatively. With no
Queue-it evidence, the present table is:

| Value | Classification | Evidence needed |
|---|---|---|
| Host/path | `unknown` | Same-event comparison across legitimate sessions. |
| customerId | `unknown` | Genuinely observed named value across sessions. |
| eventId | `unknown` | Genuinely observed named value across sessions. |
| Queue ID | `unknown` | Three consistent requests and replay response identity evidence. |
| Request body fields | `unknown` | At least three browser-observed polls. |
| Query parameters | `unknown` | At least three browser-observed polls. |
| Request cookies | `unknown` | Repeated, identity-preserving per-cookie ablation. |
| CSRF/session tokens | `unknown` | Observed semantics and repeated controlled trials. |
| Response cookies | `unknown` | Genuine `Set-Cookie` response evidence. |
| Polling/version fields | `unknown` | Repeated observed values and response comparison. |

Within an authorised artifact, the analyzer may classify a Queue ID as
`session-stable`, changing body/query values as `request-transient`, and captured
response cookie changes as `response-refreshed`. It never calls host, path, customer,
or event values `event-stable` from one session.

## Storage-design decision

| Outcome | Current result |
|---|---|
| A. Existing `storage_state` is sufficient | **UNKNOWN** |
| B. `storage_state` plus non-secret event recipe is sufficient | **UNKNOWN** |
| C. Additional protected per-session metadata is required | **UNKNOWN** |
| D. Replay is too browser-runtime-dependent | **UNKNOWN** |

The exact captured recipe is protected evidence today because its URL, headers, body,
and identifiers may contain credentials. Calling any part of it “non-secret event
metadata” requires evidence that has not yet been gathered. Response-refreshed cookies
are stored separately only for the experiment; that is not a production storage
decision.

## Security and reports

Raw cookies, Set-Cookie, Authorization, tokens, `storage_state`, transfer URLs, exact
request URLs, and raw request/response bodies never enter normal logs, metrics,
dashboard HTML, SQLite, or aggregate acceptance/benchmark reports. Normal logs contain
only profile, status/failure class, and counts. Dedicated replay artifacts live only in
the protected, git-ignored evidence directory. Cookie values are never reported;
protected reports retain cookie names beside stable candidate labels so an authorised
investigation can identify which cookie was tested.

The ownership checks prevent knowingly racing the application's browser work, but they
do not prove operating-system browser absence. Therefore the harness does not label a
browser-closed case PASS merely because persisted ownership is clear. A controlled
staging procedure must establish and record the actual open/closed condition.

## Local validation (not Queue-it evidence)

Deterministic HTTP fixtures cover exact query/body replay, storage-state cookies,
response cookie refresh, protected persistence and client restart, rotating captured
body/query analysis, redirects, rejected/expired state, timeout/network errors,
unexpected content type, malformed/oversize JSON, explicit Queue ID mismatch,
header filtering, cancellation/cleanup, permissions, recipe/state mismatch, and
sensitive log suppression.

These tests validate the harness mechanics only. They do not establish a Queue-it
visitor contract or staging behavior.

- Focused replay/discovery/monitoring/UI suite: **86 passed**.
- Full non-staging suite: **583 passed, 4 staging deselected**.
- Ruff: **PASS**.
- Strict mypy: **PASS** (86 source files).
- Queue-it staging: **NOT RUN / UNKNOWN**.

## Blocker / next task

Run the gated Prompt 2 discovery and Prompt 3 replay experiment against an authorised
Queue-it staging event with multiple legitimate sessions, repeated polls, browser-open
and browser-closed observations, and a real application restart. Prompt 4's shadow
mechanism now exists, but its equivalence result remains UNKNOWN and production direct
monitoring must not begin until the missing evidence is sufficient.
