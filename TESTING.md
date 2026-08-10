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
pip install pytest-cov --break-system-packages
pytest --cov=api --cov=state --cov=models --cov-report=term-missing
```

## What's covered

| File | Covers |
|---|---|
| `tests/test_state.py` | State load/save, atomic writes, backup rotation, log file persistence, rate/watt calculations |
| `tests/test_models.py` | Pydantic request model validation |
| `tests/test_auth.py` | Password hashing, tokens, role checks, login rate limiting/lockout |
| `tests/test_schedules.py` | Schedule command building, conflict detection |
| `tests/test_max_temp.py` | Max-temp guard trigger, hysteresis, guard-hours behavior (including the two production bugs this project hit and fixed) |
| `tests/test_endpoints.py` | Full HTTP integration tests via FastAPI's `TestClient` — auth flow, role enforcement, device/schedule CRUD |
| `tests/test_backup_restore.py` | Backup export shape (no internal fields, no users), restore merge strategies (devices merge, schedules replace, settings merge), full round-trip |

## How isolation works

Every test gets its own throwaway `DATA_FILE`/`LOG_FILE` (via `tmp_path`)
and a freshly-imported `api`/`state` module, so tests never touch your
real `/data/hvac_state.json` or make real network calls to physical AC
dongles. Device network calls are mocked in `conftest.py`
(`mock_device_response` fixture) — extend that fixture (or write a
custom mock inline in a test) if you need to simulate a specific
device response, like an offline unit or a particular temperature.

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
of `hvac-dashboard.html`'s `<script>` tag at test-run time, using exact
string boundaries — not brace-counting. (An earlier bug in this project
came from a brace-counter getting confused by `${...}` inside a
template literal; the extractor deliberately avoids that entire class
of bug.) If `hvac-dashboard.html` changes shape — a function gets
renamed or reordered — extraction throws a clear error telling you
which marker in `extract.js` needs updating, rather than silently
testing stale code.

Only pure, DOM-independent functions are covered this way — things
like temperature/cost/day formatting and watt estimation. Functions
that touch `document`, `fetch`, or `localStorage` directly (rendering,
event handlers, API calls) aren't unit tested; those are covered by
the syntax check step and by manual testing against a running
instance.

### Adding a new function to test

1. Find (or extend) a `region` in `extract.js` whose start/end markers
   already bracket your function, or add a new `extractBetween(...)`
   call with a unique start marker (your function's signature) and a
   unique end marker (whatever comes right after it in the file).
2. If your function reads a module-level `const`/`let` that isn't
   already bridged, add it to the `__bridge` object near the bottom
   of `loadDashboardFunctions()`.
3. Add `test(...)` blocks to `dashboard-functions.test.js` using
   `node:assert/strict`.

---

## CI

`.github/workflows/tests.yml` runs the full pytest suite, `pyflakes`,
the dashboard JS syntax check, and the JS unit test suite on every
push/PR to `main`, `develop`, and `release`.
