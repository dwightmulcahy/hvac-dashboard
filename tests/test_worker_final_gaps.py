"""Final coverage-gap tests for worker.py: scattered exception
branches and edge-condition paths across the network layer,
_poll_device's sensor merging, _record_usage, _check_watchdog,
_check_max_temp, _check_schedules, and _check_missed_schedules that
weren't hit by the main test suites in test_worker_network.py,
test_worker_polling.py, and test_worker_scheduling.py.
"""

import datetime as real_datetime

import httpx
import pytest


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code

    def json(self):
        return self._json


def _freeze(monkeypatch, worker_module, y, mo, d, h, mi, s=0):
    class FrozenDateTime(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(y, mo, d, h, mi, s)
    monkeypatch.setattr(worker_module.datetime, "datetime", FrozenDateTime)


def _device(host="ac1.local", name="Test AC", **overrides):
    d = {
        "host": host, "name": name, "btu": 24000, "seer": 20,
        "_stale": False, "_consecutive_failures": 0, "_retry_queue": [],
        "_last_mode": None, "_last_poll_epoch": None,
        "lock_temp": False, "locked_target_temp": None,
    }
    d.update(overrides)
    return d


def _sch(id_="a", host="ac1.local", time="07:00", days=None, **overrides):
    sch = {
        "id": id_, "device_host": host, "device_name": "Test",
        "time": time, "end_time": None, "days": days if days is not None else [0, 1, 2, 3, 4, 5, 6],
        "power": "on", "mode": "COOL", "temp": 24, "enabled": True,
        "last_run": None,
    }
    sch.update(overrides)
    return sch


# ── _fetch_sensors: wifi/esphome_version exception branches ─────


@pytest.mark.asyncio
async def test_fetch_sensors_wifi_fetch_exception_tolerated(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        if "Wi-Fi" in url or "wifi" in url:
            raise httpx.ConnectError("refused")
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    result = await worker_module._fetch_sensors("ac1.local")
    assert "wifi_signal" not in result


@pytest.mark.asyncio
async def test_fetch_sensors_esphome_version_fetch_exception_tolerated(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        if "esphome_version" in url:
            raise httpx.ConnectError("refused")
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    result = await worker_module._fetch_sensors("ac1.local")
    assert "esphome_version" not in result


# ── _send_switch: beeper path exception tolerated ────────────────


@pytest.mark.asyncio
async def test_send_switch_beeper_exception_on_one_path_falls_through(worker_module, mocker):
    calls = []
    async def fake_post(self, url, *a, **kw):
        calls.append(url)
        if len(calls) == 1:
            raise httpx.ConnectError("refused")
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    ok = await worker_module._send_switch("ac1.local", "switch/air_conditioner_beeper/turn_on")
    assert ok is True
    assert len(calls) == 2


# ── _poll_device: consecutive failure logging every 10th ────────


@pytest.mark.asyncio
async def test_poll_device_logs_every_10th_consecutive_failure(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        raise httpx.ConnectError("refused")
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)
    mocker.patch("asyncio.sleep", return_value=None)

    device = _device(_stale=True, _consecutive_failures=9)
    worker_module._state["devices"].append(device)

    await worker_module._poll_device(device)

    assert device["_consecutive_failures"] == 10
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("still unreachable (10 consecutive failures)" in m for m in logs)


# ── _poll_device: uptime/wifi/power parse exceptions ─────────────


@pytest.mark.asyncio
async def test_poll_device_tolerates_malformed_uptime(worker_module, mocker):
    worker_module._state["device_state"]["ac1.local"] = {"uptime_days": 5.0}
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse({"mode": "OFF", "current_temperature": "25", "target_temperature": "24"})
        if "Uptime" in url or "uptime" in url:
            return _FakeResponse({"value": "not-a-number"})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    device = _device()
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert not any("dongle rebooted" in m for m in logs)


@pytest.mark.asyncio
async def test_poll_device_wifi_signal_falls_back_to_raw_on_parse_failure(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse({"mode": "OFF", "current_temperature": "25", "target_temperature": "24"})
        if "Wi-Fi" in url or "wifi" in url:
            return _FakeResponse({"value": "unparseable-wifi-value"})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    device = _device()
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)

    ds = worker_module._state["device_state"]["ac1.local"]
    assert ds["wifi_signal"] == "unparseable-wifi-value"


@pytest.mark.asyncio
async def test_poll_device_firmware_version_stored_short_form(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse({"mode": "OFF", "current_temperature": "25", "target_temperature": "24"})
        if "esphome_version" in url:
            return _FakeResponse({"value": "2026.7.0 abc123def"})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    device = _device()
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)

    assert device["_firmware_version"] == "2026.7.0"


@pytest.mark.asyncio
async def test_poll_device_power_usage_parse_failure_sets_none(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse({"mode": "COOL", "current_temperature": "25", "target_temperature": "22"})
        if "Power" in url or "power" in url:
            return _FakeResponse({"value": "not-a-number"})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    device = _device()
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)

    ds = worker_module._state["device_state"]["ac1.local"]
    assert ds["actual_power_watts"] is None


# ── _poll_device: on-time tracking / usage recording ─────────────


@pytest.mark.asyncio
async def test_poll_device_records_usage_when_device_was_on(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse({"mode": "COOL", "current_temperature": "25", "target_temperature": "22"})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    now_epoch = worker_module._utcnow().timestamp()
    device = _device(_last_mode="COOL", _last_poll_epoch=now_epoch - 300)
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)

    assert device["_on_time_minutes"] > 0
    today = worker_module._today()
    assert "ac1.local" in worker_module._state["usage"]["daily"].get(today, {})


# ── _poll_device: lock-restore parse exception ────────────────────


@pytest.mark.asyncio
async def test_poll_device_lock_restore_tolerates_unparseable_reported_temp(worker_module, mocker):
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse({"mode": "COOL", "current_temperature": "25", "target_temperature": "not-a-number"})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    device = _device(lock_temp=True, locked_target_temp=22.0, _last_mode="COOL")
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("lock restore failed" in m for m in logs)


# ── _poll_device: temp_history key created if missing ─────────────


@pytest.mark.asyncio
async def test_poll_device_creates_temp_history_key_if_missing(worker_module, mocker):
    del worker_module._state["usage"]["temp_history"]
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse({"mode": "OFF", "current_temperature": "25", "target_temperature": "24"})
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    device = _device()
    worker_module._state["devices"].append(device)
    await worker_module._poll_device(device)

    assert "temp_history" in worker_module._state["usage"]


# ── _record_usage: outdoor temp parse exception ────────────────────


def test_record_usage_tolerates_unparseable_outdoor_temp(worker_module):
    """Regression test for a real bug found while writing this test:
    state.py's _est_watts() protected indoor/target parsing with
    try/except but NOT outdoor's penalty calculation, so a single
    malformed outdoor_temp reading crashed the entire watts estimate
    (and by extension _record_usage, and by extension that whole poll
    cycle) instead of gracefully falling back like a missing value
    does. Fixed in state.py by wrapping the outdoor penalty calc too."""
    device = {"host": "ac1.local", "name": "Living Room", "btu": 24000, "seer": 20}
    ds = {"mode": "COOL", "current_temperature": "25", "target_temperature": "22", "outdoor_temp": "not-a-number"}
    worker_module._record_usage(device, ds, 5.0)  # should not raise

    today = worker_module._today()
    bucket = worker_module._state["usage"]["daily"][today]["ac1.local"]
    assert bucket["avg_outdoor"] == []
    assert bucket["est_kwh"] > 0  # watts estimate still succeeded despite the bad outdoor reading


# ── _check_watchdog: malformed _last_seen ──────────────────────────


def test_check_watchdog_tolerates_malformed_last_seen(worker_module):
    device = {
        "host": "ac1.local", "name": "Test", "watchdog_minutes": 5,
        "_last_seen": "not-a-valid-timestamp",
        "_stale": False,
    }
    worker_module._check_watchdog(device)
    assert device["_stale"] is False


# ── _check_max_temp: parse exceptions and both-commands-fail ──────


@pytest.mark.asyncio
async def test_check_max_temp_tolerates_unparseable_indoor_temp(worker_module):
    device = {"host": "ac1.local", "name": "Test", "max_temp": 31.0, "_max_temp_active": False}
    worker_module._state["device_state"]["ac1.local"] = {"current_temperature": "not-a-number", "mode": "OFF"}
    await worker_module._check_max_temp(device)
    assert device["_max_temp_active"] is False


@pytest.mark.asyncio
async def test_check_max_temp_tolerates_unparseable_min_temp(worker_module, mocker):
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    device = {"host": "ac1.local", "name": "Test", "max_temp": 31.0, "_max_temp_active": False}
    worker_module._state["settings"]["max_temp_guard_start"] = 0
    worker_module._state["settings"]["max_temp_guard_end"] = 24
    worker_module._state["device_state"]["ac1.local"] = {
        "current_temperature": "32.0", "mode": "OFF", "min_temp": "not-a-number",
    }
    await worker_module._check_max_temp(device)
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("auto cool to 29.0" in m for m in logs)


@pytest.mark.asyncio
async def test_check_max_temp_deactivates_when_both_commands_fail(worker_module, mocker):
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    device = {"host": "ac1.local", "name": "Test", "max_temp": 31.0, "_max_temp_active": False}
    worker_module._state["settings"]["max_temp_guard_start"] = 0
    worker_module._state["settings"]["max_temp_guard_end"] = 24
    worker_module._state["device_state"]["ac1.local"] = {"current_temperature": "32.0", "mode": "OFF"}

    await worker_module._check_max_temp(device)
    assert device["_max_temp_active"] is False


# ── _check_schedules: malformed time, mode-only log branch, retry queue creation ──


@pytest.mark.asyncio
async def test_check_schedules_tolerates_malformed_time(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 2)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1], time="not-a-time"))

    await worker_module._check_schedules()
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert not any("Schedule firing" in m for m in logs)


