# Phase 3 Failure Recovery and Restart

## Scope

This evidence combines a 1,000-session synthetic restart benchmark with focused tests
of the existing creation, monitoring, restoration, browser, repository, and state-store
boundaries. No authorised Queue-it staging configuration was available, so no real
Queue-it identity or real Chrome crash/restart scenario was run.

The benchmark population contained 1,000 persisted sessions: 960 valid unique Queue
IDs, 50 active leases, 50 expired leases, 800 claimable due sessions, 50 sessions
requiring retry, and 100 terminal sessions. It also stored 960 correctly associated
HYBRID state files; the 40 terminal failed-creation rows intentionally had no Queue ID
or required state file.

## Controlled Result

Five restart cycles reopened SQLite, ran the aggregate startup query, and independently
recomputed a digest over every stable `session_id`, Queue ID, transfer URL, and state
path. Identity and terminal-state snapshots remained unchanged on every cycle.

| Measurement | Result |
|---|---:|
| Persisted sessions | 1,000 |
| Valid Queue IDs before / after | 960 / 960 |
| Restart repetitions | 5 |
| Startup aggregation average | 0.834 ms |
| Startup aggregation p50 / p95 / max | 0.827 / 0.907 / 0.925 ms |
| Active / expired leases before recovery | 50 / 50 |
| Expired leases recovered | 50 |
| Resumed checks | 50 |
| Scheduler workers | 5 |
| Queue peak / capacity | 50 / 50 |
| Due sessions before / after one batch | 800 / 750 |
| Missing / corrupt required state | 0 / 0 |
| Identity and terminal state preserved | yes |

The expired leases had deliberately earlier due times and were the exact 50 rows claimed
after restart. Active unexpired leases were not stolen. The scheduler updated and
released the recovered rows, retained the existing Queue IDs, and did not invoke the
creation controller.

## Scenario Outcomes

| Recovery scenario | Synthetic outcome | Real staging |
|---|---|---|
| Shutdown during creation | PASS — bounded in-flight work drains or cancels; restart counts committed IDs and creates only the deficit | UNKNOWN |
| Shutdown during monitoring | PASS — active/queued leases release on graceful or timed cancellation; identities persist | UNKNOWN |
| Restart with 1,000 parked/mixed sessions | PASS — five idempotent reopen cycles | UNKNOWN |
| Expired worker leases | PASS — all 50 recovered in one bounded batch; active leases untouched | UNKNOWN |
| Chrome process crash | PASS with controlled browser doubles — only the failed slot is replaced and lost contexts close | UNKNOWN |
| Context creation/scope failure | PASS with controlled browser doubles — capacity is released and cleanup is idempotent | UNKNOWN |
| Transfer restore failure | PASS — sanitized failure persists against the existing session; no replacement session is created | UNKNOWN |
| `storage_state` restore failure | PASS — explicit state failure is returned and the existing Queue ID is retained | UNKNOWN |
| Corrupt state file | PASS — detected by restore and consistency scan; no partial replacement or deletion | UNKNOWN |
| Missing state file | PASS — reported explicitly; recoverable retry remains observable | UNKNOWN |
| Identity mismatch | PASS — expected Queue ID remains unchanged and exactly one session remains | UNKNOWN |
| Partial 1,000-ID run | PASS — 613 existing valid IDs produce exactly 387 new successes; satisfied targets create nothing | UNKNOWN |

`PASS` here means deterministic local behavior using synthetic identities and controlled
browser/restoration doubles. It does not establish real Queue-it recovery reliability.

## Startup Summary

`SQLiteSessionRepository.recovery_summary()` returns total sessions, valid Queue IDs,
active leases, expired leases, due sessions, retry candidates, terminal sessions, and
per-status counts using one aggregate SQLite query. `ApplicationRuntime` records this
summary before starting Chrome and initializes lifecycle gauges from it. `/status` now
uses the same aggregate path instead of loading every session row into Python.

Normal startup intentionally leaves missing/corrupt state counts as `None`, meaning
“not scanned.” `StateConsistencyChecker.recovery_summary()` is the explicit, more
expensive path that combines aggregate database counts with a full state-directory
audit. This keeps the normal startup cost bounded while retaining an operator-visible
integrity report when required.

## Unresolved Risks

- Real Queue-it transfer continuity, browser crash recovery, identity mismatch behavior,
  and restart timing remain UNKNOWN because staging was not configured.
- A hard process kill leaves leases until their configured expiry; it cannot perform the
  graceful release path. Recovery is therefore bounded by `LEASE_SECONDS`.
- SQLite remains single-host and serializes repository operations per instance. No
  multi-node ownership or node-loss claim is made.
- Abrupt power loss, disk exhaustion, OS/filesystem corruption, and failures between
  external Queue-it identity acquisition and local transaction commit remain untested.
- Failed creation attempts are terminal audit rows; the target controller creates new
  bounded attempts only for the remaining successful-ID deficit. It never rewrites a
  previously successful identity.
