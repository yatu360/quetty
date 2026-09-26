# Phase 2 Resource and Stability Benchmark

## Current Result

**NOT RUN** as of 2026-09-26. No authorised staging benchmark was executed while this
harness was implemented. CPU/RAM, throughput, latency, failure, and one-browser versus
two-browser comparisons therefore remain **UNKNOWN**.

## Harness

`queue-load-test-phase2-resources` creates the configured 100-session HYBRID population
in a dedicated empty SQLite database, then runs the existing bounded parked-session
scheduler for a controlled observation window. It samples the application and Chrome
process tree at a fixed interval while reading the existing aggregate Prometheus
signals. `psutil` is optional; without it the report retains browser-manager and
application metrics while host/process fields are `null`.

The report includes the exact non-sensitive configuration, duration, interval samples,
average/peak CPU and RAM, process/context peaks, optional file-descriptor observations,
queue/backlog peaks, aggregate failures, throughput, and average/p50/p95 operation
latencies. It contains no Queue IDs, session IDs, transfer URLs, or browser state.

Both explicit environment gates and the confirmation flag are required. Use separate
empty database and state locations for each run. For an equivalent two-process profile,
set `MAX_CONTEXTS_PER_BROWSER=13` so the combined capacity covers the global limit of
25.

```powershell
python -m pip install -e ".[test,benchmark]"
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE2_RESOURCE_BENCHMARK = "1"
$env:TARGET_QUEUE_IDS = "100"
$env:SESSION_MODE = "HYBRID"
$env:MAX_ACTIVE_CONTEXTS = "25"

# Run 1: one Chrome process
$env:CHROME_PROCESS_COUNT = "1"
$env:MAX_CONTEXTS_PER_BROWSER = "25"
$env:DATABASE_URL = "sqlite:///phase2-resource-1.sqlite3"
$env:STATE_DIRECTORY = ".browser-state-resource-1"
queue-load-test-phase2-resources --confirm-authorized-staging --report phase2-resource-benchmark-1.json

# Run 2: two Chrome processes, otherwise equivalent
$env:CHROME_PROCESS_COUNT = "2"
$env:MAX_CONTEXTS_PER_BROWSER = "13"
$env:DATABASE_URL = "sqlite:///phase2-resource-2.sqlite3"
$env:STATE_DIRECTORY = ".browser-state-resource-2"
queue-load-test-phase2-resources --confirm-authorized-staging --report phase2-resource-benchmark-2.json
```

The output is evidence for only the authorised host, event, and Phase 2 configuration.
It must not be used to predict Phase 3 or Phase 4 performance.
