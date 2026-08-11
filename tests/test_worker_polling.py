"""Tests for worker.py's device polling pipeline:
_record_health_event, _poll_device, _record_temp_history,
_record_usage, _check_watchdog.

_poll_device is the biggest and most consequential function in the
whole app — it's what actually runs every poll_interval seconds,
24/7, and is where several real bugs in this project's history
actually lived (mode-change log duplication, beeper sync direction,
uptime-reset reboot detection). These tests exercise its major
branches directly rather than only through the thin /poll endpoint
wrapper.
"""

import httpx
import pytest


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code

    def json(self):
        return self._json


def _device(host="ac1.local", name="Test AC", **overrides):
    d = {
        "host": host, "name": name, "btu": 24000, "seer": 20,
        "_stale": False, "_consecutive_failures": 0, "_retry_queue": [],
        "_last_mode": None, "_last_poll_epoch": None,
        "lock_temp": False, "locked_target_temp": None,
    }
    d.update(overrides)
    return d


def _mock_state_and_sensors(mocker, state_json):
    """Mock so any climate-path GET returns state_json and any sensor
    GET 404s — used for tests where the exact sensor data doesn't
    matter, only what _poll_device does with the climate state."""
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse(state_json)
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)


# ── _record_health_event ────────────────────────────────────


def test_record_health_event_appends_and_creates_list(worker_module):
    device = {}
    worker_module._record_health_event(device, "online")
    assert len(device["_health_history"]) == 1
    assert device["_health_history"][0]["event"] == "online"
    assert "ts" in device["_health_history"][0]


def test_record_health_event_newest_first(worker_module):
    device = {}
    worker_module._record_health_event(device, "offline")
    worker_module._record_health_event(device, "online")
    assert device["_health_history"][0]["event"] == "online"
    assert device["_health_history"][1]["event"] == "offline"


def test_record_health_event_caps_at_50(worker_module):
    device = {}
    for i in range(60):
        worker_module._record_health_event(device, f"event{i}")
    assert len(device["_health_history"]) == 50


# ── _poll_device: offline path ──────────────────────────────


@pytest.mark.asyncio
async def test_poll_device_offline_marks_stale_and_logs_once(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        raise httpx.ConnectError("refused")
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)
    mocker.patch("asyncio.sleep", return_value=None)

    device = _device()
    worker_module._state["devices"].append(device)

    await worker_module._poll_device(device)

    assert device["_stale"] is True
    assert device["_consecutive_failures"] == 1
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("went offline" in m for m in logs)


@pytest.mark.asyncio
async def test_poll_device_offline_does_not_relog_on_repeated_failure(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        raise httpx.ConnectError("refused")
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)
    mocker.patch("asyncio.sleep", return_value=None)

    device = _device(_stale=True, _consecutive_failures=3)
    worker_module._state["devices"].append(device)
    worker_module._state["logs"].clear()

    await worker_module._poll_device(device)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert not any("went offline" in m for m in logs)


@pytest.mark.asyncio
async def test_poll_device_offline_caps_retry_queue_at_10(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        raise httpx.ConnectError("refused")
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)
    mocker.patch("asyncio.sleep", return_value=None)

    device = _device(_retry_queue=[{"mode": "COOL"}] * 15)
    worker_module._state["devices"].append(device)

    await worker_module._poll_device(device)

    assert len(device["_retry_queue"]) == 10


# ── _poll_device: recovery ───────────────────────────────────


@pytest.mark.asyncio
async def test_poll_device_recovery_logs_back_online(worker_module, mocker):
    _mock_state_and_sensors(mocker, {"mode": "OFF", "current_temperature": "25", "target_temperature": "24"})
    device = _device(_stale=True, _consecutive_failures=5)
    worker_module._state["devices"].append(device)

    await worker_module._poll_device(device)

    assert device["_stale"] is False
    assert device["_consecutive_failures"] == 0
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("back online" in m for m in logs)


# ── _poll_device: sensor merging ─────────────────────────────


@pytest.mark.asyncio
async def test_poll_device_merges_outdoor_temp(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse({"mode": "OFF", "current_temperature": "25", "target_temperature": "24"})
        if "Outdoor" in url or "outdoor" in url:
            return _FakeResponse({"value": 32.5})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    device = _device()
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)

    ds = worker_module._state["device_state"]["ac1.local"]
    assert ds["outdoor_temp"] == 32.5


@pytest.mark.asyncio
async def test_poll_device_detects_dongle_reboot_from_uptime_reset(worker_module, mocker):
    worker_module._state["device_state"]["ac1.local"] = {"uptime_days": 5.0}

    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse({"mode": "OFF", "current_temperature": "25", "target_temperature": "24"})
        if "Uptime" in url or "uptime" in url:
            return _FakeResponse({"value": 0.01})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    device = _device()
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("dongle rebooted" in m for m in logs)


