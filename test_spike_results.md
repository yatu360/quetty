# Primed Residential Proxy Spike Results

## Date and Environment

- Date/time: 2026-09-30T13:08:18.097200+00:00
- OS: macOS-26.5.1-arm64-arm-64bit-Mach-O
- Python version: 3.14.7
- Quetty commit: `73e14faba6de3887e87b3fc8fa10b26b92d493e2`
- Browser backend: Patchright with installed Google Chrome
- Patchright version: 1.63.0
- Installed Chrome version: 154.0.8037.59
- Primed product: Residential
- Logical session count: 3
- Proxy-routed request/navigation count: 28 (22 formal run + 6 preliminary connectivity diagnostics)
- Diagnostic IP service used: `https://api.ipify.org?format=json`
- Inactivity intervals tested: 10s, 60s
- Live gate: enabled
- Missing configuration: none
- Configuration errors: none

No Queue-it or other production/unauthorised target was contacted.

## Configuration Safety

- Credentials source: git-ignored `.env` (complete operator connection string parsed only in memory).
- Environment file mode: 0o600
- Credentials were not written to SQLite or any ordinary result file.
- Credential leakage audit: PASS
- The operator supplied a temporary credential in the external conversation; that
  surface is outside the repository artifact audit and the credential must be rotated.
- Raw exit IPs were compared in memory and were not retained in this report.
- Cross-process comparison uses a salted SHA-256 value, never the raw IP.
- Protected restart artifacts: removed

## Test Results

| Test | Result | Observations | SAME | CHANGED | Errors | Masked example | Evidence |
|---|---:|---:|---:|---:|---:|---|---|
| Basic proxy connectivity | PASS | 1 | 0 | 0 | 0 | 90.212.110.xxx | Patchright navigation succeeded in 0.744s; context accounting returned to zero. |
| Fresh-context affinity | FAIL | 9 | 3 | 3 | 0 | 90.212.110.xxx | 3 exact IP changes across fresh BrowserContexts |
| Browser-process restart affinity | FAIL | 3 | 2 | 1 | 0 | 217.42.69.xxx | clean pre-restart shutdown=True; changed=1; errors=0 |
| Independent Python/application restart affinity | FAIL | 1 | 0 | 1 | 0 | 90.212.110.xxx | fresh-process comparison changed or failed |
| Inactivity 10s | PASS | 2 | 1 | 0 | 0 | 90.212.110.xxx | SAME after 10s completely disconnected |
| Inactivity 60s | PARTIAL | 2 | 0 | 1 | 0 | 51.198.250.xxx | provider reassigned the exit IP after 60s; not a Quetty code error |
| Optional longer inactivity | NOT RUN | 0 | 0 | 0 | 0 | — | not run |
| Three-session concurrency | PASS | 3 | 0 | 0 | 0 | 51.198.250.xxx | 3/3 succeeded; collisions=0; distinct configurations retained |
| Bypass check | PASS | 2 | 0 | 0 | 0 | 90.212.110.xxx | proxy-observed exit differed from separately observed direct egress |
| Failure cleanup | PASS | 2 | 0 | 0 | 0 | — | missing password, malformed server, invalid template, unreachable endpoint, cleanup, and recovery behaved safely; invalid auth=not requested |
| Secret leakage audit | PASS | 0 | 0 | 0 | 0 | — | credentials and authenticated proxy URIs were absent from logs, reprs, JSON, markdown inputs, and temporary artifacts |
| Final context count | PASS | 0 | 0 | 0 | 0 | — | final active contexts=0 |
| Final managed browser-process count | PASS | 0 | 0 | 0 | 0 | — | final managed processes=0; OS orphan check=False |

### Fresh-context cycle detail

