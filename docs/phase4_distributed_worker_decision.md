# Phase 4 Distributed Worker Decision

## Decision

**Distribution is deferred. Phase 4 continues with the existing single-machine
deployment.**

This is an evidence gate, not a claim that one machine is sufficient for 10,000 live
Queue-it journeys. The repository contains no authorised browser-backed measurement of
the creation rate, monitoring rate, restore latency, or lifecycle mix needed to prove
that one host can meet the required cadence. It also contains no measurement showing
that one host cannot meet that cadence. Under the project rule, absence of a measured
shortfall is not permission to add distributed execution.

No PostgreSQL backend, shared state store, distributed worker service, message broker,
or deployment orchestrator is added by this prompt.

## Evidence Reviewed

- `docs/phase3-acceptance.md`
- `docs/phase4_readiness.md`
- `docs/phase4_postgresql_readiness.md`
- `docs/phase4_state_storage_readiness.md`
- Current repository, scheduler, lease, BrowserManager, creation, configuration, and
  shutdown implementations and tests

The relevant measurements are:

- A synthetic 1,000-session monitoring workload completed two sweeps at 944.98 and
  947.26 checks/s. Projecting that synthetic handler to 10,000 rows gives 10.56-10.58
  seconds, or 21.11-21.16 seconds with an assumed 50% planning utilization. This is
  not browser-backed Queue-it throughput.
- The 10,000-row SQLite benchmark measured due-count p95 0.999 ms, claim-50 p95
  0.691 ms, update p95 0.390 ms, release p95 0.317 ms, and a scheduler iteration p95
  of 1.667 ms. No measured local database bottleneck was found.
- The 10,000-file local-state benchmark measured save p95 about 0.25 ms, load p95
  about 0.08 ms, and approximately 7,600 saves/s with 20 concurrent operations. No
  measured local state-store bottleneck was found.
- Short installed-Chrome tests completed 50, 75, and 100 contexts without recorded
  context, navigation, crash, cleanup, or stall failures. The 75- and 100-context
  cases crossed a conservative RAM-pressure indicator; the 50-context case was the
  least-pressured candidate. These tests used an in-memory page, ran for only two
  seconds, and do not establish Queue-it stability.
- No authorised Phase 3 or Phase 4 Queue-it acquisition, restoration, or monitoring
  benchmark result is checked in.

## Single-Machine Sufficiency Questions

| Question | Finding | Evidence and limit |
|---|---|---|
| Can one machine acquire sessions at the required cadence? | **UNKNOWN** | No real 10,000-target creation duration or required completion window is recorded. Synthetic target accounting proves correctness, not throughput. |
| Can one machine complete monitoring checks at the required cadence? | **UNKNOWN** | Synthetic scheduling is fast, but real restore/navigation/check latency and the due-session lifecycle mix are unmeasured. |
| Is CPU the limiting resource? | **UNKNOWN** | Short local Chrome CPU observations exist, but no sustained Queue-it run establishes CPU saturation. |
| Is RAM the limiting resource? | **UNKNOWN** | Short 75/100-context cases raised a RAM-pressure flag; the bounded 50-context candidate was less pressured. Sustained real-page memory and leakage are unmeasured. |
| Is Chrome stability the limiting resource? | **UNKNOWN** | Short in-memory-page cases had no failures. Sustained Queue-it navigation, crash, and restart behavior are unmeasured. |
| Is database throughput the limiting resource? | **No measured limit in the synthetic workload; real result UNKNOWN** | Indexed 10,000-row SQLite operations were sub-2 ms at p95 for a scheduler iteration. Sustained concurrent browser-driven writes and multi-process contention were not tested. |
| Is network/page latency the limiting resource? | **UNKNOWN** | No authorised Queue-it load measurement exists. |

The required acquisition deadline and monitoring service-level cadence are also not
defined by measured staging behavior. A distribution decision cannot be derived from
throughput alone until those requirements are explicit.

## Retained Architecture

Phase 4 retains one application process with:

