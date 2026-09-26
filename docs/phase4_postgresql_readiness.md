# Phase 4 PostgreSQL and Leasing Readiness

**Date:** 2026-09-26

**Decision:** PostgreSQL implementation deferred; SQLite remains the configured backend.

**PostgreSQL recommendation for Phase 4:** OPTIONAL, gated by measurements.

The Phase 4 readiness report found no measured database failure and no evidence that
distributed workers are required. This prompt therefore tested SQLite at 10,000 rows,
audited the repository boundary, and hardened lease ownership without adding an
unjustified second database implementation.

## Decision Basis

Phase 3 measured SQLite only at 1,000 rows. The Phase 4 readiness gate required a
10,000-row measurement before choosing PostgreSQL. A new local run used the existing
synthetic repository benchmark with 10,000 rows, 6,000 eligible due sessions, 50-row
claims, and 20 samples per operation:

```text
.venv/bin/python -m queue_load_test.harness.phase3_repository \
  --sessions 10000 --batch-size 50 --samples 20
```

| Operation | 1,000-row Phase 3 p50 / p95 | 10,000-row p50 / p95 |
|---|---:|---:|
| Due count | 0.098 / 0.126 ms | 0.940 / 0.999 ms |
| Claim 50 | 0.555 / 0.673 ms | 0.538 / 0.691 ms |
| Update | 0.365 / 0.386 ms | 0.336 / 0.390 ms |
| Lease release | 0.279 / 2.321 ms | 0.198 / 0.317 ms |
| Scheduler iteration | 0.668 / 0.800 ms | 1.550 / 1.667 ms |

The 10,000-row seed took 3,031.017 ms and produced a 4,386,816-byte database. The due
query still used `idx_queue_sessions_due` without a temporary ordering tree. Query and
claim latencies remain small compared with any plausible browser restore/navigation
operation. Claim, update, and release did not degrade materially in this single-host
run. Due counting rose roughly with the eligible population, and scheduler iteration
increased to about 1.6 ms p95, but neither is a measured migration trigger.

These results do not measure sustained concurrent browser-driven writes, multiple
application processes, network database latency, or PostgreSQL. No claim is made that
SQLite is faster than PostgreSQL.

## Repository Boundary Assessment

`SessionRepository` covers the behavior needed by both backends:

- initialization;
- create, update, get, and list;
- progress save/load;
- successful Queue ID count and unique non-null Queue IDs;
- aggregate recovery summary;
- due count and bounded due-session claim;
- owner-checked lease release; and
- close.

`initialize()` was added to the protocol because concrete repositories and harnesses
already use it. SQLite now has a compile-checked contract test, which provides the
starting point for future backend parity tests. Scheduler, creation, restoration,
runtime, status, and consistency code already depend on the protocol rather than SQL.
Some benchmark harnesses intentionally name SQLite because they measure that backend.

The protocol returns domain models and backend-neutral errors. `QueueIdConflictError`
represents unique Queue ID violations. The new `LeaseOwnershipError` represents a
stale update after ownership changes, allowing a later PostgreSQL implementation to
map a conditional-update miss to the same behavior.

## Leasing Semantics

SQLite claims use one short `BEGIN IMMEDIATE` transaction:

1. select at most the requested due, unleased or expired, non-terminal rows;
2. set `worker_id` and `lease_until` for those rows;
3. read the claimed rows; and
4. commit before browser work begins.

The scheduler performs browser restore, navigation, inspection, and state persistence
outside the claim transaction. Update and release are separate short transactions.
Release includes the expected `worker_id`, so an old worker cannot release a newer
owner's lease.

This prompt also fenced session updates by lease ownership. A leased snapshot updates
only while the database row still has the same `worker_id`; an unleased snapshot
updates only while the row remains unleased. If a crashed worker's lease expires and a
replacement claims the row, any late result from the crashed worker raises
`LeaseOwnershipError` and cannot overwrite the replacement lease or session data.
Tests also cover an unleased stale snapshot attempting to clear a newly established
lease.

The current scheduler ID is unique per scheduler instance. SQLite `BEGIN IMMEDIATE`
serializes competing claim transactions, and existing two-connection tests prove
bounded disjoint claims. Expired leases remain eligible after `lease_until`, while
active leases and terminal states remain excluded. A hard worker failure therefore
recovers after lease expiry without holding a database transaction open.

## PostgreSQL Claim Design if the Gate Opens