@pytest.mark.asyncio
async def test_check_schedules_mode_only_without_power_logs_scheduled_mode(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 2)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1], power=None, mode="HEAT", temp=None))

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("scheduled mode → HEAT" in m for m in logs)


@pytest.mark.asyncio
async def test_check_schedules_creates_retry_queue_when_device_has_none(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 2)
    device = {"host": "ac1.local", "name": "Living Room"}
    worker_module._state["devices"].append(device)
    worker_module._state["schedules"].append(_sch(days=[1]))

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()
    assert len(device["_retry_queue"]) > 0


@pytest.mark.asyncio
async def test_check_schedules_end_time_malformed_tolerated(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 2)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1], end_time="not-a-time"))

    await worker_module._check_schedules()


@pytest.mark.asyncio
async def test_check_schedules_end_time_same_day_not_overnight(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 14, 2)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(
        _sch(days=[1], time="07:00", end_time="14:00")
    )
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("Schedule end" in m and "auto off" in m for m in logs)


@pytest.mark.asyncio
async def test_check_schedules_end_time_wrong_day_skipped(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 14, 2)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(
        _sch(days=[2], time="07:00", end_time="14:00")
    )
    await worker_module._check_schedules()
    assert worker_module._state["schedules"][0].get("_last_end_run") is None


