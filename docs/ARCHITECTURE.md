# Architecture

The backend was originally a single 2,240-line `api.py`. It's now split
into focused modules with a strict one-way dependency graph, backed by
a 404-test pytest suite (99% line coverage). This doc explains the
shape of it so the next person touching this code (including
future-you) doesn't have to re-derive it from scratch.

This doc covers the Python backend only. The two frontends —
`hvac-dashboard.html` and the wall-mounted `kiosk.html` — are both
single-file, no-build-step HTML apps served directly by the backend;
see [`KIOSK.md`](./KIOSK.md) for the kiosk's Raspberry Pi hardware
setup and [`TESTING.md`](./TESTING.md) for how both are tested.

## Module map

```
logging_config.py  ← no dependencies on anything else here — must be
                       configured before state.py is even imported,
                       since state.py logs at import time (see below)
    ↑
state.py       ← owns _state, persistence, JSONL log file, small helpers
    ↑
auth.py               ← hashing, tokens, roles, rate limiting, /auth/* router
maintenance_logic.py  ← pure maintenance-status computation (days vs runtime_hours)
notify.py             ← generic outbound notification webhook
    ↑
worker.py      ← background loop, polling, max-temp guard, scheduling,
                  maintenance-overdue detection, nightly dongle reboot
    ↑
routers/*.py   ← one file per feature area, each a thin FastAPI APIRouter
    ↑
api.py         ← app assembly only: creates FastAPI(), wires middleware,
                  starts the background worker, includes every router
```

Everything imports `_state` (and friends) from `state.py`. Nothing
imports "up" the chain — `state.py` never imports from `auth.py` or
`worker.py`, and `worker.py` never imports from any router. This keeps
the dependency graph acyclic and easy to reason about.

`maintenance_logic.py` and `notify.py` exist as their own thin modules
(rather than living inside `worker.py` or `routers/maintenance.py`)
specifically so `worker.py` can use them without importing anything
from `routers/` — the maintenance-overdue check that runs in the
background loop and the status computation that runs on every
`GET /maintenance` need the exact same logic, and neither module is
allowed to import the other given the one-way graph above.

| File | Lines | Responsibility |
|---|---|---|
| `logging_config.py` | ~140 | `configure_logging()` (text/JSON format toggle via `LOG_FORMAT`) and `AccessLogMiddleware` (per-request method/path/status/duration logging) |
| `state.py` | ~335 | `DEFAULT_STATE`/`DEVICE_DEFAULTS`/`SCHEDULE_DEFAULTS`/`MAINTENANCE_DEFAULTS`, the shared `_state` dict, atomic JSON persistence + backup rotation, the JSONL log file, rate/watt calculation helpers |
| `models.py` | ~50 | Pydantic request models (`DeviceConfig`, `CommandPayload`, `ScheduleConfig`, `MaintenanceConfig`) |
| `auth.py` | ~495 | Password hashing, bearer tokens, role checks, login rate limiting, the default admin bootstrap, the recovery key, `auth_middleware`, and the full `/auth/*` router |
| `maintenance_logic.py` | ~50 | Pure maintenance-status computation — days-based and runtime_hours-based overdue/due-soon logic, shared by `worker.py` and `routers/maintenance.py` |
| `notify.py` | ~30 | Generic outbound notification webhook (device-offline, maintenance-overdue, schedule-failure alerts) — separate from the Watchtower-specific webhook in `routers/system.py` |
| `worker.py` | ~870 | The 24/7 background loop: device polling, the max-temp guard (with hysteresis + guard hours), schedule firing (including missed-schedule recovery), the watchdog, health/usage tracking, nightly dongle reboot, maintenance-overdue notification |
| `routers/devices_crud.py` | ~85 | Device list/add/update/delete/reorder |
| `routers/devices_control.py` | ~130 | On-demand poll, commands, temp lock, display toggle, beeper |
| `routers/devices_discovery.py` | ~155 | OTA firmware upload, network discovery scan, health history |
| `routers/schedules.py` | ~90 | Schedule CRUD + conflict detection |
| `routers/settings.py` | ~80 | Settings, exchange rate, backup/restore |
| `routers/usage.py` | ~130 | Usage reports, CSV export, logs |
| `routers/system.py` | ~200 | Health checks (incl. Uptime Kuma push target), vacation mode, Watchtower webhook, root status, usage reset |
| `routers/maintenance.py` | ~95 | Maintenance reminder CRUD, days/runtime_hours status, mark-complete with service history |
| `api.py` | ~110 | Pure app assembly + CORS configuration |

