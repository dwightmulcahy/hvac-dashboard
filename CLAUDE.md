# CLAUDE.md

Self-hosted dashboard + wall kiosk for Innovair mini-splits controlled via SMLIGHT SLWF-01pro ESPHome dongles (Midea serial protocol). FastAPI backend, runs 24/7 in Docker on a QNAP NAS. All automation runs server-side in `worker.py`.

Deeper docs (read before non-trivial changes):
- `docs/ARCHITECTURE.md` — module map, one-way dependency graph, refactor history
- `docs/TESTING.md` — test suites, coverage command
- `docs/KIOSK.md` — Raspberry Pi kiosk setup

## Commands

```bash
pip install -r requirements-dev.txt --break-system-packages
pytest                          # backend suite (asyncio_mode=auto)
ruff check .                    # lint gate in CI; do NOT run `ruff format` repo-wide
npm ci && npm run test:js       # JS suites (node --test tests-js/*.test.js)
npm run lint:js                 # eslint, scoped to tests-js/ only
```

## Layout

- Backend: `logging_config.py` → `state.py` → `auth.py` / `maintenance_logic.py` / `notify.py` → `worker.py` → `routers/*.py` → `api.py`. Imports only go down this chain; `worker.py` never imports from `routers/`. Routers may import from `worker.py`.
- Frontend: `frontend/hvac-dashboard.html`, `frontend/kiosk.html` — single-file, no build step, zero runtime deps. Don't run eslint/prettier on them.
- Firmware: `firmware/*.yaml` — ESPHome configs for the dongles.
- Tests: `tests/` (pytest), `tests-js/` (node test runner; `extract.js` pulls pure functions out of the HTML via sentinel comments).

## Rules / gotchas

- **New module importing `_state`** → add it to the reload list in `tests/conftest.py`'s `api_module` fixture AND the copy in `tests/test_api_lifespan.py`, or tests break order-dependently.
- **ruff target is py313, not py314** — py314 formatter corrupts `except (A, B):`. See `pyproject.toml`.
- **Device commands can silently not apply.** An HTTP 2xx from the dongle does not mean the unit changed state (e.g. temp sent right after a mode change). Any code path that sets `target_temperature` must go through `worker._verify_temp_command` (re-poll after 1.5s, retry once, else `_retry_queue`). Currently wired into `_check_schedules`, `_check_missed_schedules`, and `/cmd` in `routers/devices_control.py`. The dashboard's `adjustTemp()` has its own client-side equivalent.
- **Keep-temp** (`worker._check_keep_temp`, per-device `keep_temp`, COOL only): pauses at ≤ target−0.5°C, resumes at ≥ target+0.5°C after ≥5 min off. Any `_send_cmd` mode command cancels the pause unless called with `keep_temp=True` — so schedules/users/guard/vacation always win.
- `_verify_temp_command` sleeps inside the sequential worker loop; many devices firing in the same minute add ~1.5s each.

## Conventions

- Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:` …) — `git-cliff` builds release notes from them (`cliff.toml`).
- Commit directly to `main` and push to `origin/main`; no PRs. The owner handles merging to `release` and tagging.
- Every fix gets a regression test; keep the suite green before committing.
- Responses: terse, factual, compact diffs only, ≤3 bullets of reasoning, ask instead of speculating.
