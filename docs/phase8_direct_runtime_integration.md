# Phase 8 Prompt 5 — Production Direct Monitoring with Browser Fallback

**Date:** 2026-09-29

**Implementation status:** COMPLETE (runtime integration, fallback, persistence, tests)

**Queue-it staging:** NOT RUN

**Queue-it direct equivalence:** UNKNOWN (unchanged from Prompt 4)

## Gate decision

Prompt 4 concluded **UNKNOWN / NOT READY**: no authorised discovery artifact, genuine
replay, or reviewed Queue-it response schema exists. The operator made the Prompt 4
equivalence condition optional for this prompt and asked for the integration to go
ahead.

The integration is therefore **evidence-gated per session** rather than trusted by
default:

- A direct request is made only for a session that has its *own* browser-observed
  status request (Prompt 2 evidence) whose captured response already parses, with the
  persisted Queue ID, through a reviewed response schema.
- A real (non-loopback) target accepts only `authorized_queue_it_staging` evidence and
  schema scope. `local_simulator` evidence is accepted only when the run itself targets
  a loopback simulator, so simulator evidence can never enable direct requests to a
  real event.
- With no schema configured (the state of this workspace), every Direct Monitoring
  Strategy check uses the browser fallback. Behavior is then the same as the Prompt 1–4
  runtime, plus a classified `schema_unavailable` fallback reason.

Nothing here is Queue-it evidence. Simulator runs are **not** Queue-it success.

## Routing

The run's persisted `run_config.monitoring_strategy` selects the handler once, at
runtime assembly (`_automatic_monitor_for_strategy`):

| Persisted strategy | Automatic monitoring | Refresh Now | Manual Open |
|---|---|---|---|
| `headed_window` — Headed Window Strategy | `QueueSessionMonitor` (unchanged): claim → browser restore → live inspect → persist → park → release | browser monitor (unchanged) | browser |
| `direct` — Direct Monitoring Strategy | `DirectMonitoringHandler`: claim → direct request → validate → normalize → persist → release; otherwise claim → direct attempt → classified failure → **the same** browser restore/DOM inspection → persist → park → release | the Direct handler, with the same fallback rules | browser |

The browser fallback is part of Direct Monitoring Strategy. It never changes the
persisted strategy: the run stays Direct in SQLite and on the dashboard however many
of its sessions currently need the fallback. A Direct run can no longer be assembled
without its direct handler.

The scheduler owns claim, lease, and release around `check()`, so both handlers inherit
the same due-session leases, owner-fenced writes, fixed worker pool, bounded queue and
claims, retry/backoff (browser path), overdue/backlog visibility, shutdown drain and
cancellation, and crash recovery. No database transaction is held across network or
browser work: the direct request runs between the claim transaction and the fenced
`update`.

A successful direct observation is persisted through
`QueueSessionMonitor.apply_direct_observation`, which shares the browser path's
progress-change tracking, Queue-it last-update handling, adaptive polling interval,
transition metric, and owner-fenced write (`_schedule_and_persist`). The browser path
was refactored onto the same helpers without changing its behavior.

## What a direct response may do

The one lifecycle evaluator (`evaluate_monitoring_observation`) decides status. A
direct observation is persisted only when all of these hold
(`validate_direct_observation`):

- the parsed Queue ID equals the persisted Queue ID (identity match);
- no contradictory markers (pre-queue with active or late-stage markers, admitted with
  expired);
- no expiry, redirect, or admission indicator;
- no connection-loss indicator;
- evaluated status is an in-queue state: PRE_QUEUE, ACTIVE_QUEUE, PAUSED,
  SERVICED_SOON, TURN_STARTED, or READY;
- the transition from the persisted status is legal.

So a direct field can never on its own mark a session ADMITTED, EXPIRED, or FAILED.
Admission and expiry are always confirmed by the browser monitor. Poll-timing hints are
parsed but not yet used to change cadence, because no evidence defines them; the
configured polling policy applies.

## Fallback classification

Every fallback is classified (`DirectFallbackReason`) and then runs the unchanged
browser monitor.

