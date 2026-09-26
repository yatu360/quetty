# Phase 2 Concurrency Tuning Benchmark

## Current Result

**NOT RUN** as of 2026-09-26. The authorised staging gates and configuration were not
available during implementation. There are no measured throughput, latency, resource,
reliability, one-browser/two-browser, or saturation observations.

## Purpose

The harness gathers objective evidence while bounded concurrency increases. It does
not choose a winner or an “optimal” configuration. Cases run sequentially; matrix size
does not become simultaneous task, browser, or context count.

The built-in matrix covers maximum active-context levels 5, 10, 15, 20, and 25 for the
currently configured Chrome-process count. `--chrome-processes 1,2` generates both
process variants. For completely explicit worker and queue settings, use
[`benchmarks/phase2-concurrency-matrix.example.json`](../benchmarks/phase2-concurrency-matrix.example.json)
as a `--matrix-file` or copy and edit it.
The ten-case example acquires a separate 100-session population per case; it is not a
1,000-session population test. Run only the explicitly authorised subset when the
staging event cannot accept the full cumulative test volume.

Each case uses its own SQLite database and browser-state directory. It runs bounded
100-session acquisition and monitoring, then optionally probes transfer-only and
storage-state-only restoration. The aggregate matrix report excludes Queue IDs,
session IDs, transfer URLs, and browser state. Case work directories are sensitive
because they contain the SQLite database, state files, and an identity-audit restore
report; the default work root is git-ignored.

## Execution

```powershell
python -m pip install -e ".[test,benchmark]"
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE2_CONCURRENCY_BENCHMARK = "1"
$env:TARGET_QUEUE_IDS = "100"
$env:SESSION_MODE = "HYBRID"
queue-load-test-phase2-tuning --confirm-authorized-staging `
  --matrix-file benchmarks/phase2-concurrency-matrix.example.json `
  --monitoring-seconds 600 `
  --sample-interval-seconds 5 `
  --restore-sample-size 10 `
  --report phase2-concurrency-benchmark.json
```

Use a new `--work-directory` for every repetition. The output includes full per-case
resource data, non-sensitive transfer/storage aggregate reliability and latency,
comparison rows, and configurable saturation flags. Flags cover adjacent p95 latency,
failure rate, CPU, RAM growth, browser crashes, identity mismatches, continuously
increasing backlog, restore reliability, and BrowserManager context-acquisition time.
They are observations requiring interpretation, not automatic configuration choices.

The results apply only to the tested authorised Phase 2 host and event. They must not
be extrapolated to 1,000 or 10,000 sessions.
