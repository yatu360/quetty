# Phase 3 Monitoring-Sweep Benchmark

## Scope and Definition

This benchmark exercises 1,000 persisted parked sessions through the real SQLite due
query, transactional leases, bounded scheduler queue, fixed worker pool, persistence
updates, and lease releases. It uses a synthetic check handler and does not restore or
navigate Queue-it sessions.

A **full sweep duration** is wall time from starting scheduling with all 1,000 benchmark
sessions due until every session has completed its synthetic check, persistence update,
and lease release. Initial database seeding is excluded. This intentionally measures a
synchronized worst-case due set and must not be confused with normal adaptive polling.

The benchmark then assigns each session a deterministic jittered `next_check_at` over a
ten-second interval and advances simulated scheduling time through that window. Every
due cohort is processed rather than dropped, and backlog is measured after each
checkpoint.

## Local Synthetic Result

Run on 2026-09-26:

```text
.venv/bin/python -m queue_load_test.harness.phase3_monitoring \
  --database <empty-temporary-directory>/monitoring.sqlite3 \
  --report phase3-monitoring-benchmark.json \
  --workers 20 --queue-capacity 50 --batch-size 50 --sweeps 2 \
  --check-delay-seconds 0.001 --sample-interval-seconds 0.01
```

The one-millisecond delay is synthetic and only ensures asynchronous worker overlap.
It does not model Queue-it navigation or restoration latency.

| Measure | Sweep 1 | Sweep 2 |
|---|---:|---:|
| Persisted / initially due | 1,000 / 1,000 | 1,000 / 1,000 |
| Checked / successful / failed | 1,000 / 1,000 / 0 | 1,000 / 1,000 / 0 |
| Full sweep duration | 1.0582 s | 1.0557 s |
| Synthetic checks/s | 944.98 | 947.26 |
| Average check duration | 11.05 ms | 11.38 ms |
| p50 check duration | 13.25 ms | 13.14 ms |
| p95 check duration | 14.64 ms | 14.33 ms |
| Backlog start / end | 1,000 / 0 | 1,000 / 0 |
| Queue peak / worker peak | 50 / 20 | 50 / 20 |
| Lease conflicts | 0 | 0 |

The second synchronized sweep returned to the same zero backlog, so this synthetic
configuration did not accumulate work across repeated sweeps. It proves bounded local
scheduler throughput for this handler and host only.

The staggered pass checked all 1,000 sessions with zero failures and ended with zero
backlog. The generated offsets ranged from 25.007 to 34.982 seconds, all 1,000 exact
timestamps were distinct, and the largest one-second bucket contained 128 sessions.
Every checkpoint drained its due cohort before simulated time advanced. This verifies
that configured jitter spreads scheduling and that due work is processed rather than
hidden or discarded.

## Local Resources

During the complete two-sweep plus staggered run:

- application CPU averaged 85.21% and peaked at 114.3%, where 100% is one logical core;
- application RSS averaged 59,745,834 bytes and peaked at 60,882,944 bytes;
- sampled scheduler backlog averaged 262.64 and peaked at 950 after the first bounded
  claim;
- sampled queue depth averaged 20.56 and peaked at its configured bound of 50;
- active workers averaged 9.65 and peaked at the fixed bound of 20;
- Chrome process count, Chrome CPU/RAM, and active BrowserContexts remained zero because
  this was deliberately a browser-free synthetic benchmark.

## Queue-it / Browser Evidence

Authorised staging monitoring is **NOT RUN**. Therefore real Queue-it checks/s, check
latency, full-sweep duration, context-acquisition wait, active-context peak, restore
failures, identity mismatches, browser crashes, navigation failures, and Chrome CPU/RAM
remain **UNKNOWN**. The synthetic zeros for browser activity are not real monitoring
success results.

The machine-readable JSON is git-ignored and contains no Queue IDs, session IDs,
transfer URLs, or browser state. Results do not establish 10,000-session capacity.