@pytest.mark.asyncio
async def test_check_schedules_end_time_not_yet_happened_skipped(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 6, 0)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(
        _sch(days=[1], time="07:00", end_time="14:00")
    )
    await worker_module._check_schedules()
    assert worker_module._state["schedules"][0].get("_last_end_run") is None


@pytest.mark.asyncio
async def test_check_schedules_end_time_already_ran_today_skipped(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 14, 2)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(
        _sch(days=[1], time="07:00", end_time="14:00", _last_end_run="2026-01-05 14:00:00")
    )
    await worker_module._check_schedules()
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert not any("Schedule end" in m for m in logs)


@pytest.mark.asyncio
async def test_check_schedules_end_time_unknown_device_skipped(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 14, 2)
    worker_module._state["schedules"].append(
        _sch(host="nonexistent.local", days=[1], time="07:00", end_time="14:00")
    )
    await worker_module._check_schedules()


@pytest.mark.asyncio
async def test_check_schedules_end_time_creates_retry_queue_when_missing(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 14, 2)
    device = {"host": "ac1.local", "name": "Living Room"}
    worker_module._state["devices"].append(device)
    worker_module._state["schedules"].append(
        _sch(days=[1], time="07:00", end_time="14:00")
    )
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()
    assert len(device["_retry_queue"]) > 0


