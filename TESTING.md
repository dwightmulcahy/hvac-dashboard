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

## CI

`.github/workflows/tests.yml` runs the full suite plus `pyflakes` and
a dashboard JS syntax check on every push/PR to `main`, `develop`, and
`release`.
