# Phase 7 Prompt 3 — Patchright Park/Reopen and Queue Identity Strategy

**Date:** 2026-09-28  
**Decision:** **TEMPORARY_CONTEXTS_PASS**  
**Scope:** controlled local Queue simulator only  
**Staging:** **NOT RUN / UNKNOWN**

## Context strategy decision

Patchright passes this prompt with Quetty's existing bounded-process, disposable
`BrowserContext` architecture. No persistent user-data directory is needed or adopted.

The production-shaped path acquired and persisted one synthetic Queue identity, saved
its transfer URL and HYBRID storage state, completely closed every context, restored
the same expected Queue ID in fresh contexts, inspected progress, refreshed state,
and parked again. Twenty consecutive Patchright cycles passed. The sequence also
survived a full managed Patchright/Chrome process shutdown and relaunch and a separate
SQLite repository/state-store object restart.

The result does not weaken the identity invariant: the persisted Queue ID remained the
authority. Transfer URLs and storage state were used only to reconstruct and verify a
journey. An observed different Queue ID caused `IDENTITY_MISMATCH`; it was not adopted.

Patchright upstream's persistent-context example is useful for browser presentation
and profile continuity, but it does not match Quetty's need to park many sessions with
no live browser. Controlled evidence shows ordinary temporary contexts can restore the
required identity here. Per-session profiles would add lifecycle ownership, disk
cleanup, locking, profile-version, process-isolation, and concurrency questions without
solving a demonstrated failure, so they remain out of scope.

## Controlled method

`queue-load-test-phase7-patchright-identity` drives the real production creator,
restorer, browser manager, SQLite repository, and filesystem state store against
`LocalQueueSimulator` on `127.0.0.1`. It launches installed Chrome through Patchright,
uses one bounded managed browser slot, and creates a new temporary context for every
attempt. No Queue-it endpoint is contacted.

The committed aggregate result is
`docs/results/phase7_patchright_identity_result.json`. It contains no Queue IDs,
session IDs, transfer URLs, cookies, local-storage values, or browser-state documents.

Local environment:

- Python 3.14.7;
- Patchright 1.63.0;
- Playwright control 1.62.0;
- installed Google Chrome 153.0.8010.54.

## Restoration matrix

| Scenario | Patchright result | Identity/resource behavior |
|---|---|---|
| Fresh context and production-path acquisition | PASS | One identity persisted; acquisition context returned |
| Transfer URL into a fresh context | PASS | Expected Queue ID verified |
| `storage_state` into a fresh context | PASS | Expected Queue ID verified |
| HYBRID transfer first | PASS | Transfer is always attempted first |
| HYBRID storage fallback | PASS | Controlled HTTP 503 fell back and verified; refreshed state saved |
| Repeated park/reopen | PASS, 20/20 | Context count returned to zero after every cycle |
| Full managed browser-process restart | PASS | Restore succeeded after complete manager shutdown/relaunch |
| Full repository/state-store object restart | PASS | Persisted row and state restored with new objects |
| State refresh after verified restore | PASS | 22 measured Patchright refreshes succeeded |
| Missing state | `STATE_MISSING` | Existing Queue ID retained; no replacement |
| Corrupt state | `STATE_CORRUPT` | Existing Queue ID retained; no replacement |
| Unavailable/unreadable state | `STATE_UNAVAILABLE` | Existing Queue ID retained; no replacement |
| Transfer HTTP failure | `HTTP_FAILURE` | Existing session retained; HYBRID may fall back |
| Identity mismatch | `IDENTITY_MISMATCH` | Expected Queue ID retained; observed ID rejected |
| Backend provenance mismatch | `BACKEND_MISMATCH` | Rejected before context/state access; provenance retained |
| Context creation after manager shutdown | `NAVIGATION_FAILED` | Bounded failure; existing session retained |
| Navigation timeout | `NAVIGATION_FAILED` | Context returned; existing session retained |
| Recovery after injected failures | PASS | Same persisted session restored and state refreshed |
| Final context/process cleanup | PASS | Zero active contexts, managed processes, or new child Chrome processes |

The deterministic unit suite continues to cover additional restoration details such as
invalid/missing transfer URLs, state-save failure, bounded hung browser calls, and
TRANSFER_ONLY behavior. Prompt 3 adds real Patchright browser evidence for the
production-shaped mechanisms and failures above.

## Repeated-cycle and restart results

Patchright completed 20/20 cycles. Across the whole Patchright evidence run there were
32 restore operations and 36 mechanism attempts: 31 transfer attempts and five storage
attempts. Twenty-two transfer attempts and two storage attempts succeeded. The other
attempts were intentional fault injections. Restore duration was mean 0.1452 s, p50
0.1536 s, p95 0.2251 s, and maximum 0.2506 s.

The smaller installed-Chrome control completed 5/5 cycles, process restart, repository
restart, explicit transfer/state restores, and HYBRID fallback. Its eight operations
had mean restore duration 0.1619 s and p95 0.2368 s. This control is descriptive, not a
performance comparison: both samples are small local runs.

Both restart boundaries loaded the same persisted Queue ID. The workflow recorded zero
persisted identity changes and zero replacement identities. Failures were retried only
against the existing persisted session; the simulator issued no new identity after
initial acquisition.

## Failure and mismatch behavior

The Patchright fault matrix intentionally produced one observed identity mismatch.
That is a successful negative test, not silent identity loss: the result exposed
`identity_match=false`, repository row count did not change, the authoritative Queue ID
did not change, and a later clean restore of that same row succeeded.

Missing, corrupt, and unreadable state were tested after forcing transfer failure so
the HYBRID path reached storage fallback. Backend mismatch was tested with a separate
foreign-provenance fixture and opened no context. Context-creation failure and
navigation timeout were bounded and returned all capacity. No failure path created a
replacement identity.

## Resource cleanup

Patchright performed 33 explicit post-attempt context-cleanup checks with zero
failures. Before final shutdown it had zero active contexts. After shutdown it had zero
active contexts, zero BrowserManager processes, and zero new descendant Chrome main
processes relative to baseline. Chrome control had the same final zero counts.

## Acceptance decision and limits

**PASS for Phase 7 Prompt 3.** On controlled local evidence, temporary isolated
Patchright contexts repeatedly park, destroy, restore, verify, refresh, and re-park the
authoritative Queue identity. They survive browser-process and application/repository
restart, preserve identity under controlled failures, and release resources. There is
no evidence requiring persistent profile directories, so the architecture stays
unchanged.

This is not a claim about real Queue-it behavior, anti-detection, fingerprint
continuity, or staging reliability. Authorised staging was explicitly not run and
remains **UNKNOWN**.

## Validation

- Dedicated 20-cycle Patchright plus 5-cycle Chrome evidence workflow: PASS.
- Focused unit and installed-browser workflow tests: PASS.
- Full non-staging suite, Ruff, strict mypy, and `pip check`: recorded in
  `CHANGELOG_AI.md` after final validation.

## Next task

**Phase 7 Prompt 4 — Full Runtime and Dashboard Integration.**
