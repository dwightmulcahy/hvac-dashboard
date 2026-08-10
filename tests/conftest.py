"""
Shared pytest fixtures for the HVAC dashboard test suite.

Key design constraint: api.py (and state.py) load DATA_FILE and create
_state at *import time* as a module-level side effect. To get real
isolation between tests we:

  1. Point DATA_FILE / LOG_FILE at a fresh temp path before import.
  2. Import the module fresh for every test via importlib.reload,
     so no state leaks between tests even though _state is a
     module-level global.

This means tests never touch your real /data/hvac_state.json on disk,
and never make real HTTP calls to physical AC dongles — all device
network calls go through a mocked httpx client (see mock_httpx below).
"""

import importlib
import sys
import pytest


@pytest.fixture
def temp_data_file(tmp_path, monkeypatch):
    """Point DATA_FILE/LOG_FILE at throwaway paths for this test only."""
    data_file = tmp_path / "hvac_state.json"
    log_file = tmp_path / "hvac_state_log.jsonl"
    monkeypatch.setenv("DATA_FILE", str(data_file))
    monkeypatch.setenv("LOG_FILE", str(log_file))
    return data_file


@pytest.fixture
def state_module(temp_data_file):
    """Fresh, isolated import of state.py with an empty _state.

    Use this for unit tests that only need state.py's helpers
    (rate calc, watt estimate, log persistence) without pulling in
    the whole FastAPI app.
    """
    sys.path.insert(0, ".")
    if "state" in sys.modules:
        del sys.modules["state"]
    import state as state_mod
    yield state_mod
    if "state" in sys.modules:
        del sys.modules["state"]


@pytest.fixture
def auth_module(temp_data_file):
    """Fresh, isolated import of auth.py alone (plus the state.py it
    depends on). Use this for unit tests of password hashing, tokens,
    role checks, and login rate limiting that don't need the full
    FastAPI app running.
    """
    sys.path.insert(0, ".")
    for mod in ("auth", "state"):
        if mod in sys.modules:
            del sys.modules[mod]
    import auth as auth_mod
    yield auth_mod
    for mod in ("auth", "state"):
        if mod in sys.modules:
            del sys.modules[mod]


@pytest.fixture
def worker_module(temp_data_file):
    """Fresh, isolated import of worker.py alone (plus the state.py it
    depends on). Use this for unit tests of schedule-command building,
    the max-temp guard, and other worker internals that don't need the
    full FastAPI app.
    """
    sys.path.insert(0, ".")
    for mod in ("worker", "state"):
        if mod in sys.modules:
            del sys.modules[mod]
    import worker as worker_mod
    yield worker_mod
    for mod in ("worker", "state"):
        if mod in sys.modules:
            del sys.modules[mod]


@pytest.fixture
def schedules_router_module(temp_data_file):
    """Fresh, isolated import of routers/schedules.py alone (plus the
    state.py it depends on). Use this for unit tests of
    _detect_schedule_conflicts that don't need the full FastAPI app.
    """
    sys.path.insert(0, ".")
    for mod in ("routers.schedules", "routers", "state"):
        if mod in sys.modules:
            del sys.modules[mod]
    from routers import schedules as schedules_mod
    yield schedules_mod
    for mod in ("routers.schedules", "routers", "state"):
        if mod in sys.modules:
            del sys.modules[mod]


@pytest.fixture
def api_module(temp_data_file, monkeypatch):
    """Fresh, isolated import of api.py — the full FastAPI app.

    Every test using this fixture gets its own empty _state and its
    own throwaway JSON/log files, so tests can freely add devices,
    schedules, users, etc. without affecting other tests or your
    real deployment.

    Critically, every module api.py transitively imports that itself
    imports _state from state.py must be cleared here too — auth,
    worker, and every router submodule. If any were left cached from
    a previous test, it would keep referencing a stale state._state
    dict from that earlier test's state.py instance — not the
    freshly-reloaded one this test's api.py is actually using. That
    mismatch is exactly the kind of bug this reload list exists to
    prevent (and the kind we've hit for real, more than once, during
    this project's development — most recently with the router split,
    where a test would pass in isolation but fail when run after
    another test in the same session).
    """
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
    yield api_mod
    for mod in router_modules + core_modules:
        if mod in sys.modules:
            del sys.modules[mod]


@pytest.fixture
def client(api_module):
    """FastAPI TestClient wired to an isolated api_module instance."""
    from fastapi.testclient import TestClient
    with TestClient(api_module.app) as c:
        yield c


@pytest.fixture
def admin_token(api_module, client):
    """Log in as the default admin/admin user and return a bearer token.

    api_module._ensure_default_admin() runs during the app's lifespan
    startup, so by the time the TestClient context has entered,
    admin/admin already exists and must_change_password is True —
    good enough for tests that just need *a* valid token.
    """
    r = client.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def mock_device_response(mocker):
    """Patch httpx.AsyncClient so any call "to a device" returns a
    canned climate-state JSON instead of hitting the real network.

    Tests that need specific device behavior (offline, custom temp,
    etc.) should build their own mock rather than using this one —
    it's meant as a sane default for tests that don't care about the
    exact device response shape.
    """
    import httpx

    class _FakeResponse:
        def __init__(self, json_data, status_code=200):
            self._json = json_data
            self.status_code = status_code

        def json(self):
            return self._json

    async def _fake_get(self, url, *args, **kwargs):
        return _FakeResponse({
            "mode": "OFF",
            "current_temperature": "25.0",
            "target_temperature": "24.0",
            "fan_mode": "AUTO",
        })

    async def _fake_post(self, url, *args, **kwargs):
        return _FakeResponse({"value": True, "state": "ON"})

    mocker.patch.object(httpx.AsyncClient, "get", _fake_get)
    mocker.patch.object(httpx.AsyncClient, "post", _fake_post)
