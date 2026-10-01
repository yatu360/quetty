# Phase 9 Acceptance — IPRoyal Per-Session Proxy and Proxy-IP Observation

Date: 2026-10-01.

**Decision: application integration ACCEPTED (local evidence).** Provider continuity is
reported separately below, with an explicit duration boundary.

## Evidence sources and boundary

| Source | What it is | What it proves |
| --- | --- | --- |
| `queue-load-test-phase9-acceptance` → `docs/results/phase9_iproyal_acceptance_result.json` | The real operator stack (FastAPI app and lifespan, `ApplicationRunRuntime`, creation, scheduler/monitor, operator actions, headed pool, SQLite). Runs on installed Patchright and Chrome with the unmodified production proxy path. | Application integration. `LocalAuthProxy` stands in for the IPRoyal gateway and ipify: it uses Basic auth, records each request's sticky ID, and maps one stable synthetic exit IP to each sticky session. `LocalQueueSimulator` stands in for the target behind a non-loopback alias. **Not IPRoyal, ipify, or Queue-it evidence.** |
| `RUN_PHASE9_IPROYAL_LIVE=1 queue-load-test-phase9-iproyal-live --confirm-live-iproyal --checkpoints 5,30` → `docs/results/phase9_iproyal_live_result.json` | Real IPRoyal Residential (GB, `2h`). Uses production `SessionProxyResolver`, `BrowserManager` (Patchright), and `ProxyIpObserver` against real ipify. | Real provider behaviour: same-session IP observation and sticky continuity for the measured intervals only. Contacts ipify only, never Queue-it. |
| Deterministic tests (`tests/unit/test_proxy_ip_observation.py`, `test_iproyal_*.py`, `tests/integration/test_iproyal_proxy_routing.py`, `test_phase9_acceptance.py`) | Parser, observer bounds, tracker, repository, hooks, Direct, dashboard, fail-closed, and secrecy. | Branch-level behaviour, including failures that are unsafe to induce live. |

No Queue-it traffic was made. Direct Monitoring evidence comes from local fixtures
only, and no Queue-it Direct claim is made.

## Configuration

| Field | Value |
| --- | --- |
| Provider | `iproyal` (persisted run provenance) |
| Country | `gb` |
| Sticky lifetime | `2h` |
| Browser backends | Patchright (default) and Chrome (fallback), each run fully |
| Monitoring strategy | Headed Window (application run); Direct via local fixture |
| Session count | 3 initial; plus Add, Replace, and a 2-session run after Reset (7 provider IDs per backend) |

No credentials appear in any report.

## Assignment

- Each backend assigned 7 provider session IDs, all distinct and all 8-character
  alphanumeric. No ID collided or was reused across Replace or Reset.
- The consistency checker reports, without repair, any malformed, duplicate, missing,
  or unexpected ID (Prompt 1).
- After an application restart, persisted IDs were reloaded unchanged and used for the
  first check and the first post-check IP lookup (gate G).

## Runtime paths (application acceptance, Patchright and Chrome: 30/30 PASS each)

