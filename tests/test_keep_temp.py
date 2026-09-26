"""Tests for _check_keep_temp — COOL-only server-side thermostat.

Pauses (powers off) a unit once the room is KEEP_TEMP_BAND below target,
resumes COOL once it's KEEP_TEMP_BAND above target after a minimum off time,
and never resumes a unit that something else (schedule, user, guard) turned off.
"""

from unittest.mock import AsyncMock

import pytest


def _device(worker_module, host="ac1.local", **kw):
    from state import DEVICE_DEFAULTS

    d = {**DEVICE_DEFAULTS, "_retry_queue": [], "host": host, "name": "Test AC", "keep_temp": True, **kw}
    worker_module._state["devices"] = [d]
    return d


def _ds(worker_module, host="ac1.local", **kw):
    worker_module._state["device_state"][host] = kw


@pytest.fixture
def send(worker_module, mocker):
    """Record commands and apply the real _send_cmd side effects without HTTP."""
    calls = []
    async def fake(host, params, *, keep_temp=False):
        calls.append((params, keep_temp))
        return True

    mocker.patch.object(worker_module, "_send_cmd", side_effect=fake)
    mocker.patch.object(worker_module, "_verify_temp_command", AsyncMock(return_value=True))
    return calls


@pytest.mark.asyncio
async def test_disabled_does_nothing(worker_module, send):
    d = _device(worker_module, keep_temp=False)
    _ds(worker_module, mode="COOL", current_temperature="24.5", target_temperature="25.5")
    await worker_module._check_keep_temp(d)
    assert send == []
    assert d["_keep_temp_paused"] is False


@pytest.mark.asyncio
async def test_pauses_when_room_below_target_minus_band(worker_module, send):
    d = _device(worker_module)
    _ds(worker_module, mode="COOL", current_temperature="24.5", target_temperature="25.5")
    await worker_module._check_keep_temp(d)
    assert send == [({"mode": "OFF"}, True)]
    assert d["_keep_temp_paused"] is True
    assert d["_keep_temp_target"] == 25.5


@pytest.mark.asyncio
async def test_does_not_pause_within_band(worker_module, send):
    d = _device(worker_module)
    _ds(worker_module, mode="COOL", current_temperature="25.2", target_temperature="25.5")
    await worker_module._check_keep_temp(d)
    assert send == []


@pytest.mark.parametrize("mode", ["HEAT", "AUTO", "DRY", "FAN_ONLY", "OFF"])
@pytest.mark.asyncio
async def test_only_cool_mode_pauses(worker_module, send, mode):
    d = _device(worker_module)
    _ds(worker_module, mode=mode, current_temperature="20", target_temperature="25.5")
    await worker_module._check_keep_temp(d)
    assert send == []


@pytest.mark.asyncio
async def test_skipped_while_max_temp_guard_active(worker_module, send):
    d = _device(worker_module, _max_temp_active=True)
    _ds(worker_module, mode="COOL", current_temperature="20", target_temperature="25.5")
    await worker_module._check_keep_temp(d)
    assert send == []


@pytest.mark.asyncio
async def test_resumes_after_min_off_when_room_warms(worker_module, send):
    now = worker_module._utcnow().timestamp()
    d = _device(worker_module, _keep_temp_paused=True, _keep_temp_target=25.5,
                _keep_temp_paused_at=now - worker_module.KEEP_TEMP_MIN_OFF_SECS - 1)
    _ds(worker_module, mode="OFF", current_temperature="26.0", target_temperature="25.5")
    await worker_module._check_keep_temp(d)
    assert send == [({"mode": "COOL"}, True)]
    assert d["_keep_temp_paused"] is False
    worker_module._verify_temp_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_min_off_time_blocks_resume(worker_module, send):
    now = worker_module._utcnow().timestamp()
    d = _device(worker_module, _keep_temp_paused=True, _keep_temp_target=25.5, _keep_temp_paused_at=now - 60)
    _ds(worker_module, mode="OFF", current_temperature="27.0")
    await worker_module._check_keep_temp(d)
    assert send == []
    assert d["_keep_temp_paused"] is True


@pytest.mark.asyncio
async def test_no_resume_below_target_plus_band(worker_module, send):
    d = _device(worker_module, _keep_temp_paused=True, _keep_temp_target=25.5, _keep_temp_paused_at=0)
    _ds(worker_module, mode="OFF", current_temperature="25.8")
    await worker_module._check_keep_temp(d)
    assert send == []


@pytest.mark.asyncio
async def test_external_power_on_cancels_pause(worker_module, send):
    d = _device(worker_module, _keep_temp_paused=True, _keep_temp_target=25.5, _keep_temp_paused_at=0)
    _ds(worker_module, mode="HEAT", current_temperature="27")
    await worker_module._check_keep_temp(d)
    assert send == []
    assert d["_keep_temp_paused"] is False


@pytest.mark.asyncio
async def test_disabling_while_paused_resumes_cool(worker_module, send):
    d = _device(worker_module, keep_temp=False, _keep_temp_paused=True, _keep_temp_target=25.5)
    _ds(worker_module, mode="OFF", current_temperature="24")
    await worker_module._check_keep_temp(d)
    assert send == [({"mode": "COOL"}, False)]
    assert d["_keep_temp_paused"] is False


# ── _send_cmd side effects (real function, mocked HTTP) ──

@pytest.mark.asyncio
async def test_non_keep_temp_mode_command_cancels_pause(worker_module, mock_device_response):
    """Schedule/user OFF while paused must stop keep-temp from turning the unit back on."""
    d = _device(worker_module, _keep_temp_paused=True, _keep_temp_target=25.5, _keep_temp_paused_at=0)
    assert await worker_module._send_cmd("ac1.local", {"mode": "OFF"})
    assert d["_keep_temp_paused"] is False


@pytest.mark.asyncio
async def test_keep_temp_mode_command_keeps_pause(worker_module, mock_device_response):
    d = _device(worker_module, _keep_temp_paused=True, _keep_temp_target=25.5, _keep_temp_paused_at=0)
    assert await worker_module._send_cmd("ac1.local", {"mode": "OFF"}, keep_temp=True)
    assert d["_keep_temp_paused"] is True


@pytest.mark.asyncio
async def test_temp_change_while_paused_updates_resume_target(worker_module, mock_device_response):
    d = _device(worker_module, _keep_temp_paused=True, _keep_temp_target=25.5, _keep_temp_paused_at=0)
    assert await worker_module._send_cmd("ac1.local", {"target_temperature": 23})
    assert d["_keep_temp_target"] == 23.0
    assert d["_keep_temp_paused"] is True


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


def test_keep_temp_flag_round_trips_through_device_api(client, auth_headers):
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "LR"})
    r = client.put("/devices/ac1.local", headers=auth_headers,
                   json={"host": "ac1.local", "name": "LR", "keep_temp": True})
    assert r.status_code == 200
    d = client.get("/devices", headers=auth_headers).json()["devices"][0]
    assert d["keep_temp"] is True
    assert d["_keep_temp_paused"] is False
