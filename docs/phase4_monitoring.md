# Phase 4 10,000-Session Monitoring Report

**Date:** 2026-09-27  
**Deployment model:** single machine, SQLite, local state storage  
**Authorised Queue-it run:** **NOT RUN / UNKNOWN**

## Outcome

The local bounded scheduler and SQLite persistence layer processed a 10,000-row
synthetic population without creating population-sized task or browser sets. Two
deliberate all-due sweeps and one adaptive-schedule simulation completed with zero
failed checks, zero lease conflicts, a maximum queue depth of 50, and at most 20 active
workers. The benchmark created no BrowserContexts, which is evidence of parked-session
behavior, not evidence of browser restoration capacity.

There is no `.env`, authorised staging gate, application session database, or real
10,000-identity population in this checkout. Transfer restore, storage-state restore,
identity continuity, Chrome stability, navigation, real check duration, and sustainable
Queue-it cadence therefore remain **UNKNOWN**.

The complete aggregate machine-readable result is
[`phase4_monitoring_result.json`](results/phase4_monitoring_result.json). The normal
runner also writes a detailed, git-ignored JSON report containing per-batch adaptive
checkpoints.

## Configuration

| Setting | Value |
|---|---:|
| Persisted synthetic sessions | 10,000 |
| Fixed monitoring workers | 20 |
| Bounded queue capacity | 50 |
| Claim batch | 50 |
| Lease duration | 120 seconds |
| Synthetic handler delay | 1 ms |
| Resource sample interval | 1 second |
| Browser processes | 0 (synthetic run) |
| BrowserContexts | 0 (synthetic run) |

SQLite seeding used 10,000 separately committed repository creates and took 35.746
seconds. Seeding is excluded from sweep duration.

## Scenario A - Deliberate Full Sweeps

All 10,000 non-terminal sessions were deliberately due. A fixed scheduler drained the
population, updated each row, and released each lease. A second sweep verified that the
same persisted population can be re-parked and processed again.

| Metric | Sweep 1 | Sweep 2 |
|---|---:|---:|
| Sessions due / checked | 10,000 / 10,000 | 10,000 / 10,000 |
| Failed checks | 0 | 0 |
| Duration | 76.595 s | 92.241 s |
| Throughput | 130.56/s | 108.41/s |
| Check p50 | 75.20 ms | 92.05 ms |
| Check p95 | 106.90 ms | 124.63 ms |
| Check p99 | 133.71 ms | 154.49 ms |
| Scheduler aggregate query p95 | 98.01 ms | 115.05 ms |
| Claim p95 | 100.06 ms | 119.77 ms |
| Update p95 | 102.96 ms | 119.80 ms |
| Lease release p95 | 95.87 ms | 116.13 ms |
| Queue-depth peak | 50 | 50 |
| Active-worker peak | 20 | 20 |
| Backlog start -> end | 10,000 -> 0 | 10,000 -> 0 |
| Oldest overdue start -> end | 120 s -> 0 s | 1 s -> 0 s |
| Lease conflicts | 0 | 0 |

The latency values above are end-to-end async repository-call latency during concurrent
writes; they include waiting for SQLite's serialized executor. They should not be
compared directly with the isolated Phase 4 repository microbenchmark.

Measured synthetic throughput implies a 10,000-row scheduler/persistence sweep of
about 76.6-92.2 seconds for this handler and host. This is a measured-throughput-derived
synthetic estimate, not a Queue-it browser sweep estimate.

## Scenario B - Adaptive Scheduling and Jitter

The benchmark generated 10,000 `next_check_at` values through the production
`PollingPolicy`, using a deterministic mix of pre-queue, early active, mid active,
serviced-soon, and turn-started signals. Simulated time advanced by actual batch
processing duration, so work becoming due while an earlier batch ran accumulated in
the visible backlog.

| Metric | Result |
|---|---:|
| Sessions scheduled / checked | 10,000 / 10,000 |
| Successful / failed | 10,000 / 0 |
| Processing wall time | 93.185 s |
| Processing throughput | 107.31/s |
| Original due-time window | 184.996 s |
| Last due work completed | simulated +196 s (checkpoint floor) |
| Distinct exact due times | 9,801 |
| Largest one-second due bucket | 439 |
| Maximum visible due backlog | 2,885 |
| Maximum oldest-overdue age | 26.599 s |
| Ending backlog / oldest age | 0 / 0 s |
| Queue-depth / active-worker peak | 50 / 20 |
| Lease conflicts | 0 |
| Check p50 / p95 / p99 | 78.13 / 103.55 / 138.93 ms |
| Due aggregate query p95 | 2.312 ms |
| Claim p95 | 11.750 ms |

Jitter prevented a single 10,000-session synchronization spike, but it did not prevent
all short-term backlog: the adaptive mix produced a maximum 2,885 due rows and 26.599
seconds of oldest-overdue age before draining to zero. This is now observable rather
than silently dropped. The result does not prove the actual Queue-it population will
have the same lifecycle mix or timing distribution.

## Resources

The Python benchmark process averaged 59.00% CPU and peaked at 81.8% according to
`psutil`. RSS averaged 68,082,768 bytes (64.93 MiB) and peaked at 76,541,952 bytes
(73.00 MiB). There were 308 one-second samples. Chrome CPU/RAM and browser failure
metrics were zero/not applicable because no Chrome process was launched.

## Browser and Restore Evidence

| Measurement | Result |
|---|---|
| Active-context peak | 0 (synthetic handler) |
| Transfer restore success | UNKNOWN / NOT RUN |
| `storage_state` restore success | UNKNOWN / NOT RUN |
| Identity mismatches | UNKNOWN / NOT RUN |
| Browser crashes | UNKNOWN / NOT RUN |
| Navigation failures | UNKNOWN / NOT RUN |
| Context acquisition wait | UNKNOWN / NOT RUN |

An active-context peak of zero only confirms that scheduler-scale testing did not keep
10,000 browsers alive. It does not validate the expected bounded Chrome path.

## Cadence Assessment

**Real Queue-it monitoring cadence: UNKNOWN.** The local scheduler and SQLite layer can
drain the synthetic 10,000-row workload in roughly 77-92 seconds for a full all-due
sweep, and the tested adaptive distribution ended at simulated +196 seconds with no
remaining backlog. Browser restore, navigation, live DOM evaluation, state refresh,
and network latency were not exercised and will dominate a real check. No claim is made
that every session can be checked every 30 seconds.

The scheduler never dropped due work. Backlog count, overdue count, oldest-overdue age,
queue depth, and active-worker gauges are now available without session or Queue ID
labels.

## Reproduction

```powershell
python -m pip install -e ".[test,benchmark]"
queue-load-test-phase4-monitoring `
  --database phase4-monitoring-synthetic.sqlite3 `
  --report phase4-monitoring-benchmark.json `
  --population 10000 --workers 20 --queue-capacity 50 --batch-size 50 `
  --sweeps 2 --check-delay-seconds 0.001 --sample-interval-seconds 1
```

Use a new empty database path for every run. This command is local and synthetic; it
does not send staging traffic.

## Remaining Unknowns

- A real 10,000-session Queue-it population was not acquired or monitored.
- The tested adaptive distribution is deterministic synthetic input, not observed
  staging `next_check_at` data.
- Transfer and storage-state restoration rates and latencies are unknown.
- Real context acquisition, Chrome CPU/RAM, crashes, navigation failures, page cadence,
  and identity mismatches are unknown.
- No numeric product/service-level monitoring cadence has been supplied against which
  the real browser path can be accepted.
- Single-machine sufficiency for live Queue-it monitoring is not established.

