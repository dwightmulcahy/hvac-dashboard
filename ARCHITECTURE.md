# Architecture

The backend was originally a single 2,240-line `api.py`. It's now split
into focused modules with a strict one-way dependency graph, backed by
a 90-test pytest suite. This doc explains the shape of it so the next
person touching this code (including future-you) doesn't have to
re-derive it from scratch.

## Module map

```
state.py       ← owns _state, persistence, JSONL log file, small helpers
    ↑
auth.py        ← hashing, tokens, roles, rate limiting, /auth/* router
worker.py      ← background loop, polling, max-temp guard, scheduling
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

| File | Lines | Responsibility |
|---|---|---|
| `state.py` | ~310 | `DEFAULT_STATE`/`DEVICE_DEFAULTS`/`SCHEDULE_DEFAULTS`, the shared `_state` dict, atomic JSON persistence + backup rotation, the JSONL log file, rate/watt calculation helpers |
| `models.py` | ~40 | Pydantic request models (`DeviceConfig`, `CommandPayload`, `ScheduleConfig`) |
| `auth.py` | ~380 | Password hashing, bearer tokens, role checks, login rate limiting, the default admin bootstrap, the recovery key, `auth_middleware`, and the full `/auth/*` router |
| `worker.py` | ~830 | The 24/7 background loop: device polling, the max-temp guard (with hysteresis + guard hours), schedule firing (including missed-schedule recovery), the watchdog, health/usage tracking, nightly dongle reboot |
| `routers/devices_crud.py` | ~85 | Device list/add/update/delete/reorder |
| `routers/devices_control.py` | ~130 | On-demand poll, commands, temp lock, display toggle, beeper |
| `routers/devices_discovery.py` | ~155 | OTA firmware upload, network discovery scan, health history |
| `routers/schedules.py` | ~90 | Schedule CRUD + conflict detection |
| `routers/settings.py` | ~80 | Settings, exchange rate, backup/restore |
| `routers/usage.py` | ~130 | Usage reports, CSV export, logs |
| `routers/system.py` | ~200 | Health checks (incl. Uptime Kuma push target), vacation mode, Watchtower webhook, root status, usage reset |
| `api.py` | ~90 | Pure app assembly |

47 endpoints total across `auth.py` (10) + the seven router files (37).

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
   `state.py` between tests — `auth`, `worker`, and every `routers.*`
   submodule. Miss one and a test can pass in isolation but fail
   depending on what ran before it in the same pytest session, because
   the missed module keeps holding a stale `_state` reference from an
   earlier test's `state.py` instance. This bug class was hit three
   separate times (once per new module added to the reload list) and
   is now explicitly documented in the fixture's docstring so it isn't
   rediscovered a fourth time.

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
3. Run `pyflakes` on every touched file — catches unused imports,
   undefined names, and duplicate definitions immediately.
4. Verify the endpoint count before and after matches exactly (a
   simple `grep -c '@router\.'` sum across all files).
5. Run the full pytest suite, then run it again 3-4 times with
   `pytest-randomly` using different seeds — isolation bugs are
   nondeterministic and only show up depending on test order.
6. Diff every changed file between the working copy and wherever it's
   being delivered (outputs directory, repo, etc.) to catch partial
   copies.
7. As a final check, populate a *completely fresh* directory from only
   the delivered files (not the working copy) and run the test suite
   there — this is the only check that actually proves what you're
   about to ship works, independent of local state.

## Testing

See [`TESTING.md`](./TESTING.md) for how to run the suite locally and
what each test file covers. Briefly: 90 tests across state persistence,
auth (hashing/tokens/rate-limiting), the max-temp guard (including
regression tests for the hysteresis and guard-hours bugs hit during
development), schedule command building and conflict detection, full
HTTP integration tests via FastAPI's `TestClient`, and a backup/restore
round-trip.