A later PostgreSQL implementation can preserve the protocol and use a short
transaction conceptually equivalent to:

```sql
WITH due AS (
    SELECT session_id
    FROM queue_sessions
    WHERE COALESCE(next_check_at, created_at) <= :now
      AND (lease_until IS NULL OR lease_until <= :now)
      AND status NOT IN ('ADMITTED', 'EXPIRED', 'FAILED', 'NEW', 'CREATING')
    ORDER BY COALESCE(next_check_at, created_at), created_at, session_id
    FOR UPDATE SKIP LOCKED
    LIMIT :limit
)
UPDATE queue_sessions AS sessions
SET worker_id = :worker_id, lease_until = :lease_until
FROM due
WHERE sessions.session_id = due.session_id
RETURNING sessions.*;
```

The transaction must commit before returning claimed models. Leased updates and
releases must include `WHERE worker_id = :worker_id`; a zero-row result is an ownership
conflict. This provides row-level claim concurrency and stale-result fencing without
holding locks during Chrome work.

PostgreSQL parity integration tests would need a real disposable database and would be
marked separately from the normal suite. Required cases are create/update/get/list,
progress, Queue ID uniqueness, bounded `SKIP LOCKED` claims, multiple concurrent
workers over one due set, no duplicate claims, terminal exclusion, expired-lease
recovery, owner-checked release, worker-crash expiry, and stale-update fencing. No such
tests were added or reported because no PostgreSQL implementation or configured test
database exists.

## Schema and Index Assessment

SQLite uses two tables with a foreign key from progress to session. `queue_id TEXT
UNIQUE` supplies the identity index; adding another Queue ID index would duplicate it.
The monitored hot path uses one partial ordered expression index:

```text
COALESCE(next_check_at, created_at), created_at, session_id
WHERE status NOT IN ('ADMITTED', 'EXPIRED', 'FAILED', 'NEW', 'CREATING')
```

That index matches selection order and remained in the 10,000-row query plan. A
standalone status index is not justified by the due query. A standalone `lease_until`
index is also deferred because the query orders by due time, the current plan is fast,
and the `lease_until IS NULL OR lease_until <= now` predicate would not automatically
make it useful. Future sustained measurements should drive any additional index.

SQLite schema setup is idempotent and its existing lightweight migration inspects
columns and the due-index definition before adding/replacing them. There is no general
migration framework. If PostgreSQL becomes justified, the smallest maintainable next
step is backend-specific versioned SQL migrations applied explicitly at deployment;
introducing a framework should follow an actual multi-version operational need.

## Configuration and Migration Instructions

No migration is required for this result:

- keep `DATABASE_URL=sqlite:///queue_load_test.sqlite3` for local development and the
  current single-host workflow;
- existing SQLite files are initialized and migrated in place on repository startup;
- PostgreSQL URLs remain rejected because a PostgreSQL repository is not implemented;
- do not point the current application at PostgreSQL or copy sensitive session data
  into another database as part of this prompt.

If a later gate selects PostgreSQL, add an optional async PostgreSQL dependency, a
backend factory selected by `DATABASE_URL`, explicit schema migrations, and a
controlled identity-preserving data migration. Run the complete parity and concurrent
leasing integration suite against the target PostgreSQL version before moving any
staging population.

## Decision Gates

| Question | Status | Evidence |
|---|:---:|---|
| Is the repository boundary sufficient for a future backend? | PASS | Full domain operations plus initialization, bounded claims, recovery summary, and owner-fenced updates/releases. |
| Are SQLite claims bounded and transactional? | PASS | One short `BEGIN IMMEDIATE` transaction and configured `LIMIT`; browser work occurs after commit. |
| Do concurrent SQLite claimers avoid duplicate ownership? | PASS | Existing two-connection 1,000-row test returns disjoint 50-row batches. |
| Are expired leases recoverable? | PASS | Existing restart/recovery tests and due predicate reclaim expired leases. |
| Can stale workers overwrite a replacement owner? | PASS | Conditional ownership update now raises `LeaseOwnershipError`; replacement data remains intact. |
| Are terminal states excluded? | PASS | Shared due predicate excludes `ADMITTED`, `EXPIRED`, `FAILED`, `NEW`, and `CREATING`. |
| Is SQLite adequate for the measured 10,000-row query/claim workload? | PASS | Indexed due p95 0.999 ms, claim-50 p95 0.691 ms, scheduler iteration p95 1.667 ms. |
| Is sustained Phase 4 SQLite write capacity proven? | UNKNOWN | No browser-driven multi-worker duration or required service cadence has been measured. |
| Is PostgreSQL now required? | UNKNOWN | No measured trigger; implementation deferred and remains optional. |
| Is distributed PostgreSQL leasing proven? | UNKNOWN | No PostgreSQL backend or integration database was used. |

