"""AWAY mode: worker._away_on / _away_off and POST /away/{state}.

On: snapshot every running unit's user-intended state (looking through the
max-temp guard and KEEP), turn it off, hold schedules. Off: per unit, the
latest schedule event inside the away window wins (start → its commands,
end → OFF); otherwise the snapshot is restored. The guard keeps running.
"""

import datetime as real_datetime
from unittest.mock import AsyncMock

import pytest


def _freeze(monkeypatch, worker_module, y, mo, d, h, mi):
    class Frozen(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(y, mo, d, h, mi)
    monkeypatch.setattr(worker_module.datetime, "datetime", Frozen)


def _devices(worker_module, *specs):
    from state import DEVICE_DEFAULTS

    out = []
    for host, mode, temp, extra in specs:
        d = {**DEVICE_DEFAULTS, "_retry_queue": [], "host": host, "name": host, **extra}
        worker_module._state["device_state"][host] = {"mode": mode, "target_temperature": temp}
        out.append(d)
    worker_module._state["devices"] = out
    return out


def _sch(host, time, end_time=None, days=(0, 1, 2, 3, 4, 5, 6), **kw):
    return {"id": f"{host}-{time}", "device_host": host, "time": time, "end_time": end_time,
            "days": list(days), "power": "on", "mode": "COOL", "temp": 21, "enabled": True,
            "last_run": None, **kw}


@pytest.fixture
def sent(worker_module, mocker):
    calls = []

    async def fake_cmd(host, params, *, keep=False):
        calls.append((host, params))
        return True

    async def fake_eco(host, state, *, remember=True):
        calls.append((host, {"eco": state}))
        return True

    mocker.patch.object(worker_module, "_send_cmd", side_effect=fake_cmd)
    mocker.patch.object(worker_module, "_send_eco", side_effect=fake_eco)
    mocker.patch.object(worker_module, "_verify_temp_command", AsyncMock(return_value=True))
    return calls


@pytest.mark.asyncio
async def test_away_on_turns_running_units_off_and_snapshots(worker_module, sent, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 18, 0)
    a, b = _devices(worker_module, ("a", "COOL", "22", {}), ("b", "OFF", "24", {}))
    await worker_module._away_on()
    assert worker_module._state["settings"]["away_mode"] is True
    assert sent == [("a", {"mode": "OFF"})]
    assert a["_away_snapshot"] == {"mode": "COOL", "target_temperature": "22"}
    assert b["_away_snapshot"] is None


@pytest.mark.asyncio
async def test_away_on_snapshots_pre_guard_state_and_releases_guard(worker_module, sent, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 18, 0)
    (a,) = _devices(worker_module, ("a", "COOL", "25", {
        "max_temp": 27.0, "_max_temp_active": True, "_pre_autocool_mode": "OFF", "_pre_autocool_temp": "24"}))
    await worker_module._away_on()
    assert a["_away_snapshot"] is None  # guard had it on; user had it off
    assert a["_max_temp_active"] is False  # free to re-trigger while away
    assert ("a", {"mode": "OFF"}) in sent


@pytest.mark.asyncio
async def test_away_on_snapshots_keep_resume_mode(worker_module, sent, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 18, 0)
    (a,) = _devices(worker_module, ("a", "FAN_ONLY", "25", {
        "_keep_paused": True, "_keep_resume_mode": "COOL", "_keep_target": 23.0}))
    await worker_module._away_on()
    assert a["_away_snapshot"] == {"mode": "COOL", "target_temperature": 23.0}


@pytest.mark.asyncio
async def test_schedules_held_while_away(worker_module, sent, monkeypatch):
    _devices(worker_module, ("a", "OFF", "24", {}))
    worker_module._state["schedules"] = [_sch("a", "19:00")]
    worker_module._state["settings"]["away_mode"] = True
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 19, 1)
    await worker_module._check_schedules()
    await worker_module._check_missed_schedules()
    assert sent == []
    assert worker_module._state["schedules"][0]["last_run"] is None


@pytest.mark.asyncio
async def test_away_off_restores_snapshot(worker_module, sent, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 18, 0)
    a, b = _devices(worker_module, ("a", "COOL", "22", {}), ("b", "OFF", "24", {}))
    await worker_module._away_on()
    sent.clear()
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 20, 0)
    await worker_module._away_off()
    assert worker_module._state["settings"]["away_mode"] is False
    assert sent == [("a", {"mode": "COOL"}), ("a", {"target_temperature": 22.0})]
    assert worker_module._verify_temp_command.await_count == 1
    assert "_away_snapshot" not in a


