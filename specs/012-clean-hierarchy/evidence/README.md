# Spec 012 gate evidence

Complete gate logs, kept with the specification so acceptance decisions can be traced to the exact
source tree that produced them. Test-only data (fake keys such as `sk-e2e-…`); no real secret is
present. Never commit real credentials here.

## Phase 6A (accepted 2026-11-19 on the final run)

| Directory | Backend tree | Result |
|---|---|---|
| `phase-6a/final-run-790495a4/` | `790495a48348e6052854a973d810576502c61724` | **All gates passed** (the accepted run) |
| `phase-6a/failed-run-1-1d4e064c/` | `1d4e064cb3d0c8d661b2b84c970a1bd28287a6cd` | FAILED: `test_api_auth::test_ip_throttle` (rate-limit window boundary). Image not built; every later gate failed for that reason. **Not a successful run.** |
| `phase-6a/failed-run-2-263788ce/` | `263788cedbb6d5b8578200e5a038ce4bf21dfe95` | FAILED: `test_api_webchat::test_history_requires_the_session_token` (tamper could equal the real token). **Not a successful run.** |

Final-run verification (Phase 6B, 2026-11-19), re-read from the logs:

| Item | Log | Value |
|---|---|---|
| Source tree | `gate-summary.txt` line 1 | `TREE=790495a48348…` |
| Backend tests (in build) | `build.log` | `Ran 1242 tests` · `OK (skipped=86)` |
| Backend tests (in image, verbose) | `unit.log` | `Ran 1242 tests` · `OK (skipped=86)` |
| Specs 009–011 security | `gate-summary.txt` | 240 OK |
| Permission matrix | `gate-summary.txt` | 134 routes × 7 actors = 938 requests |
| Real MongoDB, 3 runs | `gate-summary.txt` | 50 each, OK (skipped=7: the gated load suite) |
| Load | `gate-summary.txt`, `load.json` | 7 OK |
| Frontend Jest | `jest.log` | 132 passed |
| Contract | `contract/routes.json` | 154 routes, no drift |
| Real-HTTP journeys | `e2e_http.log` | 11 OK |
| Chromium | `playwright.log` | 23 passed |

The working tree at the start of Phase 6B equalled tree `790495a4` exactly (`git diff` of the two
trees showed only the two Phase 6B regression tests added afterwards).

Regression coverage for the two failed runs (Phase 6B):
- `tests/unit/test_core_throttle.py::test_boundary_sweep_never_allows_more_than_the_limit` (fails with
  the pre-fix estimate; mutation-checked) and `test_login_window_at_quarter_hour_mirrors_the_gate_failure`.
- `tests/unit/test_api_webchat.py::TamperedTokenRegressionTests` (every possible final character) and the
  shared `tampered_token()` helper used by the history test.

`gates6a.sh` is the exact script that produced the runs. `SHA256SUMS` covers every file.
