# Phase 4 Shared State Storage Readiness

**Date:** 2026-09-26

**Decision:** Shared/object state storage deferred. `FileSystemStateStore` remains the
active and only `StateStore` implementation.

**Distribution basis:** Phase 4 stays single-machine. [Phase 4 readiness](phase4_readiness.md)
left distributed workers at `UNKNOWN` need, with no measurement showing that one host
misses an acquisition window, monitoring cadence, or recovery objective. Shared storage
exists only to serve multiple nodes, so there is no requirement for it. No cloud or
object-storage dependency is documented for this environment, and none was added.

## 1. What Changed

The store boundary is unchanged: `save(session_id, state) -> Path`,
`load(session_id) -> BrowserState | None`, and `delete(session_id) -> bool`. No
metadata operations were needed.

| Change | Reason |
|---|---|
| Each state file is a self-describing envelope: `format`, `version`, `session_id`, `sha256` of the state JSON, and `state` | A load now rejects another session's document copied or renamed into place (`StateSessionMismatchError`) and detects corruption that still parses as valid JSON (`StateCorruptError`). Previously only the file name linked a file to a session. |
| Plain `storage_state` files written before this change still load | Existing `.browser-state/` directories stay usable with no migration. The audit counts them as `legacy_state_files`; the next successful HYBRID state refresh rewrites each one in the new format. |
| `save` fsyncs the state directory after `os.replace` | The rename becomes durable across power loss, not just the file contents. Measured cost is within run-to-run noise (section 3). |
| Typed errors: `StateUnreadableError`, `StateCorruptError`, `StateSessionMismatchError`, all subclasses of `StateStoreError` | Callers that catch `StateStoreError` (restoration maps it to `STATE_CORRUPT`) are unchanged. The audit uses the subtypes to report the specific problem. Messages name only the session ID, never state contents. |
| The consistency audit resolves each parent directory once and lists files with `os.scandir` | Profiling at 10,000 files showed per-path `Path.resolve()` took about half the scan (30,000 resolves producing 270,000 `lstat` calls). |
| The audit reports `unreadable_state_file`, `mismatched_state_file`, and `insecure_state_permissions` | These were previously grouped as corrupt or not detected at all. |

Save remains atomic: a private temporary file in the same directory is written,
flushed, fsynced, set to `0600`, and renamed over the destination. A reader sees either
the previous complete document or the new one, never a partial write. Save is
idempotent, so it is safe to retry after a failure; tests prove that a retried save
after a simulated failure succeeds and leaves no temporary file. The store has no
internal retry loop, because local-filesystem failures such as a full disk or wrong
permissions are not transient. Callers keep their existing attempt-level retry policy.

## 2. Consistency Audit

`queue-load-test-state-check` (`StateConsistencyChecker`) compares repository rows with
the state directory in one worker thread. It **only reports**; it never deletes,
rewrites, or repairs anything. A test snapshots every file's mode, mtime, and bytes
before and after a scan with orphans, temporary files, and corrupt files present, and
confirms nothing changed.

| Finding kind | Meaning |
|---|---|
| `missing_state_file` | A HYBRID row with a Queue ID, not `FAILED`, references no file. |
| `orphaned_state_file` | A `.json` file in the directory is referenced by no row. |
| `corrupt_state_file` | Invalid JSON, not an object, unknown format/version, missing state, or digest mismatch. |
| `unreadable_state_file` | The file exists but cannot be read (for example, permissions or I/O errors). |
| `mismatched_state_file` | The document's embedded `session_id` differs from the session that owns the path. |
| `duplicate_state_path` | More than one row references the same path. |
| `conflicting_state_path` | A HYBRID row's path is not `<state-directory>/<session_id>.json`. |
| `insecure_state_permissions` | The file is readable or writable by group or others. |
| `stale_temporary_file` | An interrupted atomic write left a `.tmp` file. |

The optional `recovery_summary()` counts corrupt, unreadable, and mismatched files
together as `corrupt_state_files` (unusable for restore). Findings contain paths and
session IDs only, never Queue IDs, transfer URLs, or state contents.

