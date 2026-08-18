"""Tests for api.py's CORS configuration.

The middleware is only added when CORS_ALLOWED_ORIGINS is set — the
packaged deployment (nginx.conf proxies both / and /api/ from the same
origin) never needs it, so the default is closed rather than the
previous allow_origins=["*"]. See api.py's comment above the
middleware registration for the full rationale.

Uses the same fresh-import pattern as test_api_lifespan.py rather than
the standard api_module fixture, since the env var must be set before
api.py is imported (module-level side effect), not after.
"""

from test_api_lifespan import _fresh_import_modules, _clear_modules


def test_cors_disabled_by_default(temp_data_file):
    router_modules, core_modules = _fresh_import_modules()
    import api as api_mod
    from fastapi.testclient import TestClient

    try:
        with TestClient(api_mod.app) as client:
            r = client.get("/", headers={"Origin": "https://evil.example.com"})
            assert "access-control-allow-origin" not in {k.lower() for k in r.headers.keys()}
    finally:
        _clear_modules(router_modules, core_modules)


def test_cors_enabled_for_configured_origin(temp_data_file, monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://dashboard.example.com")
    router_modules, core_modules = _fresh_import_modules()
    import api as api_mod
    from fastapi.testclient import TestClient

    try:
        with TestClient(api_mod.app) as client:
            r = client.get("/", headers={"Origin": "https://dashboard.example.com"})
            assert r.headers.get("access-control-allow-origin") == "https://dashboard.example.com"
    finally:
        _clear_modules(router_modules, core_modules)


def test_cors_rejects_unconfigured_origin(temp_data_file, monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://dashboard.example.com")
    router_modules, core_modules = _fresh_import_modules()
    import api as api_mod
    from fastapi.testclient import TestClient

    try:
        with TestClient(api_mod.app) as client:
            r = client.get("/", headers={"Origin": "https://evil.example.com"})
            assert "access-control-allow-origin" not in {k.lower() for k in r.headers.keys()}
    finally:
        _clear_modules(router_modules, core_modules)


def test_cors_allows_multiple_comma_separated_origins(temp_data_file, monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.example.com, https://b.example.com")
    router_modules, core_modules = _fresh_import_modules()
    import api as api_mod
    from fastapi.testclient import TestClient

    try:
        with TestClient(api_mod.app) as client:
            r = client.get("/", headers={"Origin": "https://b.example.com"})
            assert r.headers.get("access-control-allow-origin") == "https://b.example.com"
    finally:
        _clear_modules(router_modules, core_modules)
