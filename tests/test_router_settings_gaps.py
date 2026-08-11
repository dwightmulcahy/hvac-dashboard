"""Tests filling remaining gaps in routers/settings.py: the live
exchange-rate fetch path, and the verbose-logging-toggled log line
(the basic GET/PUT settings and backup/restore round-trip are already
covered in test_endpoints.py and test_backup_restore.py)."""

import httpx


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code

    def json(self):
        return self._json


def test_exchange_rate_fetches_live_when_not_cached_today(client, auth_headers, mocker):
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse({"rates": {"CRC": 520.0}, "date": "2026-08-10"})
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    r = client.get("/exchange-rate", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["rate"] == 520


def test_exchange_rate_returns_cached_value_when_already_today(client, auth_headers, api_module):
    import datetime
    today = datetime.date.today().isoformat()
    api_module._state["settings"]["_rate_date"] = today
    api_module._state["settings"]["exchangeRate"] = 500

    r = client.get("/exchange-rate", headers=auth_headers)
    assert r.json()["rate"] == 500
    assert r.json()["date"] == today


def test_exchange_rate_falls_back_to_cached_value_on_fetch_failure(client, auth_headers, mocker):
    async def fake_get(self, url, *a, **kw):
        raise httpx.ConnectError("refused")
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    r = client.get("/exchange-rate", headers=auth_headers)
    assert r.status_code == 200  # doesn't fail the request, just uses fallback
    assert "rate" in r.json()


def test_update_settings_logs_when_verbose_logging_enabled(client, auth_headers, api_module):
    api_module._state["settings"]["verbose_logging"] = False
    client.put("/settings", headers=auth_headers, json={"verbose_logging": True})
    logs = [l["msg"] for l in api_module._state["logs"]]
    assert any("Verbose logging enabled" in m for m in logs)


def test_update_settings_logs_when_verbose_logging_disabled(client, auth_headers, api_module):
    api_module._state["settings"]["verbose_logging"] = True
    client.put("/settings", headers=auth_headers, json={"verbose_logging": False})
    logs = [l["msg"] for l in api_module._state["logs"]]
    assert any("Verbose logging disabled" in m for m in logs)


def test_update_settings_no_log_when_verbose_logging_unchanged(client, auth_headers, api_module):
    api_module._state["settings"]["verbose_logging"] = False
    client.put("/settings", headers=auth_headers, json={"poll_interval": 90})
    logs = [l["msg"] for l in api_module._state["logs"]]
    assert not any("Verbose logging" in m for m in logs)