Normal runtime startup, the scheduler, creation, and restoration never traverse the
state directory. Each opens only `<session_id>.json`. The audit is an explicit operator
tool.

## 3. 10,000-State Local Benchmark

Local synthetic benchmark on macOS 26.5.1 (ARM64, APFS), with Python 3.14.7 from the
project virtual environment. Each run used a fresh empty database and state directory,
10,000 HYBRID sessions, 100 delete probes, and 20 concurrent operations (matching the
validated 20-worker Phase 4 monitoring profile). No Chrome and no Queue-it traffic.

```text
queue-load-test-phase3-storage --database <empty>/sessions.sqlite3 \
  --state-directory <empty>/state --sessions 10000 --delete-samples 100 \
  --concurrency 20 --report <report>.json
```

The two runs after the change are shown with the pre-change baseline, measured on the
same host with the same command minus the new fields:

| Measurement | Baseline (before change) | Run 1 | Run 2 |
|---|---:|---:|---:|
| State files | 10,000 | 10,000 | 10,000 |
| Logical state bytes | 12,490,000 | 14,160,000 | 14,160,000 |
| Average file size | 1,249 B | 1,416 B | 1,416 B |
| Allocated on disk | 40,960,000 B (`du`) | 40,960,000 B | 40,960,000 B |
| SQLite database | 5,070,848 B | 5,070,848 B | 5,070,848 B |
| Sequential save | 5,351.6/s | 5,310.7/s | 5,296.4/s |
| Save p50 / p95 | 0.176 / 0.241 ms | 0.179 / 0.252 ms | 0.179 / 0.253 ms |
| Sequential load | 17,672.7/s | 15,798.3/s | 15,895.4/s |
| Load p50 / p95 | 0.055 / 0.069 ms | 0.063 / 0.076 ms | 0.062 / 0.075 ms |
| Atomic replace p50 / p95 | 0.198 / 0.269 ms | 0.208 / 0.291 ms | 0.206 / 0.291 ms |
| Delete p50 / p95 | 0.052 / 0.075 ms | 0.052 / 0.071 ms | 0.052 / 0.072 ms |
| Concurrent save ×20 | not measured | 7,597.7/s, p95 3.724 ms | 7,608.9/s, p95 3.693 ms |
| Concurrent load ×20 | not measured | 15,812.6/s, p95 2.117 ms | 15,731.4/s, p95 2.133 ms |
| Directory traversal (`scandir` + `stat`) | not measured | 21.6 ms | 18.2 ms |
| Consistency audit, baseline / after restart | 677.1 / 670.1 ms | 421.0 / 446.5 ms | 414.1 / 409.2 ms |
| Audit findings | 0 | 0 | 0 |
| Event-loop lag p95 / max | 0.160 / 53.885 ms | 0.465 / 12.705 ms | 0.437 / 12.685 ms |

Interpretation:

- The envelope adds 167 bytes to each file. Each file still fits in one 4 KiB APFS
  block, so allocated disk usage did not change. At this state size, space on disk is
  set by the block count (about 4 KiB per session, about 41 MB per 10,000), not the
  logical bytes. Real Playwright state is likely larger and was never measured.
- Save latency rose by at most about 0.012 ms p95, which is within noise. The added
  directory fsync is therefore cheap on this host. macOS `fsync` does not force a
  physical media flush (`F_FULLFSYNC`), so this is not a power-loss proof.
- Load p50 rose by about 0.008 ms for digest verification, and load stays about three
  times faster than save.
- Twenty concurrent saves reached about 1.43 times the sequential save rate. Each save
  took longer (p95 about 3.7 ms) because of thread-pool and disk queueing. At
  the illustrative 333 state refreshes/s (all 10,000 sessions refreshed every 30 s),
  sustained save demand would be about 4% of the measured concurrent rate. That is
  file-system headroom only, not a real browser workload measurement.
- The full audit fell from about 675 ms to about 410–450 ms, even though it now verifies
  digests and file permissions. Plain directory traversal of 10,000 entries takes about
  20 ms. A flat single directory is therefore adequate at 10,000 files, and
  hash-sharded subdirectories are not justified.