- one local SQLite repository;
- one local atomic `FileSystemStateStore`;
- a bounded creation queue and fixed creation workers;
- a bounded monitoring queue, bounded claims, and fixed monitoring workers;
- one `BrowserManager` controlling a bounded pool of installed Google Chrome processes
  and isolated contexts; and
- persisted leases and owner-fenced updates for crash/restart recovery.

The controller never needs to hold 10,000 tasks or contexts. Persisted sessions remain
parked and only due sessions are restored through bounded capacity.

## Distribution-Readiness Audit

The current design has useful distribution boundaries, but it is deliberately not a
deployable multi-node system.

| Capability | Current evidence |
|---|---|
| Unique worker ownership | Scheduler IDs are unique by default and injectable; repository rows persist `worker_id` and `lease_until`. There is no deployment-level worker identity configuration because no distributed worker is deployed. |
| Bounded claims and local work | Implemented and tested with bounded batches, queues, workers, and BrowserManager capacity. |
| Lease expiry and stale-worker fencing | Implemented and tested using independent SQLite repository connections. Expired work can be reclaimed; a stale owner cannot update or release the replacement lease. |
| Graceful worker shutdown | Implemented for the single-process scheduler; queued leases are released and in-flight work is bounded by the shutdown timeout. |
| Shared database claiming | **Not implemented.** SQLite is the only configured backend. PostgreSQL `FOR UPDATE SKIP LOCKED` behavior is design-only and untested. |
| Shared browser state | **Not implemented.** State paths and files are local. A distributed store would require key/URI addressing, conditional-write fencing, retries, access controls, and parity tests. |
| Cross-node state-write fencing | **Not implemented.** Database updates are owner-fenced, but local state files are last-writer-wins. This must be resolved before multi-node execution. |
| Multi-controller creation target | **Not implemented.** Unique Queue IDs are enforced, but multiple creation controllers do not transactionally reserve the remaining target and could overshoot. |
| Identity mismatch preservation | Implemented in the existing restoration path and tested locally; real shared-worker behavior remains unverified. |
| Multi-process/node integration | **NOT RUN / UNKNOWN.** No PostgreSQL/shared-store environment is configured. |

These boundaries make a later distribution change tractable, but they must not be
described as distributed correctness.

## Worker Failure and Recovery Evidence

Local tests demonstrate disjoint due-session claims through separate SQLite
connections, lease expiry takeover, owner-fenced update/release behavior, bounded
scheduler shutdown, and restart persistence. Those results support single-machine
crash recovery. They do not prove node-loss recovery, PostgreSQL locking semantics, or
shared-state visibility, so distributed recovery remains **UNKNOWN**.

## Gate for Reconsidering Distribution

Reopen this decision only after all of the following are available:

1. An explicit acquisition completion objective and monitoring cadence/backlog SLO.
2. A sustained, authorised, single-host Queue-it run at the bounded candidate profile.
3. Measured creation/check throughput, restore and navigation latency, backlog trend,
   CPU/RAM, context waits, browser crashes, and identity outcomes.
4. Evidence that a cadence shortfall is host-capacity related rather than imposed by
   Queue-it/page/network latency or an incorrect polling objective.

If those measurements require multiple hosts, implementation must first provide and
integration-test PostgreSQL lease claiming, shared conditionally written state,
multi-controller creation reservations, worker/node recovery, and shared observability.
PostgreSQL leasing is sufficient initially; a message broker is not justified by the
current architecture.

## Infrastructure Requirements

The selected single-machine path adds no infrastructure. It continues to require local
SQLite, local protected browser-state files, and installed Google Chrome.

Potential future distributed execution would require PostgreSQL and shared durable
state storage. Kafka, RabbitMQ, Redis, Kubernetes, and similar infrastructure remain
explicitly unjustified by current evidence.

## Next Task

**Phase 4 Prompt 5 — Acquire 10,000 Queue IDs.**

That run must preserve the bounded single-machine architecture and record real evidence
without treating these synthetic results as a prediction of Queue-it performance.
