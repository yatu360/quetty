# IPRoyal Residential Proxy Spike Results

## Date and Environment

- Date/time: 2026-09-30T23:19:51.650413+00:00
- OS: macOS-26.5.1-arm64-arm-64bit-Mach-O
- Python version: 3.14.7
- Quetty commit: `e829e5a28de53245110ffb4dff507b95d1c3fe3a`
- Browser backend: Patchright with installed Google Chrome
- Patchright version: 1.63.0
- Installed Chrome version: 154.0.8037.59
- Provider: IPRoyal Residential
- Configured country: `gb`
- Configured lifetime: `2h`
- Logical session count: 3
- Proxy-routed request count: 44 (initial run, finalization run, and one geo fallback request)
- IP diagnostic service: `https://api.ipify.org?format=json`
- Geo diagnostic service: `https://api.country.is/`
- Disconnect checkpoints completed: T+5m, T+30m
- Live gate state: enabled
- Missing configuration: none
- Configuration errors: none

No Queue-it or ordinary website was contacted.

## Configuration Safety

- Credentials source: git-ignored .env plus process environment overrides.
- `.env` permission status: 0o600.
- Credentials and constructed effective passwords were held in memory only.
- Leakage-audit result: PASS.
- Full observed IPs were not retained in human or ordinary machine results.
- Cross-process comparison used a salted SHA-256 digest.
- Protected temporary state cleanup: removed

## Test Results

| Test | Result | Observations | SAME | CHANGED | Errors | Masked example | Evidence |
|---|---:|---:|---:|---:|---:|---|---|
| Basic proxy connectivity | PASS | 1 | 0 | 0 | 0 | 84.71.214.xxx | Patchright navigation succeeded in 1.896s; context accounting returned to zero. |
| Bypass check | PASS | 2 | 0 | 0 | 0 | 84.71.214.xxx | proxy-observed exit differed from separately observed direct egress |
| UK geo verification | PASS | 1 | 0 | 0 | 0 | — | GB confirmed by the proxied lightweight geo endpoint |
| Fresh-context affinity | PASS | 9 | 6 | 0 | 0 | 84.71.214.xxx | 6 comparisons stayed SAME across completely new contexts |
| Browser-process restart affinity | PASS | 3 | 3 | 0 | 0 | 84.71.214.xxx | all tested identities remained SAME after managed browser restart |
| Independent Python/application restart affinity | PASS | 1 | 1 | 0 | 0 | 84.71.214.xxx | a fresh Python process and fresh Patchright manager observed the SAME IP hash |
| 5-minute disconnected affinity | PASS | 1 | 1 | 0 | 0 | 84.71.214.xxx | SAME vs T+0 after 5.0 minutes |
| 30-minute disconnected affinity | PASS | 1 | 1 | 0 | 0 | 84.71.214.xxx | SAME vs T+0 after 30.0 minutes |
| 60-minute disconnected affinity | NOT RUN | 0 | 0 | 0 | 0 | — | ignored after operator reduced the required continuity window to 30 minutes |
| 90-minute disconnected affinity | NOT RUN | 0 | 0 | 0 | 0 | — | ignored after operator reduced the required continuity window to 30 minutes |
| 115-minute disconnected affinity | NOT RUN | 0 | 0 | 0 | 0 | — | ignored after operator reduced the required continuity window to 30 minutes |
| Three-session concurrency | PASS | 3 | 0 | 0 | 0 | 84.71.214.xxx | 3/3 succeeded; collisions=0; distinct configurations retained |
| Failure cleanup | PASS | 2 | 0 | 0 | 0 | — | missing required fields, malformed server, invalid session ID, unreachable endpoint, cleanup, and recovery behaved safely; invalid auth=not requested |
| Secret leakage audit | PASS | 0 | 0 | 0 | 0 | — | credentials and authenticated proxy URIs were absent from logs, reprs, JSON, markdown inputs, and temporary artifacts |
| Final context count | PASS | 0 | 0 | 0 | 0 | — | final active contexts=0 |
| Final managed browser-process count | PASS | 0 | 0 | 0 | 0 | — | final managed processes=0; OS orphan check=False |

## Fresh-context cycle detail

| Logical reference | Observation | Result | Duration | Masked IP | Error |
|---|---:|---|---:|---|---|
| iproyal-spike-1 | 1 | BASELINE | 0.871s | 84.71.214.xxx | none |
| iproyal-spike-1 | 2 | SAME | 0.597s | 84.71.214.xxx | none |
| iproyal-spike-1 | 3 | SAME | 0.671s | 84.71.214.xxx | none |
| iproyal-spike-2 | 1 | BASELINE | 1.059s | 95.144.23.xxx | none |
| iproyal-spike-2 | 2 | SAME | 0.805s | 95.144.23.xxx | none |
| iproyal-spike-2 | 3 | SAME | 0.957s | 95.144.23.xxx | none |
| iproyal-spike-3 | 1 | BASELINE | 0.744s | 90.205.17.xxx | none |
| iproyal-spike-3 | 2 | SAME | 0.663s | 90.205.17.xxx | none |
| iproyal-spike-3 | 3 | SAME | 0.697s | 90.205.17.xxx | none |

