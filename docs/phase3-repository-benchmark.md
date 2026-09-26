# Phase 3 Repository and Scheduler Benchmark

**Date:** 2026-09-26  
**Scope:** local synthetic SQLite benchmark; no browser or staging traffic.

The benchmark seeds 1,000 sessions through the public repository API with a fixed mix:

- 500 due and unleased;
- 200 due later;
- 100 due with active leases;
- 100 due with expired leases;
- 100 excluded across `ADMITTED`, `EXPIRED`, `FAILED`, `NEW`, and `CREATING`.

This produces 600 eligible sessions. Each claim is limited to 50 rows. Repeated claim
samples release their leases before the next sample, and scheduler samples use a
50-item queue without starting monitoring workers, so the measurement covers the
count, bounded transactional claim, and enqueue iteration without browser work.

Run it with:

```text
queue-load-test-phase3-repository --sessions 1000 --batch-size 50 --samples 20
```

Use `--database` only with a dedicated empty SQLite file. `--report` writes an optional
aggregate JSON report; it contains no Queue IDs, transfer URLs, or browser state.

## Local Result

One run on the local macOS/Python 3.14.7 development host produced:

| Operation | p50 | p95 | Maximum |
|---|---:|---:|---:|
| Due count | 0.098 ms | 0.126 ms | 0.165 ms |
| Claim 50 | 0.555 ms | 0.673 ms | 0.693 ms |
| Update | 0.365 ms | 0.386 ms | 0.413 ms |
| Lease release | 0.279 ms | 2.321 ms | 2.362 ms |
| Scheduler iteration | 0.668 ms | 0.800 ms | 2.641 ms |

Seeding 1,000 rows with one committed public `create()` call per row took 336.935 ms.
The resulting database was 450,560 bytes. These are local synthetic observations, not
service-level guarantees or evidence about browser/staging throughput.

## Query and Index Finding

The Phase 2 index started with `(next_check_at, lease_until, status)`. SQLite used it for
the due-time alternatives but reported `USE TEMP B-TREE FOR ORDER BY`. A controlled
1,000-row selection comparison measured approximately 0.120 ms median with that plan.

The Phase 3 index is a partial ordered index on
`COALESCE(next_check_at, created_at), created_at, session_id` for monitorable statuses.
The due query uses the same expression and deterministic ordering. `EXPLAIN QUERY PLAN`
now reports:

```text
SEARCH queue_sessions USING INDEX idx_queue_sessions_due (<expr><?)
```

The controlled selection median was approximately 0.015 ms and no temporary sort was
reported. Existing Phase 2 databases replace the old index during repository
initialization. No separate `queue_id` index was added because the unique constraint
already creates one. No standalone status or lease index was added because the actual
bounded due query would not benefit from either at this population and ordering.

## SQLite Decision

SQLite remains comfortably functional for this local 1,000-session workload. Claims
use one short `BEGIN IMMEDIATE` transaction and one commit for the whole bounded batch;
two independent repository connections produced disjoint concurrent claims. Active
leases are excluded, expired leases recover deterministically, and excluded statuses
do not enter the monitoring queue.

Remaining limits are the single serialized connection per repository instance, one
commit per individual update/release, a due-count plus claim per non-full scheduler
tick, and SQLite's one-writer model. Those constraints require continued observation,
but this benchmark provides no evidence that PostgreSQL migration is currently
justified.
