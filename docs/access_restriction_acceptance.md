# Acceptance — Acquisition-Time Access-Restriction Handling

Date: 2026-10-01.

**Decision: ACCEPTED for local application behaviour, with one FAIL (Q11, no time-based
retry pacing) recorded as an operator decision.** Queue-it staging behaviour is
**NOT RUN / UNKNOWN**.

## Expected behaviour

- **Classification.** A new visitor may render "We are sorry, your access has been
  restricted" before any Queue ID exists. That attempt is classified as
  `AcquisitionFailure.ACCESS_RESTRICTED_BEFORE_QUEUE` (code
  `access_restricted_before_queue`).
  - Detection reads the rendered body text, using a normalised whole-phrase match. It
    runs before every live-queue observation and on any 4xx response before the status
    is classified.
- **Handling.** The attempt is an acquisition failure, not a Queue lifecycle state.
  - It is not retried on its own context or proxy session.
  - Its BrowserContext closes, and any uncommitted state under its session ID is
    removed.
  - It is recorded as a leaseless `FAILED` row with `queue_id = NULL`. No placeholder ID
    is created.
- **Replacement.** The bounded controller immediately schedules an ordinary replacement
  work item. That item gets a fresh context and its own proxy assignment from the
  existing allocator. Nothing rotates or chooses proxies because of a restriction.
- **Target.** The target counts only persisted unique Queue IDs.
- **Visibility.**
  - Metrics: `queue_creation_access_restricted_total` and
    `queue_creation_access_restricted_consecutive` (both unlabelled).
  - Log: one `acquisition_access_restricted` event per attempt.
  - Dashboard: "Access-restricted attempts".

## Evidence boundary

| Source | Proves |
| --- | --- |
| `tests/unit/test_access_restriction.py` (50 tests) | Detector, classification, cleanup, controller sequencing, bounds, immutability, logs, metrics, restart. Uses deterministic fakes. |
| `tests/integration/test_access_restriction_page.py` (3) | Real Chrome: JS-rendered detection, Queue-it fixtures and hidden text not flagged, end-to-end restricted creator run against a 127.0.0.1 403 page. |
| `tests/integration/test_access_restriction_acceptance.py` (2) | Real Chrome, production creator and controller, SQLite and state store against a scripted 127.0.0.1 server. Covers the exact sequence and shutdown after repeated restrictions. |
| `tests/unit/test_iproyal_proxy.py::test_restricted_attempt_is_replaced_with_a_fresh_context_and_assignment` | Each replacement uses a new context routed through its own sticky session. |
| `tests/unit/test_web_ui.py` (2 restriction tests) | Dashboard aggregate count; runtime status property. |
| Local measurement (scratch script, real Chrome, 127.0.0.1) | Retry rate when every attempt is restricted. |

No Queue-it, IPRoyal, or staging traffic was made. The synthetic queue page in the
acceptance integration test is not Queue-it markup. Queue-it parsing is covered by the
existing extractor and transfer tests.

## Results

