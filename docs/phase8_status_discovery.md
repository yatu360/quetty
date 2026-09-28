# Phase 8 Prompt 2 — Browser-Observed Visitor Status Discovery

**Date:** 2026-09-29  
**Status:** COMPLETE as an evidence-gathering mechanism  
**Queue-it staging:** NOT RUN / UNKNOWN  
**Production direct monitoring:** NOT IMPLEMENTED

## Boundary

This prompt adds an opt-in diagnostic observer for requests made by the legitimate
visitor page in its existing browser context. It does not construct, guess, or replay a
Queue-it URL. It does not change session creation, the Headed Window Strategy, Queue ID
identity, browser-backend selection, scheduler ownership, or the browser-backed monitor.

For a Direct Monitoring Strategy run, discovery is active only when
`STATUS_DISCOVERY_ENABLED=true`. The normal restorer creates the same context and page,
attaches the observer before navigation, performs its existing transfer/storage restore
and DOM inspection, saves protected evidence, and parks the context normally. With the
flag off, both strategies behave exactly as in Prompt 1.

## Source of truth

The only prospective source of truth is a request actually emitted by that live page.
The observer records bounded `document`, `xhr`, and `fetch` exchanges and then
correlates background JSON responses with the same page's final extracted DOM state.
No path, method, body, identifier position, or request cadence is assumed.

### Historical Glastonbury clue (not an observed fact)

A saved 2025 page contained JavaScript resembling a path assembled from a configured
prefix and `customerId`, `eventId`, and `queueId`. That material was not used to build a
URL, classifier path rule, request body, or replay contract. In particular, the
implementation does not hard-code `/queue` or `/spa-api`. The clue remains historical
context only.

## Protected artifact contents

Each observation writes one mode-0600 JSON file beneath a mode-0700 directory (default
`.status-discovery/`, explicitly git-ignored). The raw file is marked
`SENSITIVE_VISITOR_CREDENTIAL_EVIDENCE` and can contain:

- exact URL and decomposed scheme, host, path, and ordered query values;
- method, resource type, full available request headers, content type, bounded body,
  and parsed JSON;
- response status, headers, content type, bounded body, parsed JSON, Set-Cookie values,
  and a post-response context-cookie snapshot;
- redirect source/destination, failure, start/completion time, duration, repeated-route
  cadence, and dropped-event counts;
- identifiers only when actually present as named query/JSON values or, for the known
  authoritative Queue ID, when its exact value occurs in captured request material;
- a final DOM snapshot and exact response-field/value correlations;
- explicit PASS/FAIL/UNKNOWN questions when the artifact is explicitly scoped as
  authorised Queue-it staging.

Bodies, headers, cookies, exchanges, and pending response work are bounded. Response
bodies with missing or excessive declared length are not loaded because Playwright's
body API buffers the whole value. Listener removal, worker drain/cancellation, and file
write are deadline-bounded and observer failures cannot change the restore result.

Normal structured logs contain only fixed event names, sanitized classification counts,
and totals. Raw URLs, headers, bodies, cookies, identifiers, artifact paths, and session
IDs are absent. No discovery data is added to Prometheus, SQLite, dashboard HTML, or
aggregate acceptance/benchmark reports.

## Configuration and gate

```text
STATUS_DISCOVERY_ENABLED=false
STATUS_DISCOVERY_DIRECTORY=.status-discovery
STATUS_DISCOVERY_SCOPE=diagnostic_unverified_target
STATUS_DISCOVERY_CONFIRM_AUTHORIZED_STAGING=false
STATUS_DISCOVERY_MAX_EXCHANGES=100
STATUS_DISCOVERY_MAX_BODY_BYTES=65536
STATUS_DISCOVERY_EVENT_QUEUE_CAPACITY=100
STATUS_DISCOVERY_CLEANUP_TIMEOUT_SECONDS=5.0
STATUS_DISCOVERY_OBSERVE_SECONDS=30.0
```

Allowed scopes are `diagnostic_unverified_target`, `local_simulator`, and
`authorized_queue_it_staging`. Enabling the last scope is rejected unless
`STATUS_DISCOVERY_CONFIRM_AUTHORIZED_STAGING=true`. That gate is a declaration of
operator authorisation, not evidence that a run occurred.

The observation dwell exists because the normal restorer can verify identity and park
the page before a periodic refresh occurs. It is diagnostic-only, bounded to at most
300 seconds, and holds normal browser capacity while enabled. It does not apply when
discovery is disabled.

## Evidence findings

No authorised Queue-it target or run was supplied for this prompt. Therefore the
Queue-it findings are:

| Question | Result | Basis |
|---|---|---|
| Was a periodic status-like request observed? | **UNKNOWN** | No Queue-it staging capture was run. |
| Was it clearly associated with the Queue-it visitor page? | **UNKNOWN** | No Queue-it staging capture was run. |
| Does the request contain Queue ID? | **UNKNOWN** | No Queue-it staging capture was run. |
| Are customerId/eventId observable? | **UNKNOWN** | No Queue-it staging capture was run. |
| Which URL components appear event-stable? | **UNKNOWN** | Requires authorised, repeated cross-session evidence. |
| Which values appear session-specific? | **UNKNOWN** | Requires authorised, repeated cross-session evidence. |
| Which values change per request? | **UNKNOWN** | Requires authorised browser evidence. |
| Are cookies changed by responses? | **UNKNOWN** | Requires authorised browser evidence. |
| Are request body/query values rotated? | **UNKNOWN** | Requires authorised browser evidence. |
| Does a response contain queue progress/status information? | **UNKNOWN** | Requires authorised browser evidence. |
| Does it contain redirect/admission information? | **UNKNOWN** | Requires authorised browser evidence. |
| Is replay safe to investigate? | **UNKNOWN** | Browser-runtime dependencies and replay sufficiency are untested. |

## Local mechanism evidence (not Queue-it)

A deliberately non-Queue-it local page emitted two arbitrary JSON `fetch` requests,
changed a cookie, and updated visible progress. The same observer seam captured and
correlated the exchanges through installed Chrome and Patchright. Unit fixtures also
cover redirects, request/response headers and bodies, query values, JSON, changing
cookies, bounded overflow/truncation, listener/worker cancellation, protected file
permissions, and suppression of sensitive values from normal structured logs.

These results prove only that the diagnostic mechanism works with the current browser
abstraction. They provide no fact about Queue-it's current visitor protocol.

## Validation

- Focused discovery/configuration/restoration/UI and backend tests: **115 passed**.
- Full non-staging suite: **566 passed, 4 staging deselected**.
- Ruff: **PASS**.
- Strict mypy: **PASS** (79 source files).
- Queue-it staging: **NOT RUN / UNKNOWN**.

## Next task

**Phase 8 Prompt 3 — Direct Request Replay and State Sufficiency Experiment.**

Prompt 3 must use genuine browser-observed evidence from an authorised target before
attempting replay. Production direct monitoring remains out of scope.
