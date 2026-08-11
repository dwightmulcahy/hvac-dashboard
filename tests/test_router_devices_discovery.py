"""Tests for routers/devices_discovery.py: OTA firmware upload,
network discovery scan, and device health history.

Discovery tests use tiny /30 subnets (2 usable hosts) instead of a
real /24 to keep the scan fast — the endpoint's behavior doesn't
depend on subnet size, just on how many addresses it has to probe.
"""

import httpx


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code

    def json(self):
        return self._json


# ── OTA upload ────────────────────────────────────────────────


def test_ota_upload_success(client, auth_headers, mocker):
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    files = {"firmware": ("firmware.bin", b"fake binary content", "application/octet-stream")}
    r = client.post("/devices/ac1.local/ota-upload", headers=auth_headers, files=files)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_ota_upload_device_error_status(client, auth_headers, mocker):
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=500)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    files = {"firmware": ("firmware.bin", b"fake binary content", "application/octet-stream")}
    r = client.post("/devices/ac1.local/ota-upload", headers=auth_headers, files=files)
    assert r.status_code == 500
    assert r.json()["ok"] is False


def test_ota_upload_network_exception(client, auth_headers, mocker):
    async def fake_post(self, url, *a, **kw):
        raise httpx.ConnectError("refused")
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    files = {"firmware": ("firmware.bin", b"fake binary content", "application/octet-stream")}
    r = client.post("/devices/ac1.local/ota-upload", headers=auth_headers, files=files)
    assert r.status_code == 500
    assert r.json()["ok"] is False


def test_ota_upload_logs_device_name_when_known(client, auth_headers, mocker, api_module):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    files = {"firmware": ("firmware.bin", b"data", "application/octet-stream")}
    client.post("/devices/ac1.local/ota-upload", headers=auth_headers, files=files)
    logs = [l["msg"] for l in api_module._state["logs"]]
    assert any("Living Room" in m and "OTA" in m for m in logs)


# ── /discover ─────────────────────────────────────────────────


def test_discover_requires_subnet_when_no_devices_configured(client, auth_headers):
    r = client.get("/discover", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "subnet" in r.json()["error"]


def test_discover_rejects_invalid_subnet(client, auth_headers):
    r = client.get("/discover?subnet=not-a-subnet", headers=auth_headers)
    assert r.status_code == 400


def test_discover_finds_esphome_device(client, auth_headers, mocker):
    async def fake_get(self, url, *a, **kw):
        if url.endswith("/") and "climate" not in url and "sensor" not in url:
            return _FakeResponse(status_code=200)
        if "climate/air_conditioner" in url:
            return _FakeResponse({"mode": "COOL", "current_temperature": "24"})
        if "esphome_version" in url:
            return _FakeResponse({"value": "2026.7.0"})
        if "mac_address" in url:
            return _FakeResponse({"value": "AA:BB:CC:11:22:33"})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    r = client.get("/discover?subnet=192.168.99.0/30", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["scanned"] == 2  # /30 has 2 usable hosts
    assert len(data["found"]) == 2
    found = data["found"][0]
    assert found["firmware"] == "2026.7.0"
    assert found["mac_suffix"] == "112233"
    assert found["already_configured"] is False


def test_discover_marks_already_configured_devices(client, auth_headers, mocker, api_module):
    api_module._state["devices"].append({"host": "192.168.99.1", "name": "Existing"})

    async def fake_get(self, url, *a, **kw):
        if url.endswith("/") and "climate" not in url and "sensor" not in url:
            return _FakeResponse(status_code=200)
        if "climate/air_conditioner" in url:
            return _FakeResponse({"mode": "OFF"})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    r = client.get("/discover?subnet=192.168.99.0/30", headers=auth_headers)
    found = r.json()["found"]
    configured = next(f for f in found if f["ip"] == "192.168.99.1")
    assert configured["already_configured"] is True
    # "new" list should exclude the already-configured one
    new_ips = [f["ip"] for f in r.json()["new"]]
    assert "192.168.99.1" not in new_ips


def test_discover_skips_non_esphome_hosts(client, auth_headers, mocker):
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(status_code=404)  # nothing responds
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    r = client.get("/discover?subnet=192.168.99.0/30", headers=auth_headers)
    assert r.json()["found"] == []


def test_discover_infers_subnet_from_existing_ip_device(client, auth_headers, mocker, api_module):
    api_module._state["devices"].append({"host": "192.168.99.5", "name": "Existing"})

    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    # no ?subnet= given — should infer 192.168.99.0/24 from the existing device's IP
    r = client.get("/discover", headers=auth_headers)
    assert r.json()["ok"] is True
    assert r.json()["subnet"] == "192.168.99.0/24"


# ── /devices/{host}/health-history ───────────────────────────


def test_health_history_unknown_device_returns_empty(client, auth_headers):
    r = client.get("/devices/nonexistent.local/health-history", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["history"] == []


def test_health_history_returns_events_for_known_device(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room",
        "_health_history": [{"ts": "2026-01-01T00:00:00", "event": "online"}],
    })
    r = client.get("/devices/ac1.local/health-history", headers=auth_headers)
    assert r.json()["name"] == "Living Room"
    assert len(r.json()["history"]) == 1
    assert r.json()["history"][0]["event"] == "online"
