"""Tests for the remaining exception-handling branches in
routers/devices_discovery.py's /discover endpoint: hostname (vs IP)
device handling during subnet inference and existing-host resolution,
per-sensor fetch failures during a probe, reverse-DNS hostname
resolution, and a probe that fails entirely.
"""

import socket

import httpx


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code

    def json(self):
        return self._json


def test_discover_skips_hostname_devices_when_inferring_subnet(client, auth_headers, api_module, mocker):
    """A device configured by hostname (not IP) should be skipped when
    inferring the subnet from existing devices — hits the
    ipaddress.ip_address() ValueError branch in the inference loop."""
    api_module._state["devices"].append({"host": "ac1.hostname.local", "name": "Hostname Device"})
    api_module._state["devices"].append({"host": "192.168.99.5", "name": "IP Device"})

    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)
    mocker.patch.object(socket, "gethostbyname", side_effect=socket.gaierror("no such host"))

    r = client.get("/discover", headers=auth_headers)
    assert r.json()["subnet"] == "192.168.99.0/24"


def test_discover_resolves_existing_hostname_to_ip_for_comparison(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.hostname.local", "name": "Hostname Device"})
    mocker.patch.object(socket, "gethostbyname", return_value="192.168.99.1")

    async def fake_get(self, url, *a, **kw):
        if url.endswith("/") and "climate" not in url and "sensor" not in url:
            return _FakeResponse(status_code=200)
        if "climate/air_conditioner" in url:
            return _FakeResponse({"mode": "OFF"})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    r = client.get("/discover?subnet=192.168.99.0/30", headers=auth_headers)
    found = r.json()["found"]
    resolved = next(f for f in found if f["ip"] == "192.168.99.1")
    assert resolved["already_configured"] is True


def test_discover_hostname_resolution_failure_is_tolerated(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.hostname.local", "name": "Hostname Device"})
    mocker.patch.object(socket, "gethostbyname", side_effect=socket.gaierror("no such host"))

    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    r = client.get("/discover?subnet=192.168.99.0/30", headers=auth_headers)
    assert r.status_code == 200


def test_discover_tolerates_esphome_version_fetch_exception(client, auth_headers, mocker):
    async def fake_get(self, url, *a, **kw):
        if url.endswith("/") and "climate" not in url and "sensor" not in url:
            return _FakeResponse(status_code=200)
        if "climate/air_conditioner" in url:
            return _FakeResponse({"mode": "OFF"})
        if "esphome_version" in url:
            raise httpx.ConnectError("refused")
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    r = client.get("/discover?subnet=192.168.99.0/30", headers=auth_headers)
    found = r.json()["found"]
    assert len(found) == 2
    assert found[0]["firmware"] is None


def test_discover_tolerates_mac_address_fetch_exception(client, auth_headers, mocker):
    async def fake_get(self, url, *a, **kw):
        if url.endswith("/") and "climate" not in url and "sensor" not in url:
            return _FakeResponse(status_code=200)
        if "climate/air_conditioner" in url:
            return _FakeResponse({"mode": "OFF"})
        if "mac_address" in url:
            raise httpx.ConnectError("refused")
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    r = client.get("/discover?subnet=192.168.99.0/30", headers=auth_headers)
    found = r.json()["found"]
    assert found[0]["mac_suffix"] is None


def test_discover_uses_reverse_dns_hostname_for_suggested_name(client, auth_headers, mocker):
    async def fake_get(self, url, *a, **kw):
        if url.endswith("/") and "climate" not in url and "sensor" not in url:
            return _FakeResponse(status_code=200)
        if "climate/air_conditioner" in url:
            return _FakeResponse({"mode": "OFF"})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)
    mocker.patch.object(socket, "gethostbyaddr", return_value=("living-room-ac.lan", [], []))

    r = client.get("/discover?subnet=192.168.99.0/30", headers=auth_headers)
    found = r.json()["found"]
    entry = found[0]
    assert entry["hostname"] == "living-room-ac.lan"
    assert entry["suggested_name"] == "Living Room Ac"


def test_discover_probe_exception_is_isolated_to_that_host(client, auth_headers, mocker):
    """If probing one IP raises unexpectedly, other IPs in the same
    batch should still be probed and reported normally."""
    async def fake_get(self, url, *a, **kw):
        if "192.168.99.1" in url:
            raise RuntimeError("totally unexpected failure")
        if url.endswith("/") and "climate" not in url and "sensor" not in url:
            return _FakeResponse(status_code=200)
        if "climate/air_conditioner" in url:
            return _FakeResponse({"mode": "OFF"})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    r = client.get("/discover?subnet=192.168.99.0/30", headers=auth_headers)
    assert r.status_code == 200
    found = r.json()["found"]
    ips = [f["ip"] for f in found]
    assert "192.168.99.1" not in ips
    assert "192.168.99.2" in ips