@pytest.mark.asyncio
async def test_poll_device_beeper_sync_reads_from_device(worker_module, mocker):
    """Regression test for a real bug found while writing this test:
    _fetch_sensors never included a 'beeper' path candidate, so
    _poll_device's beeper-sync branch (if "beeper" in sensors) could
    never fire — the documented 'beeper sync' feature was dead code.
    Fixed by adding switch/...beeper paths to _fetch_sensors."""
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse({"mode": "OFF", "current_temperature": "25", "target_temperature": "24"})
        if "Beeper" in url or "beeper" in url:
            return _FakeResponse({"value": True})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    device = _device(beeper="OFF")
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)

    assert device["beeper"] == "ON"


@pytest.mark.asyncio
async def test_poll_device_actual_power_usage_used_when_nonzero(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse({"mode": "COOL", "current_temperature": "25", "target_temperature": "22"})
        if "Power" in url or "power" in url:
            return _FakeResponse({"value": 850})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    device = _device()
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)

    ds = worker_module._state["device_state"]["ac1.local"]
    assert ds["actual_power_watts"] == 850


@pytest.mark.asyncio
async def test_poll_device_actual_power_zero_becomes_none(worker_module, mocker):
    """Innovair units report power_usage=0 even when running — worker.py
    treats 0 as 'not really supported', falling back to estimation."""
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse({"mode": "COOL", "current_temperature": "25", "target_temperature": "22"})
        if "Power" in url or "power" in url:
            return _FakeResponse({"value": 0})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    device = _device()
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)

    ds = worker_module._state["device_state"]["ac1.local"]
    assert ds["actual_power_watts"] is None


# ── _poll_device: mode change logging ────────────────────────


@pytest.mark.asyncio
async def test_poll_device_skips_mode_log_on_first_poll_after_restart(worker_module, mocker):
    _mock_state_and_sensors(mocker, {"mode": "COOL", "current_temperature": "24", "target_temperature": "22"})
    device = _device(_last_mode=None)
    worker_module._state["devices"].append(device)

    await worker_module._poll_device(device)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert not any("turned on" in m for m in logs)


@pytest.mark.asyncio
async def test_poll_device_logs_mode_change_after_known_prior_state(worker_module, mocker):
    _mock_state_and_sensors(mocker, {"mode": "COOL", "current_temperature": "24", "target_temperature": "22"})
    device = _device(_last_mode="OFF")
    worker_module._state["devices"].append(device)

    await worker_module._poll_device(device)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("turned on (COOL)" in m for m in logs)


@pytest.mark.asyncio
async def test_poll_device_turned_off_log_includes_previous_mode(worker_module, mocker):
    _mock_state_and_sensors(mocker, {"mode": "OFF", "current_temperature": "24", "target_temperature": "22"})
    device = _device(_last_mode="COOL")
    worker_module._state["devices"].append(device)

    await worker_module._poll_device(device)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("turned off (was COOL)" in m for m in logs)


# ── _poll_device: override protection (temp lock) ────────────


@pytest.mark.asyncio
async def test_poll_device_lock_restores_overridden_temp(worker_module, mocker):
    calls = []
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse({"mode": "COOL", "current_temperature": "25", "target_temperature": "27"})
        return _FakeResponse(status_code=404)
    async def fake_post(self, url, *a, **kw):
        calls.append(url)
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    device = _device(lock_temp=True, locked_target_temp=22.0, _last_mode="COOL")
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("remote override detected" in m for m in logs)
    assert any("temp restored" in m for m in logs)
    assert len(calls) > 0


@pytest.mark.asyncio
async def test_poll_device_lock_does_not_fire_when_temp_matches(worker_module, mocker):
    _mock_state_and_sensors(mocker, {"mode": "COOL", "current_temperature": "25", "target_temperature": "22"})
    device = _device(lock_temp=True, locked_target_temp=22.0, _last_mode="COOL")
    worker_module._state["devices"].append(device)

    await worker_module._poll_device(device)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert not any("remote override" in m for m in logs)


# ── _poll_device: retry queue draining ───────────────────────


@pytest.mark.asyncio
async def test_poll_device_drains_one_retry_queue_entry_on_success(worker_module, mocker):
    sent = []
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse({"mode": "OFF", "current_temperature": "25", "target_temperature": "24"})
    async def fake_post(self, url, *a, **kw):
        sent.append(url)
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    device = _device(_retry_queue=[{"mode": "COOL"}, {"mode": "OFF"}])
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)

    assert len(device["_retry_queue"]) == 1
    assert device["_retry_queue"][0] == {"mode": "OFF"}


# ── _record_temp_history ─────────────────────────────────────


def test_record_temp_history_appends_new_entry(worker_module):
    worker_module._record_temp_history("ac1.local", {"current_temperature": 25.0, "outdoor_temp": 31.0})
    history = worker_module._state["usage"]["temp_history"]["ac1.local"]
    assert len(history) == 1
    assert history[0]["in"] == 25.0
    assert history[0]["out"] == 31.0


