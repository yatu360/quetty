# Phase 1 acceptance report

Scope: the deterministic local run uses 10 HYBRID sessions, one installed
Google Chrome process, at most five active BrowserContexts, SQLite, atomic local
JSON storage state, and browser-rendered DOM monitoring. It does not contact a
real Queue-it environment. `PASS` below means the Phase 1 mechanism passed that
controlled run; vendor- and event-specific behavior remains an explicit staging
assumption.

| # | Result | Critical question | Controlled evidence |
|---:|:---:|---|---|
| 1 | PASS | Does each fresh BrowserContext receive an independent Queue ID? | Ten fresh contexts received ten distinct simulator-issued identities. |
| 2 | PASS | Does a Queue ID already exist during PRE_QUEUE? | Each PRE_QUEUE page exposed its identity through the supported transfer control. |
| 3 | PASS | Does PRE_QUEUE transition to ACTIVE_QUEUE while keeping the same Queue ID? | The same identity was verified after the controlled transition. |
| 4 | PASS | Can the official Queue-it transfer URL be extracted reliably? | The supported page-exposed transfer control was extracted for all ten controlled sessions; the real staging theme remains to be validated. |
| 5 | PASS | Can the transfer URL restore the same Queue-it journey? | A fresh context restored and verified the expected identity. |
| 6 | PASS | Can Playwright `storage_state` restore the same journey? | Saved JSON state restored the expected identity without sharing the original context. |
| 7 | PASS | Can progress percentage be read reliably? | The live DOM `aria-valuenow` value was read after JavaScript/DOM rendering. |
| 8 | PASS | Does Queue-it `lastUpdated` continue changing appropriately? | A changed timestamp was observed after a controlled page update; real cadence is still unknown. |
| 9 | PASS | Can `SERVICED_SOON` be detected? | The controlled live DOM produced `SERVICED_SOON`. |
| 10 | PASS | Can `TURN_STARTED` be detected? | The controlled live DOM produced `TURN_STARTED`, separately from admission. |
| 11 | PASS | Can final `ADMITTED` state be detected? | Normal navigation to the configured protected path was detected. |
| 12 | PASS | Does restart/recovery preserve existing sessions? | SQLite was closed and reopened; all ten identities and saved state references remained available. |

The ordinary suite also covers navigation failure/backoff, Chrome-context crash
recovery, corrupt or missing state, transfer failure, identity mismatch,
bounded capacity, graceful shutdown, and restart recovery. The staging harness
records session creation, context creation, navigation, restoration, and
monitoring latency; transfer and storage-state restore success rates; process
CPU/RAM when available; browser crashes; navigation failures; and identity
mismatches.

No staging performance numbers are reported here because no authorised staging
URL or run window was supplied. The generated `phase1-acceptance.json` is the
source of truth for a real run.

## Assumptions to validate before Phase 2

- Run all ten sessions against the authorised staging event through its timed
  PRE_QUEUE, ACTIVE_QUEUE, SERVICED_SOON, TURN_STARTED, and admission stages.
- Confirm the staging theme exposes one of the configured supported transfer
  controls and preserves the expected Queue ID for both restore paths.
- Measure the real Queue-it `lastUpdated` cadence and choose a sufficiently long
  observation window.
- Confirm the configured protected host/path uniquely represents admission.
- Capture CPU and RAM on the intended deployment host; the controller-process
  RAM observation is `null` when an optional local process sampler is unavailable.
- Treat these results as evidence only for the 10-session Phase 1 profile. They
  do not generalize to 100, 1,000, or 10,000 sessions.
