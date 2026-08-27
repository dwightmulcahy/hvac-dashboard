# Testing

Running the test suite for the backend split described in
[`ARCHITECTURE.md`](./ARCHITECTURE.md) and both frontends (`hvac-dashboard.html`,
`kiosk.html` — see [`KIOSK.md`](./KIOSK.md) for the latter's hardware
side). Back to [`README.md`](../README.md) for the project overview.

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
pytest --cov=api --cov=auth --cov=state --cov=models --cov=worker --cov=routers --cov=maintenance_logic --cov=notify --cov=logging_config --cov-report=term-missing
```

As of the last full pass: **99% overall** (1995 statements, 2 missing), with every file at 92%+ (`maintenance_logic.py`'s only gap is a defensive `except` branch for a malformed `last_done_at` timestamp). The remaining gaps are mostly `_background_worker`'s outer `while True` loop shell (its sub-functions — polling, scheduling, watchdog, maintenance checks — are all tested directly and thoroughly; the loop shell itself is inherently low-value to unit test) and scattered exception-handling edge cases in file I/O and network discovery.

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
| `tests/test_router_maintenance.py` | Maintenance CRUD, days-based and runtime_hours-based status computation, `/complete` (incl. append-only service history), operator-vs-admin role gating |
| `tests/test_worker_maintenance.py` | `_check_maintenance` overdue detection, once-per-transition notify pattern, generic notification-webhook forwarding (incl. failure tolerance) |
| `tests/test_cors.py` | CORS is closed by default (no `CORS_ALLOWED_ORIGINS`), opt-in via env var, rejects unconfigured origins, supports multiple comma-separated origins |
| `tests/test_backup_restore.py` | Backup export shape (no internal fields, no users), restore merge strategies (devices merge, schedules replace, settings merge), full round-trip |
| `tests/test_api_lifespan.py` | Startup log-loading from disk, CORS middleware registration |

## How isolation works

Every test gets its own throwaway `DATA_FILE`/`LOG_FILE` (via `tmp_path`)
and a freshly-imported set of modules (`api`, `auth`, `worker`, `state`,
`models`, `maintenance_logic`, `notify`, and every `routers.*`
submodule), so tests never touch your real `/data/hvac_state.json` or
make real network calls to physical AC dongles. Device network calls
are mocked per-test with `mocker.patch.object(httpx.AsyncClient, ...)`
— see `tests/test_worker_network.py` for the pattern, or use the
`mock_device_response` fixture in `conftest.py` for tests that don't
care about the exact device response shape.

**Every module that imports `_state` from `state.py` must be in the
reload list** in `conftest.py`'s `api_module` fixture (and its
duplicate in `test_api_lifespan.py`'s `_fresh_import_modules()`).
Missing one causes a specific, nasty bug: a test can pass in isolation
but fail depending on what ran before it in the same pytest session,
because the un-reloaded module keeps referencing a stale `_state` dict
from an earlier test. This has happened for real, more than once,
while building out this suite — if you add a new module that imports
`_state` (a new router, a new worker submodule), add it to both lists.

## Adding a regression test for a bug

This project has hit several subtle bugs during development (duplicate
log entries, timezone mismatches, schedule timing drift, max-temp
hysteresis not firing, etc.). When you fix one, add a test that would
have caught it — `test_max_temp.py` has good examples of this pattern
(each test's docstring explains which real bug it guards against).

---

## Linting and formatting

Python is linted with [ruff](https://docs.astral.sh/ruff/) (config:
`pyproject.toml`), enforced in CI via `ruff check .` — this replaced a
bare `pyflakes` call that only caught unused imports/variables; ruff's
selected rule set (`F`, `E`, `W`, `I`, `UP`, `B` — see the config file's
own comment for why this specific selection and not ruff's full
default) catches meaningfully more without the noise a fresh-project
default ruleset would produce on a mature, pre-existing codebase.

```bash
ruff check .          # what CI runs
ruff check . --fix    # auto-fix what's safely fixable
```

`ruff format` is configured (same file) but **not** run in CI or
enforced anywhere — reformatting ~8,000 lines of already-working,
already-tested code in one pass would produce an enormous diff with no
correctness benefit, and `ruff format`'s `target-version = "py314"`
has a confirmed, currently-open upstream bug that corrupts multi-
exception `except (A, B):` clauses (this codebase has one, in
`maintenance_logic.py`) — see `pyproject.toml`'s own comment for the
specific issue numbers. `target-version` is pinned to `"py313"`
specifically to avoid it. It's available to run by hand:

```bash
ruff format --check .   # see what would change, without changing it
ruff format .           # apply it — fine for a file you're already
                         # substantially rewriting for another reason
