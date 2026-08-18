"""
HVAC Dashboard — Server-Side Automation API

App assembly only: creates the FastAPI app, wires up the auth
middleware, starts the background worker on startup, and includes
every feature router. All actual endpoint logic lives in routers/,
all device/schedule automation lives in worker.py, all auth lives in
auth.py, and all shared state/persistence lives in state.py.

All automation runs 24/7 in the container regardless of browser state.
"""

import asyncio
import logging
import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hvac")

from state import _lock, _state, _load_log_file, _save_raw, _add_log
from auth import router as auth_router, auth_middleware, generate_recovery_key, _ensure_default_admin
from worker import _background_worker

from routers import devices_crud, devices_control, devices_discovery
from routers import schedules, settings, usage, system, maintenance

# holds a strong reference to the background worker task so it isn't
# garbage-collected mid-run (asyncio only keeps a weak reference otherwise)
_worker_task = None


def _on_sigterm(*_):
    """Log a clean-shutdown message and persist state. Extracted as a
    standalone function (rather than an inline closure in lifespan)
    specifically so it can be unit tested directly — registering it
    via signal.signal() only works in the main thread, which test
    runners like FastAPI's TestClient don't use, so there's no
    reliable way to actually trigger it end-to-end in tests."""
    _add_log("HVAC API stopping (SIGTERM)", "warn")
    _save_raw(_state)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # load persisted logs from disk
    persisted = _load_log_file()
    if persisted:
        _state["logs"] = persisted
        log.info(f"Loaded {len(persisted)} log entries from disk")

    # ensure default admin user exists
    _ensure_default_admin()

    # generate a one-time recovery key and print to logs
    # use this if you forget your password: POST /api/auth/recover
    recovery_key = generate_recovery_key()
    log.warning(f"=== RECOVERY KEY: {recovery_key} ===")
    log.warning("=== Use POST /api/auth/recover with this key to reset admin password ===")

    # register SIGTERM handler to log clean shutdown
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:
        # signal.signal only works in the main thread of the main
        # interpreter — this happens under test runners (FastAPI's
        # TestClient runs the app in a worker thread) or other
        # non-standard hosting contexts. Not fatal: we simply lose the
        # "graceful shutdown log line" in those environments, everything
        # else still works.
        log.info("Skipping SIGTERM handler registration — not running in main thread")

    global _worker_task
    _worker_task = asyncio.create_task(_background_worker())
    _add_log("HVAC API started", "info")
    yield
    # shutdown
    _add_log("HVAC API stopped", "warn")
    async with _lock:
        _save_raw(_state)


app = FastAPI(title="HVAC Automation API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.middleware("http")(auth_middleware)

app.include_router(auth_router)
app.include_router(devices_crud.router)
app.include_router(devices_control.router)
app.include_router(devices_discovery.router)
app.include_router(schedules.router)
app.include_router(settings.router)
app.include_router(usage.router)
app.include_router(system.router)
app.include_router(maintenance.router)
