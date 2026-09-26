# Phase 3 Browser Capacity Benchmark

## Scope

This benchmark compares three bounded installed-Google-Chrome configurations. It does
not choose an optimal value, establish Queue-it staging safety, or extrapolate to
10,000 persisted sessions. All cases preserve 25 contexts per Chrome process and run
sequentially:

| Case | Active contexts | Chrome processes | Contexts per process |
|---|---:|---:|---:|
| `p2-c50` | 50 | 2 | 25 |
| `p3-c75` | 75 | 3 | 25 |
| `p4-c100` | 100 | 4 | 25 |

The harness uses a fixed allocation worker pool and a bounded work queue. It records
context creation, total acquisition and manager-lock wait latency, navigation latency,
resource samples, failures, process accounting, and cleanup. `BrowserManager` still
launches installed Google Chrome with `channel="chrome"` and closes every context and
process after each case.

## Local Run

Run on 2026-09-26 on a 15-logical-CPU, 25,769,803,776-byte RAM macOS host:

```text
.venv/bin/python -m queue_load_test.harness.phase3_browser_capacity \
  --hold-seconds 2 --sample-interval-seconds 0.25 --allocation-workers 10 \
  --report phase3-browser-capacity-benchmark.json
```

Navigation used an in-memory `data:` page, not Queue-it. The JSON report is ignored by
Git and contains the raw samples and comparison rows.

| Case | Achieved | p95 create | p95 acquire | p95 lock wait | p95 navigation | Chrome CPU avg/peak | Chrome RSS avg/peak | Failures |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `p2-c50` | 50/50 | 0.100 s | 0.384 s | 0.320 s | 0.377 s | 153.1% / 601.9% | 13.38 GB / 17.80 GB | 0 |
| `p3-c75` | 75/75 | 0.118 s | 0.304 s | 0.243 s | 0.511 s | 206.5% / 490.0% | 16.75 GB / 22.14 GB | 0 |
| `p4-c100` | 100/100 | 0.112 s | 0.262 s | 0.189 s | 0.635 s | 293.2% / 553.1% | 20.64 GB / 27.92 GB | 0 |

CPU values are aggregate process-tree percentages, where 100% represents one logical
core. RSS is summed across Chrome processes and may count shared pages more than once;
it is an upper-bound pressure indicator, not unique physical memory. Summed peak
application-plus-Chrome RSS crossed the configured 80%-of-host indicator at 75 and 100
contexts. No CPU-capacity symptom, browser crash, context creation failure, navigation
failure, cleanup failure, allocation stall, or adjacent 1.5x latency increase was
observed.

All three local cases completed and returned active-context capacity to zero. This is
evidence that the installed browser can complete the short synthetic cases, not that
75 or 100 contexts are safe for sustained Queue-it workloads. Of the tested values,
only 50 avoided the conservative summed-RSS pressure indicator in this run.

## Authorised Staging

Queue-it staging execution is **NOT RUN**. No configured `.env` or explicit staging
authorization gates were available. A future authorised run requires all of:

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE3_BROWSER_BENCHMARK = "1"
queue-load-test-phase3-browser-capacity --staging --confirm-authorized-staging --report phase3-browser-capacity-benchmark.json
```

Until that run exists, Queue-it navigation latency, lifecycle behavior, long-duration
resource stability, crash recovery under real pages, and a staging-safe context range
remain **UNKNOWN**.
