"""Test for api.py's lifespan startup behavior: loading persisted
logs from disk. The SIGTERM handler body itself isn't covered here —
exercising it would mean sending a real OS signal to the test
process, which isn't a meaningful thing to unit test.
"""

import json


def test_lifespan_loads_persisted_logs_from_disk(temp_data_file, monkeypatch):
    import os
    log_file = str(temp_data_file).replace(".json", "_log.jsonl")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "w") as f:
        f.write(json.dumps({"time": "12:00:00", "iso": "2026-01-01T12:00:00", "msg": "pre-existing entry", "level": "info"}) + "\n")

    import sys
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

    import api as api_mod
    from fastapi.testclient import TestClient
    with TestClient(api_mod.app):
        msgs = [l["msg"] for l in api_mod._state["logs"]]
        assert "pre-existing entry" in msgs

    for mod in router_modules + core_modules:
        if mod in sys.modules:
            del sys.modules[mod]