55 endpoints total across `auth.py` (12) + the eight router files (43).

## Why the split happened this way

The original monolith made three kinds of bugs easy to introduce and
hard to catch:

1. **Duplicate state.** Early refactor passes left `api.py` with its
   own copy of `_state`/`DEFAULT_STATE` even after `state.py` existed,
   because nothing forced every consumer to actually import from the
   new module. If two parts of the app hold separate `_state` dicts,
   writes to one are invisible to the other — logins in one, devices
   in the other, silently diverging. This happened for real during
   the `auth.py` extraction and again during the `worker.py`
   extraction. The fix each time was mechanical: delete the duplicate,
   import from `state.py`, verify with the test suite.

2. **Cross-boundary sweeps.** Extracting a contiguous block of code by
   line range is risky when a REST endpoint happens to be defined in
   the middle of what looks like "worker code." This happened once
   (`GET /devices/{host}/temp-history` got pulled into `worker.py` by
   mistake) and was caught by grepping the extracted file for stray
   `@app.` decorators before deleting the original.

3. **Test isolation gaps.** `conftest.py`'s `api_module` fixture has to
   reload *every* module that transitively imports `_state` from
   `state.py` between tests — `auth`, `worker`, `maintenance_logic`,
   `notify`, and every `routers.*` submodule. Miss one and a test can
   pass in isolation but fail depending on what ran before it in the
   same pytest session, because the missed module keeps holding a
   stale `_state` reference from an earlier test's `state.py`
   instance. This bug class was hit multiple times (once per new
   module added to the reload list — most recently when
   `routers/maintenance.py`, `maintenance_logic.py`, and `notify.py`
   were added and `conftest.py` wasn't updated to match) and is now
   explicitly documented in the fixture's docstring so it isn't
   rediscovered again. `tests/test_api_lifespan.py` keeps its own
   separate copy of this reload list for the same reason (it needs a
   fresh `api` import to test env-var-gated startup behavior like
   `CORS_ALLOWED_ORIGINS`) — if you update one, check the other.

If you add a new module that imports `_state` — a new router, a new
worker submodule, whatever — **add it to the reload list in
`tests/conftest.py`'s `api_module` fixture**, or you will eventually
hit bug #3 above.

## Verification pattern used for every refactor stage

Each extraction (state → auth → worker → routers → the devices router
split) followed the same sequence, and it's worth repeating for any
future one:

1. Map every function/endpoint being moved and everything it depends
   on that lives outside the block being extracted.
2. Build the new file with content-matched string replacement (not
   line-number slicing) so a stale line count from a previous edit
   can't silently corrupt an unrelated part of the file.
3. Run `ruff check` on every touched file — catches unused imports,
   undefined names, duplicate definitions, and more (this replaced a
   bare `pyflakes` call — see `pyproject.toml` for the specific rule
   selection and why it's not ruff's full default set).
4. Verify the endpoint count before and after matches exactly (a
   simple `grep -c '@router\.'` sum across all files).
5. Run the full pytest suite, then run it again with `pytest-randomly`
   using different seeds — isolation bugs are nondeterministic and
   only show up depending on test order. `pytest-randomly` randomizes
   order automatically once installed (no flags needed), so a plain
   `pytest` run already gets this for free in an environment that has
   it — but running it a few extra times with explicit
   `--randomly-seed=N` values is still worth doing deliberately after
   any change to the module-reload lists above, since a single random
   run can still get lucky.
6. Diff every changed file between the working copy and wherever it's
   being delivered (outputs directory, repo, etc.) to catch partial
   copies.
7. As a final check, populate a *completely fresh* directory from only
   the delivered files (not the working copy) and run the test suite
   there — this is the only check that actually proves what you're
   about to ship works, independent of local state.

## Testing

See [`TESTING.md`](./TESTING.md) for how to run the suite locally and
what each test file covers. Briefly: 404 tests across state
persistence, auth (hashing/tokens/rate-limiting), the max-temp guard
(including regression tests for the hysteresis and guard-hours bugs
hit during development), schedule command building and conflict
detection, maintenance status computation and overdue notification,
CORS configuration, full HTTP integration tests via FastAPI's
`TestClient`, and a backup/restore round-trip. 99% line coverage
overall.
