# Phase 8 Prompt 1 — Monitoring Strategy Boundary and Setup UI

**Date:** 2026-09-29  
**Status:** COMPLETE (configuration, UI, provenance, and dispatch boundary)  
**Staging:** NOT RUN / UNKNOWN

## Decision

Automatic monitoring is now an immutable run-level choice with two stable persisted
values:

| Persisted value | Operator label | Prompt 1 behavior |
|---|---|---|
| `headed_window` | Headed Window Strategy | Existing browser-backed restore, live page/DOM inspection, persistence, and park path |
| `direct` | Direct Monitoring Strategy | Browser-established sessions with direct visitor-status checking where available and browser fallback; Prompt 1 uses the existing browser fallback for every check |

The operator-facing “Headed Window” name does not change the existing automatic
browser/context visibility. `HEADLESS` continues to control the automatic pool, and
Manual Open remains the explicit headed operator window.

## Boundary and provenance

`MonitoringStrategy` is a typed value on `Settings` and immutable `RunConfig`.
`MONITORING_STRATEGY` supplies only the default selected on a fresh setup page. The
submitted choice is stored in `run_config.monitoring_strategy`; startup reads that
persisted value and overwrites the runtime settings copy with it. Changing the
environment cannot migrate an existing run.

The SQLite change is additive. Databases whose `run_config` predates Phase 8 receive:

```sql
monitoring_strategy TEXT NOT NULL DEFAULT 'headed_window'
```

That is their historical behavior. No session row, Queue ID, transfer URL, browser
state, browser-backend provenance, or lifecycle field is rewritten. Strategy and
browser backend are independent dimensions. Changing either requires Stop & Reset Run
and a new setup submission.

Runtime assembly has an explicit strategy selector. In Prompt 1 both values select the
existing `QueueSessionMonitor`; for `direct`, this is the documented browser fallback,
not direct request replay. This preserves the existing restore, Queue ID verification,
DOM inspection, polling, persistence, leasing, fencing, timeout, and parking behavior.
Prompt 2 may add a direct implementation beneath this selector without adding
per-session switching.

## Operator UI

First-run setup shows the target URL, requested session count, configured browser
backend, and both monitoring strategies with their exact labels. The warning that the
target is the protected destination—not the Queue-it waiting-room URL—remains. Unknown
strategy values return HTTP 422 and create no run.

The dashboard shows the persisted strategy label in run information. Manual Open,
Refresh, Add, Replace, Delete, and pause/resume retain their existing behavior under
both selections. Pause continues to block automatic claims only.

## Safety boundary

Prompt 1 does not inspect browser traffic, discover or construct a Queue-it status URL,
replay a visitor request, migrate/reacquire Queue IDs, or change backend certification.
Patchright remains the default for new runs, Chrome the supported fallback, and
Camoufox retained under the Phase 7 experimental/uncertified policy. No stealth,
evasion, proxy rotation, CAPTCHA solving, or fingerprint spoofing was added.

## Verification

Focused tests cover both setup selections, persistence and restart, default-change
immunity, legacy migration, reset-and-reselect, backend/strategy independence, the
unchanged browser monitor dispatch, Manual Open, pause/resume, and invalid input.

- Focused configuration/repository/UI suite: **112 passed**.
- Full normal suite: **556 passed, 4 staging deselected**.
- Ruff: **PASS**.
- Strict mypy: **PASS** (77 source files).

Queue-it staging was **NOT RUN**. Local tests establish only the configuration,
persistence, UI, and existing browser-fallback behavior described above.

## Next task

**Phase 8 Prompt 2 — Browser-Observed Visitor Status Discovery.**
