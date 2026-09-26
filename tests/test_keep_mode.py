"""Tests for _check_keep — KEEP mode thermostat for COOL and HEAT.

The unit's sensor reads intake air, so KEEP drops the unit to FAN_ONLY (not
OFF) once the room is KEEP_BAND past target, and returns it to its COOL/HEAT
mode once the room drifts KEEP_BAND back, after a minimum fan-only time. It
never overrides a mode that something else (schedule, user, guard) chose.
"""

from unittest.mock import AsyncMock

import pytest


def _device(worker_module, host="ac1.local", **kw):
    from state import DEVICE_DEFAULTS

    d = {**DEVICE_DEFAULTS, "_retry_queue": [], "host": host, "name": "Test AC", "keep_mode": True, **kw}
    worker_module._state["devices"] = [d]
    return d


def _paused(worker_module, resume="COOL", target=25.5, age=None, **kw):
    age = worker_module.KEEP_MIN_FAN_SECS + 1 if age is None else age
    return _device(worker_module, _keep_paused=True, _keep_target=target, _keep_resume_mode=resume,
                   _keep_paused_at=worker_module._utcnow().timestamp() - age, **kw)


def _ds(worker_module, host="ac1.local", **kw):
    worker_module._state["device_state"][host] = kw


@pytest.fixture
def send(worker_module, mocker):
    """Record commands without HTTP."""
    calls = []

    async def fake(host, params, *, keep=False):
        calls.append((params, keep))
        return True

    mocker.patch.object(worker_module, "_send_cmd", side_effect=fake)
    mocker.patch.object(worker_module, "_verify_temp_command", AsyncMock(return_value=True))
    return calls


@pytest.mark.asyncio
async def test_disabled_does_nothing(worker_module, send):
    d = _device(worker_module, keep_mode=False)
    _ds(worker_module, mode="COOL", current_temperature="24.5", target_temperature="25.5")
    await worker_module._check_keep(d)
    assert send == []
    assert d["_keep_paused"] is False


# ── COOL ──

@pytest.mark.asyncio
async def test_cool_pauses_to_fan_when_room_below_target_minus_band(worker_module, send):
    d = _device(worker_module)
    _ds(worker_module, mode="COOL", current_temperature="24.5", target_temperature="25.5")
    await worker_module._check_keep(d)
    assert send == [({"mode": "FAN_ONLY"}, True)]
    assert d["_keep_paused"] is True
    assert d["_keep_target"] == 25.5
    assert d["_keep_resume_mode"] == "COOL"


@pytest.mark.asyncio
async def test_cool_does_not_pause_within_band(worker_module, send):
    d = _device(worker_module)
    _ds(worker_module, mode="COOL", current_temperature="25.2", target_temperature="25.5")
    await worker_module._check_keep(d)
    assert send == []


@pytest.mark.asyncio
async def test_cool_resumes_after_min_fan_time_when_room_warms(worker_module, send):
    d = _paused(worker_module, "COOL", 25.5)
    _ds(worker_module, mode="FAN_ONLY", current_temperature="26.0", target_temperature="25.5")
    await worker_module._check_keep(d)
    assert send == [({"mode": "COOL"}, True)]
    assert d["_keep_paused"] is False
    worker_module._verify_temp_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_cool_no_resume_below_target_plus_band(worker_module, send):
    d = _paused(worker_module, "COOL", 25.5)
    _ds(worker_module, mode="FAN_ONLY", current_temperature="25.8")
    await worker_module._check_keep(d)
    assert send == []
    assert d["_keep_paused"] is True


# ── HEAT (mirrored) ──

@pytest.mark.asyncio
async def test_heat_pauses_to_fan_when_room_above_target_plus_band(worker_module, send):
    d = _device(worker_module)
    _ds(worker_module, mode="HEAT", current_temperature="22.5", target_temperature="22")
    await worker_module._check_keep(d)
    assert send == [({"mode": "FAN_ONLY"}, True)]
    assert d["_keep_resume_mode"] == "HEAT"


@pytest.mark.asyncio
async def test_heat_does_not_pause_below_target(worker_module, send):
    d = _device(worker_module)
    _ds(worker_module, mode="HEAT", current_temperature="21", target_temperature="22")
    await worker_module._check_keep(d)
    assert send == []


@pytest.mark.asyncio
async def test_heat_resumes_when_room_cools_below_target_minus_band(worker_module, send):
    d = _paused(worker_module, "HEAT", 22.0)
    _ds(worker_module, mode="FAN_ONLY", current_temperature="21.5")
    await worker_module._check_keep(d)
    assert send == [({"mode": "HEAT"}, True)]
    assert d["_keep_paused"] is False


