"""Tests for routers/system.py: detailed health check, Uptime Kuma
push endpoint, vacation mode on/off, Watchtower webhook forwarding,
and usage reset.

Basic /  and empty-state /health cases are already covered in
test_endpoints.py — these fill in the device-present and error-path
branches that weren't exercised yet.
"""

import httpx


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def json(self):
        return {}


# ── /health with devices present ─────────────────────────────


def test_health_reports_stale_device(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room", "_stale": True,
        "_last_seen": "2026-01-01T00:00:00",
    })
    r = client.get("/health", headers=auth_headers)
    body = r.json()
    assert body["stale"] == 1
    assert body["online"] == 0
    assert body["status"] == "degraded"


def test_health_reports_healthy_device(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room", "_stale": False,
        "_last_seen": "2026-01-01T12:00:00",
    })
    r = client.get("/health", headers=auth_headers)
    body = r.json()
    assert body["online"] == 1
    assert body["stale"] == 0


def test_health_counts_active_schedules(client, auth_headers, api_module):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    api_module._state["schedules"].append({"id": "a", "enabled": True})
    api_module._state["schedules"].append({"id": "b", "enabled": False})
    r = client.get("/health", headers=auth_headers)
    assert r.json()["schedules_active"] == 1


def test_health_reports_vacation_mode(client, auth_headers, api_module):
    api_module._state["settings"]["vacation_mode"] = True
    r = client.get("/health", headers=auth_headers)
    assert r.json()["vacation_mode"] is True


# ── /health/push with devices present ────────────────────────


def test_health_push_returns_200_when_all_healthy(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room", "_stale": False,
    })
    r = client.get("/health/push", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_push_returns_503_when_any_stale(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room", "_stale": True,
    })
    r = client.get("/health/push", headers=auth_headers)
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert "Living Room" in r.json()["msg"]


# ── Vacation mode ─────────────────────────────────────────────


def test_vacation_on_sets_high_temp_and_pauses_schedules(client, auth_headers, api_module, mocker):
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "max_temp": 25})
    api_module._state["device_state"]["ac1.local"] = {"mode": "COOL"}
    api_module._state["schedules"].append({"id": "a", "enabled": True})
    api_module._state["settings"]["vacation_max_temp"] = 32

    r = client.post("/vacation/on", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["vacation_mode"] is True

    device = api_module._state["devices"][0]
    assert device["max_temp"] == 32
    assert device["_pre_vacation_max_temp"] == 25
    assert api_module._state["schedules"][0]["enabled"] is False
    assert api_module._state["schedules"][0]["_vacation_paused"] is True


def test_vacation_on_turns_off_running_unit(client, auth_headers, api_module, mocker):
    sent = []
    async def fake_post(self, url, *a, **kw):
        sent.append(url)
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "max_temp": 25})
    api_module._state["device_state"]["ac1.local"] = {"mode": "COOL"}

    client.post("/vacation/on", headers=auth_headers)
    assert len(sent) > 0  # a command was actually sent to turn it off
    assert api_module._state["device_state"]["ac1.local"]["mode"] == "OFF"


def test_vacation_off_restores_max_temp_and_schedules(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room",
        "max_temp": 32, "_pre_vacation_max_temp": 25, "_pre_vacation_mode": "COOL",
    })
    api_module._state["schedules"].append({"id": "a", "enabled": False, "_vacation_paused": True})

    r = client.post("/vacation/off", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["vacation_mode"] is False

    device = api_module._state["devices"][0]
    assert device["max_temp"] == 25
    assert "_pre_vacation_max_temp" not in device
    assert api_module._state["schedules"][0]["enabled"] is True


def test_get_vacation_status(client, auth_headers, api_module):
    api_module._state["settings"]["vacation_mode"] = True
    api_module._state["settings"]["vacation_max_temp"] = 30
    r = client.get("/vacation", headers=auth_headers)
    assert r.json()["vacation_mode"] is True
    assert r.json()["vacation_max_temp"] == 30


# ── Watchtower webhook ────────────────────────────────────────


def test_watchtower_notify_logs_without_forward_webhook(client, auth_headers, api_module):
    r = client.post("/watchtower-notify", headers=auth_headers, json={"container": "hvac-dashboard", "tag": "v1.2.0"})
    assert r.status_code == 200
    logs = [l["msg"] for l in api_module._state["logs"]]
    assert any("hvac-dashboard:v1.2.0" in m for m in logs)


def test_watchtower_notify_forwards_to_configured_webhook(client, auth_headers, api_module, mocker):
    api_module._state["settings"]["watchtower_webhook"] = "https://example.com/webhook"
    calls = []
    async def fake_post(self, url, *a, **kw):
        calls.append(url)
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/watchtower-notify", headers=auth_headers, json={"container": "hvac-dashboard", "tag": "latest"})
    assert r.status_code == 200
    assert len(calls) == 1
    assert calls[0] == "https://example.com/webhook"


def test_watchtower_notify_forward_failure_is_logged_not_raised(client, auth_headers, api_module, mocker):
    api_module._state["settings"]["watchtower_webhook"] = "https://example.com/webhook"
    async def fake_post(self, url, *a, **kw):
        raise httpx.ConnectError("refused")
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/watchtower-notify", headers=auth_headers, json={"container": "hvac-dashboard", "tag": "latest"})
    assert r.status_code == 200  # doesn't fail the request
    logs = [l["msg"] for l in api_module._state["logs"]]
    assert any("webhook forward failed" in m for m in logs)


# ── /reset ────────────────────────────────────────────────────


def test_reset_clears_usage_data(client, auth_headers, api_module):
    api_module._state["usage"]["daily"]["2026-01-01"] = {"ac1.local": {"runtime_minutes": 60}}
    r = client.delete("/reset", headers=auth_headers)
    assert r.status_code == 200
    assert api_module._state["usage"]["daily"] == {}