def test_record_temp_history_updates_same_hour_instead_of_duplicating(worker_module):
    worker_module._record_temp_history("ac1.local", {"current_temperature": 25.0, "outdoor_temp": 31.0})
    worker_module._record_temp_history("ac1.local", {"current_temperature": 26.0, "outdoor_temp": 32.0})
    history = worker_module._state["usage"]["temp_history"]["ac1.local"]
    assert len(history) == 1
    assert history[0]["in"] == 26.0


def test_record_temp_history_caps_at_48_entries(worker_module):
    for h in range(60):
        key = f"2026-01-{(h//24)+1:02d}T{h%24:02d}:00"
        worker_module._state["usage"].setdefault("temp_history", {}).setdefault("ac1.local", []).append(
            {"t": key, "in": 25.0, "out": 30.0}
        )
    worker_module._record_temp_history("ac1.local", {"current_temperature": 25.0, "outdoor_temp": 30.0})
    assert len(worker_module._state["usage"]["temp_history"]["ac1.local"]) <= 48


def test_record_temp_history_skips_when_indoor_missing(worker_module):
    worker_module._record_temp_history("ac1.local", {"outdoor_temp": 31.0})
    assert "ac1.local" not in worker_module._state["usage"].get("temp_history", {})


def test_record_temp_history_skips_unparseable_values(worker_module):
    worker_module._record_temp_history("ac1.local", {"current_temperature": "not-a-number"})
    assert "ac1.local" not in worker_module._state["usage"].get("temp_history", {})


# ── _record_usage ────────────────────────────────────────────


def test_record_usage_creates_device_and_daily_bucket(worker_module):
    device = {"host": "ac1.local", "name": "Living Room", "btu": 24000, "seer": 20}
    ds = {"mode": "COOL", "current_temperature": "25", "target_temperature": "22", "outdoor_temp": 32}
    worker_module._record_usage(device, ds, 5.0)

    assert "ac1.local" in worker_module._state["usage"]["devices"]
    today = worker_module._today()
    bucket = worker_module._state["usage"]["daily"][today]["ac1.local"]
    assert bucket["runtime_minutes"] == 5.0
    assert bucket["snapshots"] == 1
    assert bucket["est_kwh"] > 0


def test_record_usage_accumulates_across_calls(worker_module):
    device = {"host": "ac1.local", "name": "Living Room", "btu": 24000, "seer": 20}
    ds = {"mode": "COOL", "current_temperature": "25", "target_temperature": "22"}
    worker_module._record_usage(device, ds, 5.0)
    worker_module._record_usage(device, ds, 3.0)

    today = worker_module._today()
    bucket = worker_module._state["usage"]["daily"][today]["ac1.local"]
    assert bucket["runtime_minutes"] == 8.0
    assert bucket["snapshots"] == 2


def test_record_usage_ignores_unparseable_temps_for_averages(worker_module):
    device = {"host": "ac1.local", "name": "Living Room", "btu": 24000, "seer": 20}
    ds = {"mode": "COOL", "current_temperature": "not-a-number", "target_temperature": "22"}
    worker_module._record_usage(device, ds, 5.0)

    today = worker_module._today()
    bucket = worker_module._state["usage"]["daily"][today]["ac1.local"]
    assert bucket["avg_indoor"] == []


# ── _check_watchdog ───────────────────────────────────────────


def test_check_watchdog_noop_when_no_last_seen(worker_module):
    device = {"host": "ac1.local", "name": "Test", "watchdog_minutes": 5}
    worker_module._check_watchdog(device)
    assert device.get("_stale") is None


def test_check_watchdog_marks_stale_past_threshold(worker_module, monkeypatch):
    import datetime as real_datetime

    class FrozenDateTime(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 12, 10, 0)

    monkeypatch.setattr(worker_module.datetime, "datetime", FrozenDateTime)

    device = {
        "host": "ac1.local", "name": "Test", "watchdog_minutes": 5,
        "_last_seen": "2026-01-01T12:00:00",
        "_stale": False,
    }
    worker_module._state["device_state"]["ac1.local"] = {}
    worker_module._check_watchdog(device)

    assert device["_stale"] is True
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("no response for" in m for m in logs)


def test_check_watchdog_does_not_mark_stale_within_threshold(worker_module, monkeypatch):
    import datetime as real_datetime

    class FrozenDateTime(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 12, 2, 0)

    monkeypatch.setattr(worker_module.datetime, "datetime", FrozenDateTime)

    device = {
        "host": "ac1.local", "name": "Test", "watchdog_minutes": 5,
        "_last_seen": "2026-01-01T12:00:00",
        "_stale": False,
    }
    worker_module._check_watchdog(device)
    assert device["_stale"] is False