```

JS tooling is `eslint` + `prettier`, both enforced in CI, deliberately
scoped to `tests-js/*.js` only — not `frontend/*.html`. Those two files
ship embedded JS with no build step by design (see `package.json`'s
own description for the full reasoning); running a formatter across
~4,000 lines of already-shipped production markup in one pass would
produce exactly the same "enormous low-value diff" problem `ruff
format` has on the Python side, for the same reason.

```bash
npm run lint:js           # eslint (what CI runs)
npm run lint:js:fix       # auto-fix what's safely fixable
npm run format:js:check   # prettier (what CI runs)
npm run format:js         # apply formatting
```

---

## JavaScript tests

`hvac-dashboard.html` and `kiosk.html` are both single-file frontends
with no build step. Their tests use Node's **built-in** test runner
(`node:test`), which needs zero installation on any machine that
already has Node — but the jsdom-based suites (`kiosk.test.js`,
`dashboard-maintenance.test.js`) specifically also need `jsdom` (see
below), so a one-time `npm install` is required before running any of
the three files.

```bash
npm install                      # one-time, installs jsdom
node --test tests-js/*.test.js   # runs all three test files
```

There are two genuinely different testing approaches here, because the
two files need different things verified:

### `hvac-dashboard.html` — pure function extraction, no DOM needed

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
that touch `document`, `fetch`, or `localStorage`/`sessionStorage`
directly (rendering, event handlers, API calls) aren't unit tested
this way; those are covered by the syntax check step and by manual
testing against a running instance. Currently 77 tests.

#### Adding a new function to test

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

### `kiosk.html` — a real DOM, because most of it isn't pure functions

`kiosk.html`'s PIN lock screen, screen navigation, and rendered output
are inherently DOM-driven — sentinel extraction can't meaningfully
test "does tapping 4821 unlock the panel" or "does a locked device
gray out its +/- buttons," since there's no pure function to pull out.
(For what this file is and how it's deployed on actual hardware, see
[`KIOSK.md`](./KIOSK.md) — this section is only about testing it.)

`tests-js/kiosk.test.js` instead loads the real file into an actual
DOM implementation (`jsdom`, hence the one dependency this project
otherwise doesn't have) with a mocked `fetch`, then drives it exactly
the way a finger on the touchscreen would: dispatching real click
events on the PIN keypad, grid tiles, and buttons, and asserting on
what actually rendered. One long, sequential `t.test()` tree (56
subtests currently) — later subtests build on state left by earlier
ones (already unlocked, a detail view already open) rather than each
one re-doing the full unlock-and-navigate setup from scratch, which
would make an already multi-second test suite meaningfully slower for
no real benefit. This includes end-to-end coverage of the maintenance
view — overdue badge coloring, the complete-item flow, and viewer-role
gating (no "Done" button rendered for viewer-role users, matching the
backend's `/maintenance/{id}/complete` role check).

One thing worth knowing if you're extending this file: `kiosk.html`
runs several `setInterval` timers forever by design (the on-screen
clock, background polling) — correct behavior for a real kiosk left
open permanently, but without an explicit `window.close()` at the end
of the test, those timers keep the bare `jsdom` window alive
indefinitely and `node --test` never naturally exits. This already
happened once while building this suite — CI would have hung on every
single run until GitHub Actions' own job timeout eventually killed it,
silently burning CI minutes with no obvious cause in the output, since
every individual subtest genuinely passed the whole time.

#### Adding a new assertion to `kiosk.test.js`

Add a new `await t.test("description", () => { assert...(...); });`
block at the appropriate point in the existing flow — right after
whatever tap/interaction produces the state you're checking. If it
needs genuinely fresh setup (a new PIN, a new mock device), either
extend `MOCK_DEVICES`/`MOCK_SCHEDULES` at the top of the file, or add
a new PIN→role mapping in the mocked `/api/auth/login-pin` handler,
matching the existing `4821`/`1357` pattern.

### `dashboard-maintenance.test.js` — same jsdom pattern, different form

`hvac-dashboard.html`'s Settings → Maintenance tab has the same
problem `kiosk.html` has: the add/edit form, the role-gated buttons
(admin gets +Add/edit/delete, operator gets only Done, viewer gets
neither), and the re-rendered table after a save are all DOM-driven,
not pure functions. `tests-js/dashboard-maintenance.test.js` follows
`kiosk.test.js`'s exact approach — real `jsdom`, mocked `fetch`, real
click events — but structured differently: rather than one long shared
flow, each role (admin/operator/viewer) gets its own fresh `boot(role)`
call and its own top-level `test()`, since the admin flow mutates
maintenance state (add/edit/delete) in ways that would leak into a
separately-scoped role-gating check if they shared one window the way
`kiosk.test.js`'s single flow does.

It deliberately does not re-test the login modal — `checkAuth()` is
mocked to return an already-authenticated session for whichever role
the test needs, the same way `kiosk.test.js` mocks `/api/auth/login-pin`
rather than testing PIN hashing. Login itself has its own coverage in
`test_auth.py`/`test_auth_gaps.py` and isn't this file's job to repeat.

One real bug caught while building this suite, worth knowing before
writing similar assertions elsewhere: checking `innerHTML` for a
handler function's *name* (e.g. `assert.match(bodyHtml, /deleteMaintenanceItem/)`)
doesn't work here — this file's buttons attach handlers as JS
properties (`btn.onclick=function(){...}`), not inline `onclick="..."`
HTML attributes, so the function name never appears in the serialized
markup at all. A `doesNotMatch` assertion against that same
never-present string will trivially pass regardless of whether the
button is actually there or not — a false-negative-proof test that
looks like coverage but checks nothing. `title` attributes (`"Edit"`,
`"Delete"`) and button `textContent` are what's actually queryable;
assert against those instead.

---

## CI

Three GitHub Actions workflows:

- **`tests.yml`** — runs on every push/PR to `main`, `develop`, and
  `release`: the full pytest suite, `ruff check` (Python lint), both
  dashboard and kiosk JS syntax checks, `npm ci`, `eslint`/`prettier
  --check` on `tests-js/`, and the full JS unit test suite.
- **`ci.yml`** — runs on push to `main`/`develop` and PRs to
  `main`/`release`: builds the real Docker image, scans it with Trivy,
  **actually boots the container and curls `/health`, `/`, and `/api/`**
  — this is what would catch a startup failure like a missing file in
  the Dockerfile's `COPY` line or a broken import chain, since those
  fail silently in a filesystem-only scan — lints all Python files
  with `ruff check`, and validates `nginx.conf`.
- **`docker-release.yml`** — builds and pushes multi-arch images to
  Docker Hub, triggered only on version tags. Scans with Trivy and
  gates the push on CRITICAL/HIGH findings, but — unlike `ci.yml` —
  does not boot the container, since its build is scan-only until the
  final push step.
