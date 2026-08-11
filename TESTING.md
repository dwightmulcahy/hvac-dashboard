# Testing

## Setup

```bash
pip install -r requirements-dev.txt --break-system-packages
```

## Run all tests

```bash
pytest
```

## Run a specific file or test

```bash
pytest tests/test_max_temp.py
pytest tests/test_max_temp.py::test_hysteresis_turns_off_one_degree_below_max
```

## Run with coverage

```bash
pytest --cov=api --cov=auth --cov=state --cov=models --cov=worker --cov=routers --cov-report=term-missing
```

As of the last full pass: **91% overall** (1678 statements, 159 missing), with every file at 81%+. The remaining gaps are mostly `_background_worker`'s outer `while True` loop shell (its sub-functions — polling, scheduling, watchdog — are all tested directly and thoroughly; the loop shell itself is inherently low-value to unit test) and scattered exception-handling edge cases in file I/O and network discovery.

## What's covered

| File | Covers |
|---|---|
| `tests/test_state.py` | State load/save, atomic writes, backup rotation, log file persistence, rate/watt calculations |
| `tests/test_state_gaps.py` | Backup rotation limits, `save_state()` wrapper, log file rotation, corrupt-line tolerance |
| `tests/test_models.py` | Pydantic request model validation |
| `tests/test_auth.py` | Password hashing, tokens, role checks, login rate limiting/lockout |
| `tests/test_auth_gaps.py` | Lockout/token expiry, every `auth_middleware` branch, full users CRUD (add/delete/role/force-reset validation paths) |
| `tests/test_schedules.py` | Schedule command building, conflict detection |
| `tests/test_router_schedules_gaps.py` | Schedule update/delete/toggle endpoints, conflict reporting on update |
| `tests/test_max_temp.py` | Max-temp guard trigger, hysteresis, guard-hours behavior (including the two production bugs this project hit and fixed) |
| `tests/test_worker_network.py` | `worker.py`'s device network I/O: fetch state/sensors/exchange-rate, send command/switch — path fallback, retries, failure handling |
| `tests/test_worker_polling.py` | `_poll_device` — offline/recovery detection, sensor merging, mode-change logging, temp lock override, retry-queue draining. Includes the regression test for a real bug found here: `_fetch_sensors` never fetched beeper state, so poll-time beeper sync was dead code (fixed) |
| `tests/test_worker_scheduling.py` | `_check_schedules`/`_check_missed_schedules` — actual firing/timing logic (distinct from `_build_schedule_commands`), overnight end-time day-of-week handling, retry queuing |
| `tests/test_endpoints.py` | Full HTTP integration tests via FastAPI's `TestClient` — auth flow, role enforcement, device/schedule CRUD |
| `tests/test_router_devices_control.py` | Poll/cmd/lock/display-toggle/beeper endpoints — not-found branches, retry queuing, logging |
| `tests/test_router_devices_crud_gaps.py` | Temp-history, reorder, host-change-clears-retry-state |
| `tests/test_router_devices_discovery.py` | OTA upload, network discovery scan (found/skip/already-configured), health history |
| `tests/test_router_settings_gaps.py` | Live exchange-rate fetch/fallback, verbose-logging toggle log |
| `tests/test_router_system.py` | Detailed health check with real devices, vacation mode on/off, Watchtower webhook forwarding |
| `tests/test_router_usage.py` | Usage summary/rolling30/CSV export aggregation, log filtering |
| `tests/test_backup_restore.py` | Backup export shape (no internal fields, no users), restore merge strategies (devices merge, schedules replace, settings merge), full round-trip |
| `tests/test_api_lifespan.py` | Startup log-loading from disk |

## How isolation works

Every test gets its own throwaway `DATA_FILE`/`LOG_FILE` (via `tmp_path`)
and a freshly-imported set of modules (`api`, `auth`, `worker`, `state`,
`models`, and every `routers.*` submodule), so tests never touch your
real `/data/hvac_state.json` or make real network calls to physical AC
dongles. Device network calls are mocked per-test with `mocker.patch.object(httpx.AsyncClient, ...)`
— see `tests/test_worker_network.py` for the pattern, or use the
`mock_device_response` fixture in `conftest.py` for tests that don't
care about the exact device response shape.

**Every module that imports `_state` from `state.py` must be in the
reload list** in `conftest.py`'s `api_module` fixture. Missing one
causes a specific, nasty bug: a test can pass in isolation but fail
depending on what ran before it in the same pytest session, because
the un-reloaded module keeps referencing a stale `_state` dict from an
earlier test. This has happened for real, more than once, while
building out this suite — if you add a new module that imports
`_state` (a new router, a new worker submodule), add it to that list.

## Adding a regression test for a bug

This project has hit several subtle bugs during development (duplicate
log entries, timezone mismatches, schedule timing drift, max-temp
hysteresis not firing, etc.). When you fix one, add a test that would
have caught it — `test_max_temp.py` has good examples of this pattern
(each test's docstring explains which real bug it guards against).

---

## JavaScript tests

`hvac-dashboard.html` is a single-file frontend with no build step, so
its tests don't use npm/jest/vitest — they use Node's **built-in**
test runner (`node:test`), which needs zero installation on any
machine that already has Node.

```bash
node --test tests-js/*.test.js
```

### How it works

`tests-js/extract.js` pulls specific pure utility functions (`safeFloat`,
`fmtTemp`, `estWatts`, `fmtDays`, `effectiveRateUsd`, etc.) directly out
of `hvac-dashboard.html`'s `<script>` tag at test-run time, using
sentinel comments placed directly in the dashboard source:

```js
// ── TESTABLE:region-name:start ──
...code...
// ── TESTABLE:region-name:end ──
```

Earlier versions of this extractor used brace-counting (broke on
`${...}` inside a template literal) and then adjacent-function-name
boundaries (broke when an unrelated neighboring function got renamed)
— sentinels fix both failure modes, since a region's boundaries are
owned by whoever wrote it, not by whatever happens to be nearby. If
`extract.js`'s `REGIONS` list doesn't match every sentinel actually
present in the dashboard, extraction throws a clear error rather than
silently leaving a region untested.

Only pure, DOM-independent functions are covered this way — things
like temperature/cost/day formatting and watt estimation. Functions
that touch `document`, `fetch`, or `localStorage` directly (rendering,
event handlers, API calls) aren't unit tested; those are covered by
the syntax check step and by manual testing against a running
instance.

### Adding a new function to test

1. Wrap the function (and anything it depends on) in a new sentinel
   pair in `hvac-dashboard.html`:
   ```js
   // ── TESTABLE:my-new-region:start ──
   function myNewFunction() { ... }
   // ── TESTABLE:my-new-region:end ──
   ```
2. Add `"my-new-region"` to the `REGIONS` array in `extract.js`.
3. If your function reads a module-level `const`/`let` that isn't
   already bridged, add it to the `__bridge` object near the bottom
   of `loadDashboardFunctions()`.
4. Add `test(...)` blocks to `dashboard-functions.test.js` using
   `node:assert/strict`.

---

## CI

`.github/workflows/tests.yml` runs the full pytest suite, `pyflakes`,
the dashboard JS syntax check, and the JS unit test suite on every
push/PR to `main`, `develop`, and `release`.
