# Phase 3 Persistence and State Storage Benchmark

## Scope

This local synthetic benchmark measures SQLite plus `FileSystemStateStore` at exactly
1,000 persisted HYBRID sessions. It does not contact Queue-it or launch Chrome. The
browser-state documents are deterministic 1,249-byte JSON fixtures designed to exercise
atomic file mechanics; they are not measurements of real Queue-it state payload size.

The run used Python 3.12, SQLite 3.50.4, and a local macOS 26.5.1 ARM64 filesystem on
2026-09-26. Results are host/filesystem specific and do not extrapolate to 10,000 files,
network storage, or sustained concurrent browser refreshes.

## Verified Result

| Measurement | Result |
|---|---:|
| Persisted sessions / state files | 1,000 / 1,000 |
| SQLite database size | 417,792 bytes |
| State storage size | 1,249,000 bytes |
| Average/minimum/maximum file | 1,249 / 1,249 / 1,249 bytes |
| Initial save throughput | 4,719.6 files/s |
| Save p50 / p95 / max | 0.207 / 0.263 / 1.156 ms |
| Load throughput | 18,799.2 files/s |
| Load p50 / p95 / max | 0.052 / 0.063 / 0.089 ms |
| Atomic replace throughput | 4,325.7 files/s |
| Replace p50 / p95 / max | 0.227 / 0.291 / 0.418 ms |
| Delete throughput (100 probes) | 17,827.6 files/s |
| Delete p50 / p95 / max | 0.053 / 0.075 / 0.082 ms |
| Baseline consistency scan | 63.554 ms, zero findings |
| Restart consistency scan | 61.355 ms, zero findings |
| Event-loop lag p95 / max | 0.149 / 3.302 ms |

Restart reopened the SQLite database and reconstructed all 1,000 rows, then a fresh
state-store/checker instance matched all 1,000 references to all 1,000 files. No orphan,
missing, corrupt, duplicate/conflicting, or stale temporary state was present.

## Atomicity and Event Loop

`FileSystemStateStore.save()` writes JSON to a private temporary file in the destination
directory, flushes and fsyncs it, applies mode `0600`, and replaces the destination with
`os.replace`. A simulated replacement failure preserves the previous complete document
and removes the temporary file. Tests also cover normal replacement and verify that no
partial temporary file remains.

Save, load, and delete operations use `asyncio.to_thread`. The consistency checker also
offloads directory traversal and JSON parsing as one worker-thread operation. A forced
slow-write test proves the event loop remains schedulable; the benchmark lag probe gives
supporting local evidence. Thread-pool and disk contention under real concurrent Chrome
state refreshes remain UNKNOWN.

## Consistency and Cleanup Policy

`queue-load-test-state-check` compares an existing SQLite database with a state
directory and reports:

- database sessions that require a missing HYBRID state file;
- JSON files with no corresponding database reference;
- invalid JSON or non-object state documents;
- multiple sessions sharing one state path;
- HYBRID paths conflicting with `<state-directory>/<session_id>.json`; and
- stale atomic-write temporary files.

The checker never deletes or repairs data. Orphan and temporary cleanup requires an
operator to inspect the report and remove explicit paths separately. This avoids turning
a reporting mistake or path-configuration error into identity loss.

## Decision

SQLite plus local state files remains acceptable for the Phase 3 single-host,
1,000-session architecture. The measured database/file footprint and operation latency
do not justify PostgreSQL, object storage, or a storage framework. This decision does
not establish suitability for 10,000 sessions or multiple nodes.

Re-run with dedicated empty paths:

```text
queue-load-test-phase3-storage --database phase3-storage-synthetic/sessions.sqlite3 \
  --state-directory phase3-storage-synthetic/state --sessions 1000 \
  --report phase3-storage-benchmark.json
```