| Reason | Trigger | Capability effect |
|---|---|---|
| `no_queue_id` | session has no Queue ID | none (no request) |
| `schema_unavailable` | no valid reviewed schema accepted for this target | none (no request) |
| `discovery_required` | no recipe yet | none (no request) |
| `direct_unavailable` | session is DIRECT_UNAVAILABLE | none (no request) |
| `network` | connection error | soft |
| `timeout` | `DIRECT_MONITOR_TIMEOUT_SECONDS` exceeded | soft |
| `unexpected_http_status` | non-2xx other than 401/403/410 | soft |
| `unexpected_redirect` | any 3xx | hard |
| `unexpected_content_type` | non-JSON content type | hard |
| `malformed_response` | invalid or oversize JSON | hard |
| `schema_incompatible` | wrong types / non-object per reviewed schema | hard |
| `missing_visitor_state` | no usable browser state or cookies | hard |
| `rejected_session_state` | 401/403/410, or expiry indicated | hard |
| `identity_ambiguity` | Queue ID absent from response | hard |
| `identity_mismatch` | response Queue ID differs (client or parser) | hard |
| `unknown_lifecycle` | evaluator returns CHECKING, or connection loss | soft |
| `contradictory_lifecycle` | contradictory markers or an illegal transition | hard |
| `unsupported_admission` | redirect/admission indicated | soft |
| `recipe_uncertain` | recipe scope not accepted, loopback rule broken, corrupt record, record for another identity | hard |
| `uncertain` | any other exception in the direct path | soft |

*Hard*: the session becomes **DIRECT_UNAVAILABLE** at once. *Soft*: the recipe is kept,
and the session becomes DIRECT_UNAVAILABLE after `DIRECT_MONITOR_FAILURE_THRESHOLD`
consecutive failures. A success resets the count.

A direct failure never reacquires a Queue ID, replaces the expected identity, creates a
visitor, or makes another Queue ID authoritative. A record written for another Queue ID
is fenced (rewritten as DIRECT_UNAVAILABLE for the persisted identity) and never
reconciled.

## Per-session capability

`DirectCapability` is an internal implementation state, not an operator strategy:

- **DISCOVERY_REQUIRED** — no record yet; every check uses the browser.
- **DIRECT_CAPABLE** — a validated, browser-observed recipe exists; checks go direct.
- **DIRECT_UNAVAILABLE** — direct is disabled for the session until a new legitimate
  browser observation refreshes the recipe.

## Recipe refresh

After any Direct-run browser fallback that does not end terminal,
`DiscoveryRecipeHarvester` looks for a Prompt 2 artifact that is:

- for the same session,
- written after the fallback started,
- in an accepted scope, and
- for the persisted Queue ID (checked by `load_replay_recipe`).

It selects the newest status-candidate `xhr`/`fetch` exchange that has a 2xx
response, bodies that were not truncated, and a request carrying the Queue ID. The
captured response must parse through the reviewed schema with the persisted identity.
Only then is the exact request adopted (`adopt_recipe`). Cookies retained for the
previous recipe are discarded at that point, so the next direct check starts from the
browser's freshly saved state.

No recipe is ever constructed, guessed, taken from another session, or derived from
older evidence. Recipe refresh needs `STATUS_DISCOVERY_ENABLED=true`, which is already
Direct-run-only.

## Persistence

No SQLite schema changes. `DirectMonitorStateStore` keeps the minimum metadata in
protected files under `DIRECT_MONITOR_DIRECTORY` (default `.direct-monitor/`,
git-ignored, with an ignore-all marker):

- `records/<session>.json` — mode 0600 in a mode-0700 directory, SHA-256
  integrity-checked, bound to session and Queue ID. It holds capability, last reason,
  consecutive failures, update time, and the exact recipe (URL, method, headers, body,
  exchange sequence, identifiers, fingerprint). The recipe can carry credentials, so it
  is never stored in SQLite.
- `cookies/<session>.json` — the Prompt 3 protected response-cookie store, bound to the
  recipe fingerprint. Each check uses whichever is newer: these cookies or the browser
  `storage_state`. Direct responses never rewrite the browser-state document.

Delete, Replace (old session), Add/creation discard, and repeated Delete remove the
session's record and cookies. Stop & Reset Run clears the whole store. Normal logs carry
only session IDs, worker IDs, statuses, fallback reason values, and counts, never
URLs, headers, bodies, or cookies. Third-party `httpx` request lines are redacted by the
structured formatter.

## Pause/resume, Manual Open, Refresh Now, creation

- **Pause** is enforced by the scheduler before claim and before check start for both
  handlers. It stops new direct and browser automatic checks. In-flight checks finish,
  sessions stay persisted, backlog stays visible, and the pause survives restart.
  Resume continues from persisted due state.
- **Manual Open** stays browser-based for both strategies and uses the browser monitor
  for its final live-page evaluation. The persisted manual owner excludes the session
  from automatic claims, so a Direct run does not poll an open session. Close, window
  loss, and crash release ownership through the existing behavior.
