"""Tests for worker.py's schedule firing logic: _check_schedules and
_check_missed_schedules. These are distinct from test_schedules.py,
which only tests _build_schedule_commands (the pure command-building
helper) — these test the actual timing/firing/retry-queue behavior
that runs the built commands against a device.
"""

import datetime as real_datetime

import httpx
import pytest


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def json(self):
        return {}


def _freeze(monkeypatch, worker_module, y, mo, d, h, mi, s=0):
    class FrozenDateTime(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(y, mo, d, h, mi, s)
    monkeypatch.setattr(worker_module.datetime, "datetime", FrozenDateTime)
    return FrozenDateTime(y, mo, d, h, mi, s)


def _sch(id_="a", host="ac1.local", time="07:00", days=None, **overrides):
    sch = {
        "id": id_, "device_host": host, "device_name": "Test",
        "time": time, "end_time": None, "days": days if days is not None else [0, 1, 2, 3, 4, 5, 6],
        "power": "on", "mode": "COOL", "temp": 24, "enabled": True,
        "last_run": None,
    }
    sch.update(overrides)
    return sch


# ── _check_schedules: start-time firing ─────────────────────


@pytest.mark.asyncio
async def test_check_schedules_fires_when_time_has_passed(worker_module, mocker, monkeypatch):
    # 2026-01-05 is a Monday (weekday=0 -> isoweekday=1 -> js_day=1)
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 2)  # 2 min after 07:00
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1]))  # Monday

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("Schedule firing" in m for m in logs)
    assert worker_module._state["schedules"][0]["last_run"] is not None


@pytest.mark.asyncio
async def test_check_schedules_does_not_fire_before_scheduled_time(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 6, 55)  # before 07:00
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1]))

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()
    assert worker_module._state["schedules"][0]["last_run"] is None


@pytest.mark.asyncio
async def test_check_schedules_does_not_fire_outside_catchup_window(worker_module, mocker, monkeypatch):
    _freeze(worker_module=worker_module, monkeypatch=monkeypatch, y=2026, mo=1, d=5, h=8, mi=0)  # 60 min late
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1]))

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()
    assert worker_module._state["schedules"][0]["last_run"] is None


@pytest.mark.asyncio
async def test_check_schedules_skips_disabled_schedule(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 2)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1], enabled=False))

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()
    assert worker_module._state["schedules"][0]["last_run"] is None


@pytest.mark.asyncio
async def test_check_schedules_skips_wrong_day(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 2)  # Monday = js_day 1
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[2]))  # Tuesday only

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()
    assert worker_module._state["schedules"][0]["last_run"] is None


@pytest.mark.asyncio
async def test_check_schedules_skips_already_run_today(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 2)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1], last_run="2026-01-05 07:00:00"))

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert not any("Schedule firing" in m for m in logs)


@pytest.mark.asyncio
async def test_check_schedules_logs_warning_for_unknown_device(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 2)
    # no matching device in _state["devices"]
    worker_module._state["schedules"].append(_sch(host="nonexistent.local", days=[1]))

    await worker_module._check_schedules()
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("device" in m and "not found" in m for m in logs)


@pytest.mark.asyncio
async def test_check_schedules_queues_failed_command_for_retry(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 2)
    device = {"host": "ac1.local", "name": "Living Room", "_retry_queue": []}
    worker_module._state["devices"].append(device)
    worker_module._state["schedules"].append(_sch(days=[1]))

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(404)  # always fails
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()

    assert len(device["_retry_queue"]) > 0
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("partially failed" in m for m in logs)


@pytest.mark.asyncio
async def test_check_schedules_power_off_logs_scheduled_off(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 2)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1], power="off", mode=None, temp=None))

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("scheduled off" in m for m in logs)


# ── _check_schedules: end-time firing ────────────────────────


@pytest.mark.asyncio
async def test_check_schedules_end_time_fires_auto_off(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 6, 47)  # 2 min after 06:45, Monday morning
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    # overnight schedule "started" Sunday night -> days=[0] (Sunday), not Monday
    worker_module._state["schedules"].append(
        _sch(days=[0], time="20:45", end_time="06:45")
    )

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("Schedule end" in m and "auto off" in m for m in logs)
    assert worker_module._state["schedules"][0]["_last_end_run"] is not None


@pytest.mark.asyncio
async def test_check_schedules_end_time_overnight_checks_previous_day(worker_module, mocker, monkeypatch):
    """An overnight schedule (end < start) firing on Monday morning
    should count as belonging to Sunday's day-of-week for matching."""
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 6, 47)  # Monday morning
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    # days=[0] is Sunday only — the schedule "started" Sunday night,
    # ends Monday morning
    worker_module._state["schedules"].append(
        _sch(days=[0], time="20:45", end_time="06:45")
    )

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()
    assert worker_module._state["schedules"][0]["_last_end_run"] is not None


@pytest.mark.asyncio
async def test_check_schedules_end_time_failed_command_queues_retry(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 6, 47)  # Monday morning
    device = {"host": "ac1.local", "name": "Living Room", "_retry_queue": []}
    worker_module._state["devices"].append(device)
    worker_module._state["schedules"].append(
        _sch(days=[0], time="20:45", end_time="06:45")  # Sunday-night-started overnight schedule
    )

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(404)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_schedules()
    assert {"mode": "OFF"} in device["_retry_queue"]
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("Schedule end" in m and "failed" in m for m in logs)


# ── _check_missed_schedules ───────────────────────────────────


@pytest.mark.asyncio
async def test_check_missed_schedules_fires_within_window(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 15)  # 15 min after 07:00, within 30-min window
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1]))

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_missed_schedules()
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("Missed schedule recovered" in m for m in logs)


@pytest.mark.asyncio
async def test_check_missed_schedules_does_not_fire_outside_window(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 8, 0)  # 60 min late, past the 30-min window
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1]))

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_missed_schedules()
    assert worker_module._state["schedules"][0]["last_run"] is None


@pytest.mark.asyncio
async def test_check_missed_schedules_skips_already_run_today(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 15)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(_sch(days=[1], last_run="2026-01-05 07:00:00"))

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_missed_schedules()
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert not any("Missed schedule recovered" in m for m in logs)


@pytest.mark.asyncio
async def test_check_missed_schedules_end_time_fires_within_window(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 6, 55)  # 10 min after 06:45, Monday morning
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    worker_module._state["schedules"].append(
        _sch(days=[0], time="20:45", end_time="06:45")  # Sunday-night-started overnight schedule
    )

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_missed_schedules()
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("Missed schedule end recovered" in m for m in logs)
    assert worker_module._state["schedules"][0]["_last_end_run"] is not None


@pytest.mark.asyncio
async def test_check_missed_schedules_failed_send_queues_retry(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 7, 15)
    device = {"host": "ac1.local", "name": "Living Room", "_retry_queue": []}
    worker_module._state["devices"].append(device)
    worker_module._state["schedules"].append(_sch(days=[1]))

    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(404)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await worker_module._check_missed_schedules()
    assert len(device["_retry_queue"]) > 0