| # | Question | Result | Evidence |
| --- | --- | --- | --- |
| 1 | Restriction before a Queue ID gives the explicit classification | **PASS** | `test_restriction_before_queue_id_fails_attempt_without_persisting_identity`, `..._with_error_status_...`, `..._rendered_after_navigation_...`, and the real-Chrome `test_creator_discards_restricted_attempt_in_a_real_browser` |
| 2 | Excluded from the successful Queue ID count | **PASS** | `count_successful_queue_ids` is unchanged in every restriction test; `test_acceptance_sequence_reaches_exact_target_without_overshoot` |
| 3 | No placeholder or fake Queue ID persisted | **PASS** | FAILED rows have `queue_id IS NULL` and `transfer_url == ""` (unit and real-Chrome tests) |
| 4 | BrowserContext always released | **PASS** | `test_context_is_released_on_every_outcome` (success, restriction, exception, cancellation); `active_context_count == 0` in the real-Chrome tests |
| 5 | Temporary state cleaned; successful sessions' state untouched | **PASS** | `test_restriction_removes_uncommitted_state_and_takes_no_lease`; `test_successful_rows_and_state_are_immutable_under_restricted_attempts` (byte-identical); real-Chrome shutdown test (only seeded files remain, no `.tmp`) |
| 6 | Bounded acquisition continues afterwards | **PASS** | `test_persistent_restriction_keeps_retrying_until_success` (25 restrictions, then success) |
| 7 | R, R, S, R, S reaches the exact target with no overshoot | **PASS** | `test_acceptance_sequence_reaches_exact_target_without_overshoot[1,2,3 workers]` (5 contexts, 5 unused pages left); real-Chrome `test_real_browser_sequence_reaches_exact_target` (exactly 5 requests) |
| 8 | Duplicate, navigation-failure, timeout, cancellation and restart semantics unchanged | **PASS** | `test_duplicates_and_restrictions_are_counted_separately`, `test_creator_reports_duplicate_...`, `test_generic_navigation_error_keeps_existing_transient_retry`, `test_navigation_timeout_semantics_are_unchanged`, the cancellation tests, `test_restart_counts_only_persisted_successes_...`, and the unchanged `test_creation.py` suite |
| 9 | Successful persisted Queue IDs immutable | **PASS** | Full-row equality before and after, in the unit and real-Chrome shutdown tests |
| 10 | Workers, queue depth, contexts and in-flight work bounded | **PASS** | `maximum_active ≤ workers` and `maximum_queue_depth ≤ capacity` in the controller tests; real-Chrome `maximum_concurrent_creating ≤ 3`; no per-item tasks |
| 11 | Protection against a tight retry storm when every visitor is restricted | **FAIL (operator decision)** | The backoff and halt were removed on request. Concurrency is bounded and nothing leaks, but there is no time-based pacing. Measured locally: about **10.2 attempts/s with 1 worker and 10.0/s with 3 workers**, about 100 FAILED rows per 10 s. Remote latency would lower the rate, but nothing caps it. |
| 12 | Outcomes visible in sanitized logs, status and metrics | **PASS** | `test_restricted_attempt_emits_one_sanitized_event`; dashboard `test_summary_reports_aggregate_access_restriction_status`; metric counter tests |
| 13 | Metrics low-cardinality and free of sensitive values | **PASS** | `test_access_restriction_metrics_have_no_labels`; `test_metrics_exposition_contains_no_sensitive_values`. The rendered `/metrics` text has no Queue IDs, session IDs, URLs, `127.0.0.1`, cookies, storage-state keys or page text. |
| 14 | Shutdown after repeated restrictions leaves nothing behind | **PASS** | Real-Chrome `test_shutdown_after_repeated_restrictions_leaves_nothing_behind`: zero owned contexts, no leases on any row, no orphan or temporary state, seeded sessions and state byte-identical |
| — | Real Queue-it restriction page: wording, status, timing, and frequency | **UNKNOWN / NOT RUN** | No authorised staging gate configured |

## Remaining risks

- **Retry storm (Q11).** If every visitor is restricted, acquisition retries continuously
  at `CREATION_WORKERS` concurrency, paced only by navigation time. Each attempt leaves a
  `FAILED` row, and on IPRoyal it uses a new sticky session. Bounded backoff (commit
  `c341d5e`) was removed by operator request in `00c6364` and could be restored.
- **Detector scope.** Only the one known English phrase is detected. Other wordings fall
  through to the existing `queue_page_not_ready` or `permanent_http_response` paths.
- **Operator actions.** Add/Replace call the creator directly; restrictions there are
  classified and cleaned up the same way.
- **Volatile status.** The dashboard count is per runtime and resets on restart. FAILED
  rows persist.

## Staging status

**NOT RUN.** No authorised Queue-it staging environment was configured or gated for
this validation, so Queue-it was not contacted. All vendor-specific behaviour is
UNKNOWN.