- **Refresh Now** — chosen semantics: it uses the run's selected strategy. Under Direct
  it tries the direct path with the same browser-fallback rules, under Headed it is
  unchanged. It is still available while monitoring is paused and still reports busy
  during an automatic check, another refresh, or Open. The README had not defined
  Refresh as browser-only; its wording ("restores ... runs one normal monitor pass") now
  reads as "one normal pass of the run's monitoring strategy".
- **Creation, Add, and Replace acquisition** stay browser-based and unchanged. Direct
  monitoring is never a Queue ID acquisition path.

## Configuration

```text
DIRECT_MONITOR_DIRECTORY=.direct-monitor
DIRECT_MONITOR_SCHEMA_PATH=            # reviewed schema; unset => browser fallback only
DIRECT_MONITOR_TIMEOUT_SECONDS=10.0    # must be < MONITOR_LEASE_SECONDS
DIRECT_MONITOR_MAX_RESPONSE_BYTES=65536
DIRECT_MONITOR_FAILURE_THRESHOLD=3
STATUS_DISCOVERY_ENABLED=true          # needed for sessions to become DIRECT_CAPABLE
```

For a real target the schema document and discovery evidence must both have scope
`authorized_queue_it_staging`. Discovery with that scope still needs
`STATUS_DISCOVERY_CONFIRM_AUTHORIZED_STAGING=true`.

## Validation (local only; not Queue-it evidence)

- `tests/unit/test_direct_monitor.py`, 58 tests. They cover direct success, every
  fallback class (network, timeout including a real slow simulator, HTTP, redirect,
  content type, malformed, oversize, schema type, non-object, missing ID, mismatch,
  rejected, unknown/contradictory lifecycle, admission, missing state, backward
  transition, recipe scope/loopback uncertainty, corrupt record, other-identity
  record, unexpected error), hard/soft capability effects and threshold reset, schema
  unavailable, discovery → adoption, recovery only through a new observation, refusal
  of stale/other-identity/schema-failing/wrong-scope/remote-URL evidence, no refresh
  after terminal outcome, scheduler leases and bounds, pause/restart/resume, in-flight
  completion under pause, manual-open fencing, stale-worker fencing, shutdown
  cancellation, browser crash and restore failure during fallback, capability and
  cookie restart survival, protected permissions/integrity/cleanup, and
  SQLite/log secrecy.
- `tests/integration/test_phase8_direct_runtime.py` runs
  `queue-load-test-phase8-direct-runtime`, the complete application workflow for
  Direct Monitoring Strategy against `LocalQueueSimulator`'s page-polled JSON status
  endpoint.
  - Chrome: **35/35**. Patchright: **35/35**
    (`docs/results/phase8_direct_runtime_result.json`).
  - Checks cover:
    - browser creation and per-session recipe discovery;
    - direct progress and lifecycle updates without browser navigation;
    - twelve real fallback classes, each followed by browser monitoring with identity
      preserved;
    - recipe refresh and direct resumption;
    - the strategy staying Direct;
    - pause, Refresh Now direct and fallback, and resume;
    - Manual Open fencing and close;
    - Add capability, and Replace/Delete record cleanup;
    - bounded shutdown;
    - restart with an env default of Headed (the run stays Direct, no rediscovery);
    - Stop & Reset.
- Headed Window Strategy is unchanged. The existing Phase 5/6/7 application workflows
  (Chrome, Camoufox, Patchright) and all monitoring, UI, and operator suites pass.
- Full non-staging suite: **662 passed, 4 staging deselected**. Ruff: **PASS**. Strict
  mypy: **PASS** (99 source files).

## Known issues / Prompt 6 inputs

- A persistent direct-only fault (for example a server that rejects only replays) makes
  every check try direct, fall back, and re-adopt. That doubles per-check cost for the
  session. Add re-adoption backoff or fingerprint quarantine.
- Direct metrics exist in-process only (`DirectMonitoringMetrics`, structured logs).
  There are no Prometheus series or dashboard counts yet.
- Discovery artifacts still accumulate for every Direct-run fallback while discovery is
  enabled. Retention and pruning are not implemented.
- Replay-refreshed cookies never flow back into browser `storage_state`. If Queue-it
  rotates visitor cookies, a later browser fallback uses older browser state. This is
  UNKNOWN for Queue-it.
- The response-schema and recipe-review workflow (who approves a schema and how) is
  documentation only.
- Every Queue-it question from Prompts 2–4 remains **UNKNOWN**.

## Next task

**Phase 8 Prompt 6 — Direct Monitoring Security, Observability, and Failure Hardening.**