# ── _check_missed_schedules: malformed time, retry queue creation, end-time branches ──


@pytest.mark.asyncio
async def test_check_missed_schedules_tolerates_malformed_time(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 15)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1], time="not-a-time"))
    await worker_module._check_missed_schedules()


@pytest.mark.asyncio
async def test_check_missed_schedules_skips_when_not_yet_happened(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 6, 55)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1]))
    await worker_module._check_missed_schedules()
    assert worker_module._state["schedules"][0]["last_run"] is None


@pytest.mark.asyncio
async def test_check_missed_schedules_unknown_device_skipped(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 15)
    worker_module._state["schedules"].append(_sch(host="nonexistent.local", days=[1]))
    await worker_module._check_missed_schedules()


@pytest.mark.asyncio
async def test_check_missed_schedules_creates_retry_queue_when_missing(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 15)
    device = {"host": "ac1.local", "name": "Living Room"}
    worker_module._state["devices"].append(device)
    worker_module._state["schedules"].append(_sch(days=[1]))

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_missed_schedules()
    assert len(device["_retry_queue"]) > 0


@pytest.mark.asyncio
async def test_check_missed_schedules_end_time_malformed_tolerated(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 15)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1], end_time="not-a-time"))
    await worker_module._check_missed_schedules()


@pytest.mark.asyncio
async def test_check_missed_schedules_end_time_wrong_day_skipped(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 6, 55)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(
        _sch(days=[2], time="20:45", end_time="06:45")
    )
    await worker_module._check_missed_schedules()
    assert worker_module._state["schedules"][0].get("_last_end_run") is None


@pytest.mark.asyncio
async def test_check_missed_schedules_end_time_not_yet_happened_skipped(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 6, 30)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(
        _sch(days=[0], time="20:45", end_time="06:45")
    )
    await worker_module._check_missed_schedules()
    assert worker_module._state["schedules"][0].get("_last_end_run") is None


@pytest.mark.asyncio
async def test_check_missed_schedules_end_time_already_ran_skipped(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 6, 55)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(
        _sch(days=[0], time="20:45", end_time="06:45", _last_end_run="2026-01-05 06:45:00")
    )
    await worker_module._check_missed_schedules()
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert not any("Missed schedule end recovered" in m for m in logs)


@pytest.mark.asyncio
async def test_check_missed_schedules_end_time_outside_window_skipped(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 8, 0)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(
        _sch(days=[0], time="20:45", end_time="06:45")
    )
    await worker_module._check_missed_schedules()
    assert worker_module._state["schedules"][0].get("_last_end_run") is None


@pytest.mark.asyncio
async def test_check_missed_schedules_end_time_unknown_device_skipped(worker_module, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 6, 55)
    worker_module._state["schedules"].append(
        _sch(host="nonexistent.local", days=[0], time="20:45", end_time="06:45")
    )
    await worker_module._check_missed_schedules()


@pytest.mark.asyncio
async def test_check_missed_schedules_end_time_creates_retry_queue_when_missing(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 6, 55)
    device = {"host": "ac1.local", "name": "Living Room"}
    worker_module._state["devices"].append(device)
    worker_module._state["schedules"].append(
        _sch(days=[0], time="20:45", end_time="06:45")
    )
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_missed_schedules()
    assert len(device["_retry_queue"]) > 0


@pytest.mark.asyncio
async def test_check_schedules_end_time_catchup_window_exceeded_skipped(worker_module, monkeypatch):
    """_check_schedules' end-time firing has its own 10-minute
    catch-up window (distinct from _check_missed_schedules' 30-minute
    one) — if the loop hasn't run in over 10 minutes, this specific
    end-time trigger is skipped here (though it'd still be caught by
    _check_missed_schedules on the next cycle)."""
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 14, 15)  # 15 min after 14:00
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(
        _sch(days=[1], time="07:00", end_time="14:00")
    )
    await worker_module._check_schedules()
    assert worker_module._state["schedules"][0].get("_last_end_run") is None