@pytest.mark.asyncio
async def test_away_off_eco_wanted_presses_eco_not_temp(worker_module, sent, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 18, 0)
    _devices(worker_module, ("a", "COOL", "24", {"eco_wanted": True}))
    await worker_module._away_on()
    sent.clear()
    await worker_module._away_off()
    assert sent == [("a", {"mode": "COOL"}), ("a", {"eco": "on"})]


@pytest.mark.asyncio
async def test_away_off_applies_schedule_that_started_while_away(worker_module, sent, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 18, 0)
    (a,) = _devices(worker_module, ("a", "OFF", "24", {}))
    worker_module._state["schedules"] = [_sch("a", "19:00", end_time="07:30", temp=21)]
    await worker_module._away_on()
    sent.clear()
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 22, 0)
    await worker_module._away_off()
    assert sent == [("a", {"mode": "COOL"}), ("a", {"target_temperature": 21})]
    assert worker_module._state["schedules"][0]["last_run"].startswith("2026-01-05")
    sent.clear()
    await worker_module._check_schedules()  # not fired a second time
    assert sent == []


@pytest.mark.asyncio
async def test_away_off_schedule_ended_while_away_overrides_snapshot(worker_module, sent, monkeypatch):
    # unit on when leaving; its schedule ended overnight while away → stays off
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 22, 0)
    _devices(worker_module, ("a", "COOL", "21", {}))
    worker_module._state["schedules"] = [_sch("a", "21:00", end_time="07:30")]
    await worker_module._away_on()
    sent.clear()
    _freeze(monkeypatch, worker_module, 2026, 1, 6, 9, 0)
    await worker_module._away_off()
    assert sent == [("a", {"mode": "OFF"})]
    assert worker_module._state["schedules"][0]["_last_end_run"].startswith("2026-01-06")


@pytest.mark.asyncio
async def test_away_off_latest_event_wins_across_days(worker_module, sent, monkeypatch):
    # away Mon 18:00 → Wed 20:00; Wed 19:00 start is the latest event
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 18, 0)
    _devices(worker_module, ("a", "OFF", "24", {}))
    worker_module._state["schedules"] = [_sch("a", "19:00", end_time="23:00", temp=20)]
    await worker_module._away_on()
    sent.clear()
    _freeze(monkeypatch, worker_module, 2026, 1, 7, 20, 0)
    await worker_module._away_off()
    assert sent == [("a", {"mode": "COOL"}), ("a", {"target_temperature": 20})]


@pytest.mark.asyncio
async def test_away_off_ignores_disabled_schedule(worker_module, sent, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 18, 0)
    _devices(worker_module, ("a", "COOL", "22", {}))
    worker_module._state["schedules"] = [_sch("a", "19:00", power="off", enabled=False)]
    await worker_module._away_on()
    sent.clear()
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 20, 0)
    await worker_module._away_off()
    assert sent[0] == ("a", {"mode": "COOL"})


@pytest.mark.asyncio
async def test_away_off_releases_guard_that_fired_while_away(worker_module, sent, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 18, 0)
    (a,) = _devices(worker_module, ("a", "COOL", "22", {"max_temp": 27.0}))
    await worker_module._away_on()
    a.update(_max_temp_active=True, _pre_autocool_mode="OFF")  # guard cooled while away
    await worker_module._away_off()
    assert a["_max_temp_active"] is False


# ── endpoint ──

def test_away_endpoint_toggles(client, auth_headers, api_module, mocker):
    from routers import system
    on = mocker.patch.object(system, "_away_on", AsyncMock())
    off = mocker.patch.object(system, "_away_off", AsyncMock())
    assert client.post("/away/on", headers=auth_headers).json()["away_mode"] is True
    api_module._state["settings"]["away_mode"] = True
    assert client.post("/away/on", headers=auth_headers).status_code == 200  # idempotent
    assert on.await_count == 1
    assert client.post("/away/off", headers=auth_headers).json()["away_mode"] is False
    assert off.await_count == 1


def test_away_endpoint_rejects_while_vacation(client, auth_headers, api_module):
    api_module._state["settings"]["vacation_mode"] = True
    assert client.post("/away/on", headers=auth_headers).status_code == 409


def test_away_endpoint_bad_state(client, auth_headers):
    assert client.post("/away/maybe", headers=auth_headers).status_code == 400


def test_away_endpoint_allowed_for_operator(client, auth_headers):
    client.post("/auth/users", headers=auth_headers,
                json={"username": "op", "password": "operatorpass", "role": "operator"})
    tok = client.post("/auth/login", json={"username": "op", "password": "operatorpass"}).json()["token"]
    r = client.post("/away/on", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
