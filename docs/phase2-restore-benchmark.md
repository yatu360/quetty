# Phase 2 HYBRID Restore Benchmark

## Status

**NOT RUN / UNKNOWN** as of 2026-09-26.

The repeatable benchmark harness and deterministic aggregation tests are implemented,
but this repository contains no authorised real-staging Phase 2 restore result. The
required `RUN_STAGING_TESTS` and `RUN_PHASE2_RESTORE_BENCHMARK` gates were not set while
implementing this prompt.

## Scope

The benchmark selects a configurable sample of existing HYBRID sessions from SQLite
and can run:

- official Queue-it transfer restoration only;
- Playwright `storage_state` restoration only;
- production HYBRID transfer-first/storage-state-fallback behavior.

The default `all` mode performs three sequential restore invocations per selected
session. It records expected/observed identity, sanitized outcomes, lifecycle status,
duration, mechanism attempts, fallback use, and browser/context failure classification.
It does not include transfer URLs or browser-state contents.

## Authorised Command

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE2_RESTORE_BENCHMARK = "1"
queue-load-test-phase2-restore --confirm-authorized-staging --sample-size 100 --mode all --report phase2-restore-benchmark.json
```

The configured database must already contain the requested number of HYBRID sessions.
The JSON report contains Queue IDs for identity auditing and must be handled as
sensitive local staging evidence. Its default filename is git-ignored.

## Results

| Measure | Result |
|---|---:|
| Restore invocations | NOT RUN |
| Overall success rate | UNKNOWN |
| Transfer reliability | UNKNOWN |
| Storage-state reliability | UNKNOWN |
| Fallback usage | UNKNOWN |
| Identity mismatches | UNKNOWN |
| Average duration | UNKNOWN |
| p50 duration | UNKNOWN |
| p95 duration | UNKNOWN |

Deterministic unit tests validate report aggregation, failures, identity mismatch and
fallback accounting, percentile calculations, mode selection, explicit staging gates,
and omission of transfer URLs. They are not evidence of real Queue-it reliability.