@pytest.mark.asyncio
async def test_heat_no_resume_while_still_warm(worker_module, send):
    d = _paused(worker_module, "HEAT", 22.0)
    _ds(worker_module, mode="FAN_ONLY", current_temperature="22.3")
    await worker_module._check_keep(d)
    assert send == []


# ── shared ──

@pytest.mark.parametrize("mode", ["AUTO", "HEAT_COOL", "DRY", "FAN_ONLY", "OFF"])
@pytest.mark.asyncio
async def test_only_cool_and_heat_pause(worker_module, send, mode):
    d = _device(worker_module)
    _ds(worker_module, mode=mode, current_temperature="10", target_temperature="25.5")
    await worker_module._check_keep(d)
    _ds(worker_module, mode=mode, current_temperature="40", target_temperature="25.5")
    await worker_module._check_keep(d)
    assert send == []


@pytest.mark.asyncio
async def test_skipped_while_max_temp_guard_active(worker_module, send):
    d = _device(worker_module, _max_temp_active=True)
    _ds(worker_module, mode="COOL", current_temperature="20", target_temperature="25.5")
    await worker_module._check_keep(d)
    assert send == []


@pytest.mark.asyncio
async def test_min_fan_time_blocks_resume(worker_module, send):
    d = _paused(worker_module, "COOL", 25.5, age=60)
    _ds(worker_module, mode="FAN_ONLY", current_temperature="27.0")
    await worker_module._check_keep(d)
    assert send == []
    assert d["_keep_paused"] is True


@pytest.mark.parametrize("mode", ["OFF", "HEAT", "COOL"])
@pytest.mark.asyncio
async def test_external_mode_change_cancels_pause(worker_module, send, mode):
    d = _paused(worker_module, "COOL", 25.5, age=0)
    _ds(worker_module, mode=mode, current_temperature="27")
    await worker_module._check_keep(d)
    assert send == []
    assert d["_keep_paused"] is False


@pytest.mark.asyncio
async def test_disabling_while_paused_resumes_saved_mode(worker_module, send):
    d = _paused(worker_module, "HEAT", 22.0, keep_mode=False)
    _ds(worker_module, mode="FAN_ONLY", current_temperature="24")
    await worker_module._check_keep(d)
    assert send == [({"mode": "HEAT"}, False)]
    assert d["_keep_paused"] is False


# ── _send_cmd side effects (real function, mocked HTTP) ──

@pytest.mark.asyncio
async def test_non_keep_mode_command_cancels_pause(worker_module, mock_device_response):
    """Schedule/user OFF while paused must stop KEEP from switching the unit back on."""
    d = _paused(worker_module)
    assert await worker_module._send_cmd("ac1.local", {"mode": "OFF"})
    assert d["_keep_paused"] is False


@pytest.mark.asyncio
async def test_keep_mode_command_keeps_pause(worker_module, mock_device_response):
    d = _paused(worker_module)
    assert await worker_module._send_cmd("ac1.local", {"mode": "FAN_ONLY"}, keep=True)
    assert d["_keep_paused"] is True


@pytest.mark.asyncio
async def test_temp_change_while_paused_updates_resume_target(worker_module, mock_device_response):
    d = _paused(worker_module)
    assert await worker_module._send_cmd("ac1.local", {"target_temperature": 23})
    assert d["_keep_target"] == 23.0
    assert d["_keep_paused"] is True


@pytest.mark.asyncio
async def test_last_active_mode_tracked_on_send(worker_module, mock_device_response):
    d = _device(worker_module)
    await worker_module._send_cmd("ac1.local", {"mode": "DRY"})
    await worker_module._send_cmd("ac1.local", {"mode": "OFF"})
    assert d["_last_active_mode"] == "DRY"
    assert d["_last_mode"] == "OFF"


@pytest.mark.asyncio
async def test_last_active_mode_tracked_on_poll(worker_module, mocker):
    d = _device(worker_module)
    mocker.patch.object(worker_module, "_fetch_state",
                        AsyncMock(return_value={"mode": "HEAT", "current_temperature": 22}))
    mocker.patch.object(worker_module, "_fetch_sensors", AsyncMock(return_value={}))
    await worker_module._poll_device(d)
    assert d["_last_active_mode"] == "HEAT"
    worker_module._fetch_state.return_value = {"mode": "OFF", "current_temperature": 22}
    await worker_module._poll_device(d)
    assert d["_last_active_mode"] == "HEAT"


def test_keep_flag_round_trips_through_device_api(client, auth_headers):
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "LR"})
    r = client.put("/devices/ac1.local", headers=auth_headers,
                   json={"host": "ac1.local", "name": "LR", "keep_mode": True})
    assert r.status_code == 200
    d = client.get("/devices", headers=auth_headers).json()["devices"][0]
    assert d["keep_mode"] is True
    assert d["_keep_paused"] is False
