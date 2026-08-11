"""Tests for worker.py's device network communication:
_fetch_state, _fetch_sensors, _fetch_exchange_rate, _send_cmd, _send_switch.

These mock httpx.AsyncClient.get/post directly (rather than using the
generic mock_device_response fixture) since each test needs a specific
response shape, status code, or failure sequence to exercise a
particular branch — the exact thing mock_device_response's docstring
says to do instead of trying to force it into one shape.
"""

import httpx
import pytest


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code

    def json(self):
        return self._json


# ── _fetch_state ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_state_returns_json_on_200(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse({"mode": "COOL", "current_temperature": "25"})
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    result = await worker_module._fetch_state("ac1.local")
    assert result == {"mode": "COOL", "current_temperature": "25"}


@pytest.mark.asyncio
async def test_fetch_state_falls_through_404_to_next_path(worker_module, mocker):
    calls = []
    async def fake_get(self, url, *a, **kw):
        calls.append(url)
        if "Air%20Conditioner" in url:
            return _FakeResponse(status_code=404)
        return _FakeResponse({"mode": "OFF"})
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    result = await worker_module._fetch_state("ac1.local")
    assert result == {"mode": "OFF"}
    assert len(calls) == 2  # tried both CLIMATE_PATHS


@pytest.mark.asyncio
async def test_fetch_state_returns_none_when_all_paths_fail(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        raise httpx.ConnectError("connection refused")
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)
    mocker.patch("asyncio.sleep", return_value=None)  # skip real backoff delays

    result = await worker_module._fetch_state("offline.local")
    assert result is None


# ── _fetch_sensors ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_sensors_collects_available_sensors(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        if "Outdoor" in url or "outdoor" in url:
            return _FakeResponse({"value": 31.5})
        if "Wi-Fi" in url or "wifi" in url:
            return _FakeResponse({"value": -55})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    result = await worker_module._fetch_sensors("ac1.local")
    assert result["outdoor_temp"]["value"] == 31.5
    assert result["wifi_signal"]["value"] == -55
    assert "uptime_days" not in result  # all its paths 404'd


@pytest.mark.asyncio
async def test_fetch_sensors_returns_empty_dict_when_nothing_available(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    result = await worker_module._fetch_sensors("ac1.local")
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_sensors_survives_exceptions_per_sensor(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        if "Outdoor" in url:
            raise httpx.ConnectError("boom")
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    result = await worker_module._fetch_sensors("ac1.local")
    assert "outdoor_temp" not in result  # errored, but didn't raise


# ── _fetch_exchange_rate ─────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_exchange_rate_success_updates_settings(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse({"rates": {"CRC": 512.345}, "date": "2026-08-10"})
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    result = await worker_module._fetch_exchange_rate()
    assert result == {"rate": 512, "date": "2026-08-10"}
    assert worker_module._state["settings"]["exchangeRate"] == 512
    assert worker_module._state["settings"]["_rate_date"] == "2026-08-10"


@pytest.mark.asyncio
async def test_fetch_exchange_rate_returns_none_on_non_200(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(status_code=500)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    result = await worker_module._fetch_exchange_rate()
    assert result is None


@pytest.mark.asyncio
async def test_fetch_exchange_rate_returns_none_on_exception(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        raise httpx.ConnectError("network down")
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    result = await worker_module._fetch_exchange_rate()
    assert result is None


# ── _send_cmd ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_cmd_success_on_first_path(worker_module, mocker):
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    ok = await worker_module._send_cmd("ac1.local", {"mode": "COOL"})
    assert ok is True


@pytest.mark.asyncio
async def test_send_cmd_falls_through_404_to_next_path(worker_module, mocker):
    calls = []
    async def fake_post(self, url, *a, **kw):
        calls.append(url)
        if "Air%20Conditioner" in url:
            return _FakeResponse(status_code=404)
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    ok = await worker_module._send_cmd("ac1.local", {"mode": "COOL"})
    assert ok is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_send_cmd_returns_false_when_all_paths_fail(worker_module, mocker):
    async def fake_post(self, url, *a, **kw):
        raise httpx.ConnectError("refused")
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    ok = await worker_module._send_cmd("ac1.local", {"mode": "COOL"})
    assert ok is False


@pytest.mark.asyncio
async def test_send_cmd_updates_device_last_mode_on_success(worker_module, mocker):
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Test", "_last_mode": "OFF"})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._send_cmd("ac1.local", {"mode": "COOL"})
    device = worker_module._state["devices"][0]
    assert device["_last_mode"] == "COOL"


# ── _send_switch ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_switch_beeper_tries_both_path_variants(worker_module, mocker):
    calls = []
    async def fake_post(self, url, *a, **kw):
        calls.append(url)
        if "Air%20Conditioner" in url:
            return _FakeResponse(status_code=404)
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    ok = await worker_module._send_switch("ac1.local", "switch/air_conditioner_beeper/turn_on")
    assert ok is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_send_switch_beeper_returns_false_when_both_fail(worker_module, mocker):
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    ok = await worker_module._send_switch("ac1.local", "switch/air_conditioner_beeper/turn_off")
    assert ok is False


@pytest.mark.asyncio
async def test_send_switch_non_beeper_path_success(worker_module, mocker):
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    ok = await worker_module._send_switch("ac1.local", "button/some_other_button/press")
    assert ok is True


@pytest.mark.asyncio
async def test_send_switch_non_beeper_path_exception_returns_false(worker_module, mocker):
    async def fake_post(self, url, *a, **kw):
        raise httpx.ConnectError("refused")
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    ok = await worker_module._send_switch("ac1.local", "button/some_other_button/press")
    assert ok is False