## Remaining Issues

- SQLite remains a one-writer database and each repository instance serializes its own
  connection. Sustained multi-process update/release contention is unmeasured.
- `count_due_sessions()` scans the eligible part of the due index and rose from about
  0.10 ms p50 at 1,000 rows to 0.94 ms at 10,000. Scheduler tick frequency should be
  included in later load measurements.
- Lease duration must exceed normal end-to-end browser work with enough recovery
  tolerance. Real restore/check p95 and p99 remain unknown.
- A lease owner is identified by `worker_id`; ownership changes are fenced, but no
  heartbeat or token generation beyond the unique scheduler ID exists.
- Multiple creation controllers still do not reserve the remaining target
  transactionally. That is separate from monitoring claims and matters only if a
  distributed creation design is selected.
- PostgreSQL performance, `SKIP LOCKED` behavior, schema migration, pool sizing,
  failover, and operational recovery remain unmeasured.

The next task is **Phase 4 Prompt 3 — Shared State Storage Readiness**.

## Windows Host Recheck — 2026-09-27

The persistence decision was rechecked on the current development machine rather than
inferred from the earlier macOS result.

### Environment

- Windows 11 Pro, 64-bit, NTFS
- Python 3.14.7 (the project minimum remains Python 3.12)
- SQLite 3.50.4, threadsafety level 3
- SQLite file defaults: `journal_mode=delete`, `synchronous=2` (`FULL`)
- Approximately 16.47 GB free on the benchmark volume at the time of the check
- No `.env` and no root-level `*.sqlite3` project database existed in this checkout;
  the effective setting remains `sqlite:///queue_load_test.sqlite3`

The same temporary 10,000-row benchmark ran twice with 6,000 eligible due sessions,
50-row claims, and 20 samples per operation. Each run used a newly created database.

| Metric | Windows run 1 | Windows run 2 |
|---|---:|---:|
| Seed duration | 33,387.926 ms | 33,392.885 ms |
| Approximate seed rate | 299.51 rows/s | 299.46 rows/s |
| Database size | 4,386,816 bytes | 4,386,816 bytes |
| Due count p50 / p95 | 4.005 / 4.309 ms | 4.111 / 4.601 ms |
| Claim 50 p50 / p95 | 4.246 / 4.866 ms | 4.321 / 4.898 ms |
| Update p50 / p95 | 3.076 / 3.326 ms | 3.216 / 3.971 ms |
| Lease release p50 / p95 | 2.780 / 3.124 ms | 2.801 / 2.973 ms |
| Scheduler iteration p50 / p95 | 8.840 / 9.503 ms | 8.791 / 9.263 ms |

Both query plans reported:

```text
SEARCH queue_sessions USING INDEX idx_queue_sessions_due (<expr><?)
```

No temporary ordering B-tree appeared. The Windows host is materially slower at
individual durable commits than the earlier macOS host, especially during 10,000
one-row seed transactions. This does not currently justify PostgreSQL: creation is
browser-bound, the measured claim/update/release operations remain small relative to
browser restoration and navigation, and neither the real due-session mix nor a
required sustained write cadence has been measured. The worst-case planning example
of all 10,000 sessions due every 30 seconds would require about 333 completed checks/s,
but that is not the configured adaptive lifecycle workload and has not been validated
as a service objective.

Repository and monitoring tests passed on this host, including Queue ID uniqueness,
bounded/disjoint claims through separate repository connections, active-lease
exclusion, expired-lease takeover, stale-owner fencing, owner-checked release,
terminal-state exclusion, index migration, restart persistence, bounded queues, and
shutdown lease release.

### Recheck Decision

**PostgreSQL remains optional and deferred. No database migration is needed on this
machine.** There is no existing local database to migrate, and a first application run
will create the current SQLite schema and due index. Existing older SQLite databases
remain covered by the idempotent column/index initialization path.

Reopen the PostgreSQL gate if sustained browser-backed measurement shows SQLite write
serialization, lock waits, claim/update/release latency, backlog recovery, or a
multi-node ownership requirement missing the agreed cadence. The current check does
not measure PostgreSQL, multi-process contention, or real Queue-it write rates.