`tests/unit/test_state_consistency.py` also runs a 10,000-state audit in the normal
suite (about 1.9 s). It finds no problems in a clean state, then injects one missing,
one corrupt, one mismatched, and one orphaned file, and asserts that exactly those four
findings are reported.

## 4. Shared/Object Storage Mode

**Not implemented; not measured.** Save/load latency, error and retry behavior, and
missing-object behavior for a shared store are `UNKNOWN`. No results are claimed.

## 5. Distribution Prerequisites for a Shared Store

If Phase 4 Prompt 4 or later measurements select multiple nodes, a shared `StateStore`
must meet these requirements before any multi-node run:

1. **Boundary shape.** `save()` returns a `Path`, and the database `state_path` column
   holds a filesystem path. An object store needs a URI or key in that column. This is a
   deliberate protocol change with a data migration, not a drop-in implementation.
2. **Atomic visibility.** A single-object put that is visible all at once, or
   write-then-rename with the same guarantee. The self-describing envelope and digest
   carry over unchanged and protect against reading partial or other-session objects.
3. **Write fencing.** A conditional put (ETag, generation, or version match) or
   lease-scoped keys, so that a stale worker whose lease was reclaimed cannot overwrite
   a newer state. Database lease fencing (`LeaseOwnershipError`) covers session rows but
   not the state file written just before the row update.
4. **Retries.** Bounded retries with backoff for transient network errors only. Puts are
   idempotent on the same key, and `load` must distinguish "missing" from
   "unavailable".
5. **Audit parity.** A bounded, paginated listing to replace `scandir`, reporting the
   same finding kinds, still report-only.
6. **Sensitive data.** Encryption at rest, access limited to worker identities, no
   public URLs, and object keys that never contain Queue IDs or transfer URLs.
7. **Measurements.** Save/load p50/p95, error rate, retry behavior, and missing-object
   behavior against the real backend, compared with section 3.

## 6. Known Limits

- All measurements use synthetic 1,416-byte documents on one local APFS volume. Real
  Playwright `storage_state` size and churn, NFS or shared volumes, Linux filesystems,
  and disk-full behavior are unmeasured.
- Legacy plain files have no embedded identity or digest until they are rewritten. A
  legacy file copied to the wrong session's path is not detectable from contents. The
  audit reports how many legacy files remain.
- Two writers saving the same session concurrently leave one complete version (last
  rename wins). The store does not arbitrate between them; ownership comes from the
  repository lease.
- A process kill between the state save and the database commit during creation can
  leave an orphaned state file. The audit reports it; nothing deletes it automatically.
- The audit loads every session row and reads every file. At about 0.4 s per 10,000
  rows it is fine as an operator tool but should not run on a scheduler tick.

## 7. Decision Gates

| Question | Status | Evidence |
|---|:---:|---|
| Is Phase 4 distribution justified now? | UNKNOWN | No measured single-host shortfall; decision belongs to Phase 4 Prompt 4. |
| Is local state storage adequate at 10,000 synthetic files? | PASS | Save p95 0.25 ms, load p95 0.08 ms, audit about 0.4 s, zero findings, 41 MB allocated. |
| Are partial writes never visible as valid? | PASS | Same-directory temporary file, fsync, and atomic rename; failed-replace and retry tests pass. |
| Can a load return another session's state? | PASS (envelope files) | Embedded `session_id` is checked; copy-into-place test raises `StateSessionMismatchError`. |
| Is corruption detectable? | PASS | Truncated, tampered, unknown-version/format, and non-object documents raise `StateCorruptError`. |
| Are missing, orphaned, unreadable, and duplicate state reported without destructive action? | PASS | Audit finding kinds plus the non-mutation snapshot test. |
| Is real-workload state storage adequate? | UNKNOWN | Real state size, churn, and concurrent browser refresh are unmeasured. |
| Is shared/object storage required? | UNKNOWN | Only if distribution is selected; prerequisites listed in section 5. |

The next task is **Phase 4 Prompt 4 — Distributed Worker Gate and Implementation**.