| Logical session | Observation | Result vs prior context | Duration | Masked IP | Error |
|---|---:|---|---:|---|---|
| proxy-spike-1 | 1 | BASELINE | 0.854s | 90.212.110.xxx | none |
| proxy-spike-1 | 2 | SAME | 0.610s | 90.212.110.xxx | none |
| proxy-spike-1 | 3 | CHANGED | 0.795s | 217.42.69.xxx | none |
| proxy-spike-2 | 1 | BASELINE | 0.690s | 165.120.78.xxx | none |
| proxy-spike-2 | 2 | CHANGED | 0.620s | 37.152.227.xxx | none |
| proxy-spike-2 | 3 | SAME | 0.536s | 37.152.227.xxx | none |
| proxy-spike-3 | 1 | BASELINE | 0.806s | 109.181.228.xxx | none |
| proxy-spike-3 | 2 | CHANGED | 0.579s | 86.161.182.xxx | none |
| proxy-spike-3 | 3 | SAME | 0.588s | 86.161.182.xxx | none |

## Per-Logical-Session Summary

| Logical session reference | Observations | SAME / CHANGED | Masked IP | Browser restart | Process restart | Longest disconnected interval with same IP | Sanitized errors |
|---|---:|---|---|---|---|---|---|
| proxy-spike-1 | 11 | 1 / 1 | 51.198.250.xxx | SAME | CHANGED | 10s | UNKNOWN |
| proxy-spike-2 | 5 | 1 / 1 | 90.201.210.xxx | SAME | UNKNOWN | UNKNOWN | none |
| proxy-spike-3 | 5 | 1 / 1 | 46.65.23.xxx | CHANGED | UNKNOWN | UNKNOWN | none |

## Provider-Behavior Observations

- Quetty/browser integration failures: none observed
- Primed authentication failures: none observed
- Provider IP reassignment: 3 immediate fresh-context changes; inactivity results are shown separately.
- Provider timeout/network failures: see Failure cleanup and sanitized session errors.
- Unknown/inconclusive behavior: every NOT RUN or UNKNOWN row remains unclaimed.
- Cross-session IP collisions are observational only and are not failures: 0 collision(s).

## Cleanup

- Active contexts after run: 0
- Managed browser processes after shutdown: 0
- Temporary files remaining: 0
- Protected spike-state file: removed
- Orphan process: no

## Decision Matrix

1. Can Patchright connect through Primed Residential? PASS — Patchright navigation succeeded in 0.744s; context accounting returned to zero.
2. Is traffic demonstrably proxied? PASS — proxy-observed exit differed from separately observed direct egress
3. Can the proxy be configured per temporary BrowserContext? PASS — all temporary contexts completed with distinct logical proxy configurations; IP reassignment is assessed separately
4. Does the same Primed sticky-session identity retain its IP after BrowserContext destruction? FAIL — 3 exact IP changes across fresh BrowserContexts
5. Does it retain its IP after managed browser-process destruction? FAIL — clean pre-restart shutdown=True; changed=1; errors=0
6. Does it retain its IP across a new Python/application process? FAIL — fresh-process comparison changed or failed
7. What is the longest tested disconnected interval that retained the same IP? PASS — 10s
8. Were multiple logical proxy sessions usable concurrently? PASS — 3/3 succeeded; collisions=0; distinct configurations retained
9. Did any test require keeping a browser/context alive to preserve affinity? PARTIAL — no context/browser was retained, but affinity was not consistently preserved after destruction
10. Did credentials remain absent from normal persistence/logging/results? PASS — credentials and authenticated proxy URIs were absent from logs, reprs, JSON, markdown inputs, and temporary artifacts
11. Did all resources cleanly return to baseline? PASS — final manager accounting is zero
12. Were any IP changes observed, and under what condition? PARTIAL — see the per-test SAME/CHANGED counts; no behavior is generalized beyond tested intervals

## Final Outcome

`SPIKE_PARTIAL`

This outcome is limited to the exact endpoint, host, configuration, and intervals recorded above.

## Implementation Recommendation

Do not implement production proxy assignment yet. Collect the missing or failed live evidence first; Queue ID remains authoritative and proxy IP must never become identity.

Primed syntax was not guessed. The operator must supply the exact documented username template through environment configuration; see [https://primed-proxies.gitbook.io/primed-proxies-documentation/residential-proxies/making-requests](https://primed-proxies.gitbook.io/primed-proxies-documentation/residential-proxies/making-requests).