| Scenario | Result |
| --- | --- |
| A: three sessions | PASS. 3 session IDs and 3 provider IDs. Initial acquisition was proxied with each session's own ID, and every target request arrived through the proxy. Each initial IP lookup used the session's own ID, and the recorded IP equals that sticky session's exit. The dashboard shows all three. |
| B: repeated automatic checks | PASS ×2. The same provider ID and Queue ID were used, one lookup was made through the same ID, and IP Checked ≥ Queue Checked. |
| C/M: Queue Checked vs IP Checked | PASS for ipify HTTP 500, timeout, invalid JSON, and invalid IP. Each time the queue check succeeded (no FAILED or CONNECTION_LOST, no `last_error`), Queue Checked advanced, and Proxy IP and IP Checked were retained. IP Checked advanced again after recovery. |
| L: IP change | PASS. A synthetic exit change for the same sticky session updated `proxy_ip`, increased the change count, and set the change time. The dashboard showed `changed ×N`. Provider ID and Queue ID were unchanged, with no Replace and no rotation. |
| D: Refresh Now | PASS while paused (exactly one lookup, with all traffic on the session's ID) and while active. |
| E: Manual Open | PASS. Only the existing ID was used. No lookup was made while the window was open; exactly one was made at close. The Queue ID was unchanged and no new ID was created. Later automatic monitoring used the same ID. |
| F: browser kill | PASS. After SIGKILL of the managed browser, the replacement process restored every session with the same Queue ID and provider ID, and post-check lookups continued through the same IDs. |
| G: application restart | PASS. Provenance and IDs were restored, credentials came from the environment, and the dashboard IPs survived. The first checks and lookups used the original IDs. |
| G: restart without credentials | PASS (fail closed). `IPRoyalProxyConfigurationError` was raised before any browser started, with zero target requests, zero ipify lookups, and no direct egress. |
| H: Add | PASS. New session ID and new provider ID, proxied acquisition, and a baseline IP. Queue Checked and IP Checked are separate. |
| I: Replace | PASS. The replacement got a new ID and a new IP baseline. When a replacement failed (proxy upstream down), the original row kept its Queue ID, provider ID, Proxy IP, and IP Checked. |
| J: Delete | PASS. The row, assignment, and IP metadata were removed, followed by asynchronous state cleanup. Other assignments were unchanged. |
| K: Stop & Reset | PASS. Run provenance and all rows were removed. The fresh run's IDs are disjoint from every earlier ID. |
| Direct (local fixture) | PASS (unit). A Direct success causes one same-session Direct request plus one same-session lookup. A Direct failure leads to a proxied browser fallback plus exactly one lookup. An unresolvable proxy sends no Direct request and no lookup. |
| No-ID adoption | PASS (Prompt 2 integration). The existing ID is kept after adoption. |

## IP observation

- Initial observation happens after creation succeeds (after the Queue ID is persisted
  and the creation context has closed). No lookup is made for a failed creation.
- A post-status observation happens after every successful queue-status check, exactly
  once per logical check. Failed checks make no lookup.
- Manual Open policy: one lookup at the final close inspection (when it succeeds), and
  none while the window is open.
- IP Checked advances only on a successful lookup. Queue Checked is independent.
- A simulated IP change was recorded as `PROXY_IP_CHANGED` with no identity effect.
  Live exit changes are reported in the continuity section below.
- No ipify lookup was ever made without a sticky session
  (`ip_lookups_without_sticky_session = 0`). Dashboard polling performs no HTTP
  request (unit test with HTTP disabled).

## Security

- SQLite: no username or password bytes in the database file.
- Logs: every log record (message and structured context) was captured during each run
  and contained no credentials and no exit IPs. `proxy_ip_changed` and
  `proxy_ip_observation_failed` carry only the session ID and a classification.
- Dashboard and summary HTML: no credentials. The local operator dashboard shows the
  full current Proxy IP, as requested.
- Metrics: no credentials, IPs, or provider IDs. Labels are closed enums only
  (`proxy_ip_observations_total{result}`, `proxied_attempts_total{purpose}`,
  `proxy_failures_total{purpose,reason}`).
- Result JSON: no IPs, provider IDs, or credentials. Live results are salted-hash
  comparisons only.

## Resource cleanup

On both backends:

- managed browser processes returned to baseline (0 after shutdown);
- no worker or manual leases remained;
- operator work finished;
- no orphan processes remained.

## Provider continuity evidence (live IPRoyal; measured intervals only)

All runs used real IPRoyal Residential (GB, `2h`), production `SessionProxyResolver`,
Patchright, and production `ProxyIpObserver` against real ipify. Comparisons use salted
hashes; no IP was written to any report. Four live runs were made on 2026-10-01: one
30-minute checkpoint run (`docs/results/phase9_iproyal_live_result.json`) and three
quick runs.

| Interval / transition | Runs | Result |
| --- | --- | --- |
| Fresh BrowserContext, same sticky session | 4 | SAME ×4 |
| Browser-process restart | 4 | SAME ×4 |
| Independent Python/application process (reloads the row and re-reads credentials) | 4 | SAME ×4 |
| T+5 min, fully disconnected | 1 | SAME |
| T+30 min, fully disconnected | 1 | SAME |
| T+60 / T+90 / T+115 min | 0 | **NOT RUN** |
| Full `2h` lifetime | 0 | **NOT RUN** |

**Same-session browser vs observer exit.** For each new sticky session, the browser
context's ipify exit was compared with the production observer's exit a moment later,
through the same credentials:

- 11 of 12 pairs were SAME.
- 1 pair, at the very start of a new sticky session in the 30-minute run, was CHANGED.
  That session's later fresh-context, restart, T+5, and T+30 observations all matched
  the observer's value.

The application did everything it should here. Both requests used the identical
persisted sticky-session credentials, nothing rotated, and the identity was unaffected.
The mismatch is provider behaviour: the exit can still settle in the first seconds of a
brand-new sticky session. As a result, the dashboard's Proxy IP right after creation can
occasionally differ from the exit the acquisition page used. Later observations reflect
the settled exit, and a real change is surfaced as `PROXY_IP_CHANGED`.

Provider-duration conclusion: sticky continuity is evidenced up to **30 minutes**
disconnected. It is **not** evidenced for 60, 90, or 115 minutes or the configured
`2h`.

## Phase 9 gates

| # | Gate | Result |
| --- | --- | --- |
| 1 | One provider ID per IPRoyal QueueSession | PASS |
| 2 | Every new QueueSession gets a new ID | PASS |
| 3 | No automatic rotation | PASS |
| 4 | Initial acquisition proxied | PASS |
| 5 | Automatic monitoring reuses the ID | PASS |
| 6 | Refresh reuses the ID | PASS |
| 7 | Manual Open reuses the ID | PASS |
| 8 | Browser restart preserves the assignment | PASS |
| 9 | Application restart preserves the assignment | PASS |
| 10 | Direct cannot bypass the proxy | PASS (local fixture) |
| 11 | Proxy failures fail closed | PASS |
| 12 | IP observed through the same IPRoyal session | PASS (same credentials on every path; live: 11/12 browser and observer exits matched, 1 differed at session start; see above) |
| 13 | IP checked immediately after creation | PASS |
| 14 | IP checked after every successful queue check | PASS |
| 15 | Independent Queue Checked / IP Checked | PASS |
| 16 | ipify failure does not invalidate the queue check | PASS |
| 17 | IP change does not rotate the ID | PASS |
| 18 | Queue ID authoritative | PASS |
| 19 | Dashboard shows Proxy IP and IP Checked | PASS |
| 20 | Dashboard polling does no network or browser work | PASS |
| 21 | Credentials secret | PASS |
| 22 | Bounded persisted-many/live-few architecture | PASS (no per-session context, process, watcher, or sweep) |
| 23 | Resources return to baseline | PASS |

## Known limitations

- A crash between creation reservation and completion leaves a CREATING row without a
  Queue ID (Prompt 1). It is not counted or monitored.
- The gated benchmark harnesses remain proxy-disabled.
- Live provider continuity beyond the measured intervals is not claimed. A 30-minute
  PASS does not prove the configured `2h` lifetime.
- The first IP observation of a brand-new sticky session can occasionally differ from
  the exit the acquisition page used (provider settling; 1 of 12 live pairs).
