"""Integration tests for AccessLogMiddleware and the LOG_FORMAT env var
end-to-end — a real request through the real app, asserting on what
actually got logged.

Uses the same fresh-import pattern as test_cors.py (and for the same
reason): LOG_FORMAT must be set before api.py is imported, since
configure_logging() runs as a module-level side effect at import time,
not per-request.
"""

import json
import logging

from test_api_lifespan import _clear_modules, _fresh_import_modules


def test_access_log_records_method_path_status_and_duration(temp_data_file, caplog):
    router_modules, core_modules = _fresh_import_modules()
    from fastapi.testclient import TestClient

    import api as api_mod

    try:
        with caplog.at_level(logging.INFO, logger="hvac.access"):
            with TestClient(api_mod.app) as client:
                client.get("/health")
        records = [r for r in caplog.records if r.name == "hvac.access"]
        assert len(records) == 1
        rec = records[0]
        assert rec.method == "GET"
        assert rec.path == "/health"
        assert rec.status_code == 200
        assert isinstance(rec.duration_ms, float)
        assert rec.duration_ms >= 0
    finally:
        _clear_modules(router_modules, core_modules)


def test_access_log_captures_requests_rejected_by_auth_middleware(temp_data_file, caplog):
    """The access log middleware is added after auth_middleware
    specifically so it wraps outermost and sees every response,
    including ones auth_middleware itself rejects — this is the
    regression test for that ordering."""
    router_modules, core_modules = _fresh_import_modules()
    from fastapi.testclient import TestClient

    import api as api_mod

    try:
        with TestClient(api_mod.app) as client:
            # create a real user so auth_middleware actually enforces
            # role checks instead of the "no users configured yet"
            # open-access bypass
            client.post("/auth/users", json={"username": "someone", "password": "longenough123", "role": "viewer"})
        with caplog.at_level(logging.INFO, logger="hvac.access"):
            # caplog.records accumulates for the whole test from the
            # moment the fixture attaches its handler — at_level only
            # adjusts the level filter, it doesn't scope *what's
            # already been captured* to this block. The setup request
            # above already logged at INFO (root logger's level is
            # already permissive by the time configure_logging() runs),
            # so without this clear() the assertion below would see
            # both that request and the one actually being tested.
            caplog.clear()
            with TestClient(api_mod.app) as client:
                r = client.get("/auth/users")  # no auth header — should be rejected
        records = [r for r in caplog.records if r.name == "hvac.access"]
        assert len(records) == 1
        assert records[0].status_code == r.status_code
        assert r.status_code in (401, 403)
    finally:
        _clear_modules(router_modules, core_modules)


def test_access_log_does_not_include_request_body(temp_data_file, caplog):
    """Deliberate: request bodies here can include password-change
    payloads. Logging them would be a credential-leak risk."""
    router_modules, core_modules = _fresh_import_modules()
    from fastapi.testclient import TestClient

    import api as api_mod

    try:
        with caplog.at_level(logging.INFO, logger="hvac.access"):
            with TestClient(api_mod.app) as client:
                client.post("/auth/login", json={"username": "admin", "password": "super-secret-value"})
        records = [r for r in caplog.records if r.name == "hvac.access"]
        assert len(records) == 1
        assert "super-secret-value" not in records[0].getMessage()
        assert not hasattr(records[0], "body")
    finally:
        _clear_modules(router_modules, core_modules)


def test_log_format_json_produces_parseable_json_lines_end_to_end(temp_data_file, monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "json")
    router_modules, core_modules = _fresh_import_modules()
    from fastapi.testclient import TestClient

    import api as api_mod

    try:
        with TestClient(api_mod.app) as client:
            client.get("/health")
        err = capsys.readouterr().err
        json_lines = [line for line in err.splitlines() if line.strip().startswith("{")]
        assert len(json_lines) > 0
        # every line that looks like JSON must actually parse as JSON —
        # a format string leaking through unconverted would fail this
        for line in json_lines:
            parsed = json.loads(line)
            assert "timestamp" in parsed
            assert "level" in parsed
    finally:
        _clear_modules(router_modules, core_modules)


def test_log_format_default_produces_plain_text_not_json(temp_data_file, monkeypatch, capsys):
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    router_modules, core_modules = _fresh_import_modules()
    from fastapi.testclient import TestClient

    import api as api_mod

    try:
        with TestClient(api_mod.app) as client:
            client.get("/health")
        err = capsys.readouterr().err
        assert "[INFO]" in err or "[WARNING]" in err
        json_lines = [line for line in err.splitlines() if line.strip().startswith("{")]
        assert json_lines == []
    finally:
        _clear_modules(router_modules, core_modules)
