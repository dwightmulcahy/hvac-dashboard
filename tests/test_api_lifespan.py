"""Tests for api.py's lifespan startup behavior: loading persisted
logs from disk, and the SIGTERM handler's log-and-save behavior.
"""

import json
import sys


def _fresh_import_modules():
    sys.path.insert(0, ".")
    router_modules = (
        "routers.devices_crud", "routers.devices_control", "routers.devices_discovery",
        "routers.schedules", "routers.settings", "routers.usage", "routers.system",
        "routers",
    )
    core_modules = ("api", "auth", "worker", "state", "models")
    for mod in router_modules + core_modules:
        if mod in sys.modules:
            del sys.modules[mod]
    return router_modules, core_modules


def _clear_modules(router_modules, core_modules):
    for mod in router_modules + core_modules:
        if mod in sys.modules:
            del sys.modules[mod]


def test_lifespan_loads_persisted_logs_from_disk(temp_data_file):
    import os
    log_file = str(temp_data_file).replace(".json", "_log.jsonl")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "w") as f:
        f.write(json.dumps({
            "time": "12:00:00", "iso": "2026-01-01T12:00:00",
            "msg": "pre-existing entry", "level": "info",
        }) + "\n")

    router_modules, core_modules = _fresh_import_modules()
    import api as api_mod
    from fastapi.testclient import TestClient

    with TestClient(api_mod.app):
        msgs = [l["msg"] for l in api_mod._state["logs"]]
        assert "pre-existing entry" in msgs

    _clear_modules(router_modules, core_modules)


def test_sigterm_handler_logs_and_saves_state(temp_data_file):
    """_on_sigterm is a standalone module-level function specifically
    so it can be tested directly like this — registering it via
    signal.signal() only works in the main thread, which test runners
    like FastAPI's TestClient don't use, so there's no reliable way to
    exercise it end-to-end through real signal delivery in tests."""
    router_modules, core_modules = _fresh_import_modules()
    import api as api_mod
    from fastapi.testclient import TestClient

    try:
        with TestClient(api_mod.app):
            api_mod._state["devices"].append({"host": "ac1.local", "name": "Test"})
            api_mod._on_sigterm()
            logs = [l["msg"] for l in api_mod._state["logs"]]
            assert any("stopping (SIGTERM)" in m for m in logs)
    finally:
        _clear_modules(router_modules, core_modules)