## Long disconnect detail

| Checkpoint | Elapsed | Result vs T+0 | Masked IP | Full browser restart | Fresh Python process | Error |
|---|---:|---|---|---|---|---|
| T+5m | 5.0m | SAME | 84.71.214.xxx | yes | no | none |
| T+30m | 30.0m | SAME | 84.71.214.xxx | yes | no | none |

## Per-logical-session summary

| Logical reference | Provider session ID | Observations | SAME | CHANGED | Masked IP | Browser restart | Python restart | Longest SAME disconnect | Errors |
|---|---|---:|---:|---:|---|---|---|---|---|
| iproyal-spike-1 | `e6d41917` | 8 | 2 | 0 | 84.71.214.xxx | SAME | SAME | 30.0m | UNKNOWN |
| iproyal-spike-2 | `440feaed` | 5 | 2 | 0 | 95.144.23.xxx | SAME | UNKNOWN | UNKNOWN | none |
| iproyal-spike-3 | `0917ff64` | 5 | 2 | 0 | 90.205.17.xxx | SAME | UNKNOWN | UNKNOWN | none |

## Provider behavior observations

- Patchright/Quetty integration failure: none observed
- IPRoyal authentication/configuration failure: no failure observed
- Residential provider IP reassignment: 0 exact comparison change(s).
- Residential peer disappearance: not separately distinguishable from provider reassignment.
- Network/timeout failure: see sanitized errors and Failure cleanup.
- Unknown/inconclusive behavior: every UNKNOWN or NOT RUN row remains unclaimed.
- Cross-session collisions: 0; collisions are not failures.
- An IP reassignment is provider continuity evidence, not Queue identity corruption.

## Cleanup

- Active contexts after run: 0
- Managed browser processes after shutdown: 0
- Temporary files remaining: 0
- Protected restart state removed: True
- Orphan-process result: False

## Decision Matrix

1. Can Patchright connect through IPRoyal Residential? PASS — Patchright navigation succeeded in 1.896s; context accounting returned to zero.
2. Is traffic demonstrably proxied? PASS — proxy-observed exit differed from separately observed direct egress
3. Is the proxy exit UK/GB? PASS — GB confirmed by the proxied lightweight geo endpoint
4. Can proxy configuration be applied per temporary BrowserContext? PASS — 3/3 succeeded; collisions=0; distinct configurations retained
5. Does the same IPRoyal session ID retain its IP across fresh BrowserContexts? PASS — 6 comparisons stayed SAME across completely new contexts
6. Does it retain its IP across managed browser-process restart? PASS — all tested identities remained SAME after managed browser restart
7. Does it retain its IP across independent Python/application restart? PASS — a fresh Python process and fresh Patchright manager observed the SAME IP hash
8. Same IP after 5 minutes disconnected? PASS — SAME vs T+0 after 5.0 minutes
9. Same IP after 30 minutes disconnected? PASS — SAME vs T+0 after 30.0 minutes
10. Same IP after 60 minutes disconnected? NOT RUN — ignored after operator reduced the required continuity window to 30 minutes
11. Same IP after 90 minutes disconnected? NOT RUN — ignored after operator reduced the required continuity window to 30 minutes
12. Same IP after approximately 115 minutes disconnected? NOT RUN — ignored after operator reduced the required continuity window to 30 minutes
13. What is the longest tested disconnected interval retaining the T+0 baseline IP? PASS — 30.0 minutes
14. Can three logical sessions operate concurrently? PASS — 3/3 succeeded; collisions=0; distinct configurations retained
15. Did any test require keeping the browser/context/proxy connection alive? PASS — no; disconnect checkpoints retained no browser or proxy connection
16. Did credentials remain absent from ordinary persistence/logs/results? PASS — credentials and authenticated proxy URIs were absent from logs, reprs, JSON, markdown inputs, and temporary artifacts
17. Did resources return to baseline? PASS — final resource counts returned to baseline
18. Did the IP change at any point before the configured `2h` lifetime? PASS — no change in completed checkpoints

## Final Outcome

`SPIKE_PASS`

Evidence is limited to the exact configuration and intervals recorded above. The operator
reduced the required continuity window to 30 minutes during the run; this result makes no
claim about 60, 90, 115, or the full configured two-hour lifetime.

## Production recommendation

A future implementation may use:

`QueueSession -> immutable IPRoyal proxy-session assignment -> deterministic 8-character provider session ID -> reconstruct password using country + session + lifetime -> create temporary proxied Patchright BrowserContext -> restore authoritative Queue ID -> verify identity -> inspect -> persist -> close/park`

Queue ID remains authoritative. The residential IP is continuity/diagnostic metadata only;
a change should surface as `PROXY_IP_CHANGED`.

`Design and implement persisted per-session IPRoyal Residential proxy assignments in Quetty using the proven UK sticky-session reconstruction mechanism, while keeping Queue ID authoritative and validating proxy-IP continuity on every restore.`
