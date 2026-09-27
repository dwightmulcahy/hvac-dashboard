"""ECO intent persistence: the Midea unit drops its ECO preset whenever the
mode or setpoint changes (and while in fan-only for KEEP). Once the user or a
schedule turns ECO on, worker._maintain_eco keeps re-applying it."""

from unittest.mock import AsyncMock

import pytest


def _device(worker_module, **kw):
    from state import DEVICE_DEFAULTS

    d = {**DEVICE_DEFAULTS, "_retry_queue": [], "host": "ac1.local", "name": "LR", **kw}
    worker_module._state["devices"] = [d]
    return d


def _ds(worker_module, **kw):
    worker_module._state["device_state"]["ac1.local"] = kw


@pytest.fixture
def eco_presses(worker_module, mocker):
    """Record ECO button presses without HTTP; keep the real intent bookkeeping."""
    calls = []

    async def fake(host, state, *, remember=True):
        calls.append((state, remember))
        if remember:
            dev = worker_module._state["devices"][0]
            dev["eco_wanted"] = state == "on"
            dev["_eco_reapply_tries"] = 0
            dev["_eco_gave_up_mode"] = None
        return True

    mocker.patch.object(worker_module, "_send_eco", side_effect=fake)
    return calls


# ── intent recording (real _send_eco, mocked HTTP) ──

@pytest.mark.parametrize("state,wanted", [("on", True), ("off", False)])
@pytest.mark.asyncio
async def test_send_eco_records_intent(worker_module, mock_device_response, state, wanted):
    d = _device(worker_module)
    assert await worker_module._send_eco("ac1.local", state)
    assert d["eco_wanted"] is wanted


@pytest.mark.asyncio
async def test_reapply_press_does_not_change_intent(worker_module, mock_device_response):
    d = _device(worker_module, eco_wanted=True)
    await worker_module._send_eco("ac1.local", "off", remember=False)
    assert d["eco_wanted"] is True


# ── _maintain_eco ──

@pytest.mark.asyncio
async def test_no_intent_never_touches_eco(worker_module, eco_presses):
    d = _device(worker_module, eco_wanted=None)
    _ds(worker_module, mode="COOL", eco=False)
    await worker_module._maintain_eco(d)
    await worker_module._maintain_eco(d, force=True)
    assert eco_presses == []


@pytest.mark.asyncio
async def test_intent_off_never_reenables(worker_module, eco_presses):
    d = _device(worker_module, eco_wanted=False)
    _ds(worker_module, mode="COOL", eco=False)
    await worker_module._maintain_eco(d)
    assert eco_presses == []


@pytest.mark.asyncio
async def test_poll_sees_eco_dropped_and_reapplies(worker_module, eco_presses):
    d = _device(worker_module, eco_wanted=True)
    _ds(worker_module, mode="COOL", eco=False)
    await worker_module._maintain_eco(d)
    assert eco_presses == [("on", False)]
    assert d["eco_wanted"] is True


@pytest.mark.asyncio
async def test_already_on_is_left_alone(worker_module, eco_presses):
    d = _device(worker_module, eco_wanted=True)
    _ds(worker_module, mode="COOL", eco=True)
    await worker_module._maintain_eco(d)
    assert eco_presses == []


@pytest.mark.parametrize("mode", ["OFF", "FAN_ONLY"])
@pytest.mark.asyncio
async def test_not_reapplied_while_off_or_fan(worker_module, eco_presses, mode):
    """KEEP's fan-only pause and a powered-off unit can't hold ECO — wait."""
    d = _device(worker_module, eco_wanted=True)
    _ds(worker_module, mode=mode, eco=False)
    await worker_module._maintain_eco(d)
    await worker_module._maintain_eco(d, force=True)
    assert eco_presses == []


@pytest.mark.asyncio
async def test_gives_up_per_mode_then_retries_after_mode_change(worker_module, eco_presses):
    d = _device(worker_module, eco_wanted=True)
    _ds(worker_module, mode="HEAT", eco=False)
    for _ in range(5):
        await worker_module._maintain_eco(d)
    assert len(eco_presses) == worker_module.ECO_REAPPLY_MAX_TRIES
    assert any("won't hold ECO in HEAT" in l["msg"] for l in worker_module._state["logs"])
    _ds(worker_module, mode="COOL", eco=False)
    await worker_module._maintain_eco(d)
    assert len(eco_presses) == worker_module.ECO_REAPPLY_MAX_TRIES + 1


@pytest.mark.asyncio
async def test_force_presses_even_when_cached_state_says_on(worker_module, eco_presses):
    """Right after our own temp/mode command the cached eco flag is stale."""
    d = _device(worker_module, eco_wanted=True)
    _ds(worker_module, mode="COOL", eco=True)
    await worker_module._maintain_eco(d, force=True)
    assert eco_presses == [("on", False)]


# ── integration points ──

@pytest.mark.asyncio
async def test_keep_resume_to_cool_reenables_eco(worker_module, eco_presses, mocker):
    """The user's example: KEEP pauses to fan (ECO lost), resumes COOL → ECO back."""
    mocker.patch.object(worker_module, "_send_cmd", AsyncMock(return_value=True))
    mocker.patch.object(worker_module, "_verify_temp_command", AsyncMock(return_value=True))
    d = _device(worker_module, eco_wanted=True, keep_mode=True, _keep_paused=True, _keep_target=24.0,
                _keep_resume_mode="COOL", _keep_paused_at=0)
    _ds(worker_module, mode="FAN_ONLY", current_temperature="25.0", target_temperature="24", eco=False)
    await worker_module._check_keep(d)
    assert worker_module._state["device_state"]["ac1.local"]["mode"] == "COOL"
    assert eco_presses == [("on", False)]


@pytest.mark.asyncio
async def test_schedule_temp_change_reenables_eco(worker_module, eco_presses, mocker, monkeypatch):
    import datetime as real_dt

    class Frozen(real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 5, 7, 2)
    monkeypatch.setattr(worker_module.datetime, "datetime", Frozen)
    mocker.patch.object(worker_module, "_send_cmd", AsyncMock(return_value=True))
    mocker.patch.object(worker_module, "_verify_temp_command", AsyncMock(return_value=True))
    _device(worker_module, eco_wanted=True)
    _ds(worker_module, mode="COOL", eco=True)
    worker_module._state["schedules"] = [{
        "id": "s", "device_host": "ac1.local", "device_name": "LR", "time": "07:00", "end_time": None,
        "days": [1], "power": None, "mode": None, "temp": 23, "eco": None, "enabled": True, "last_run": None,
    }]
    await worker_module._check_schedules()
    assert eco_presses == [("on", False)]


@pytest.mark.asyncio
async def test_schedule_with_explicit_eco_off_clears_intent(worker_module, eco_presses, mocker, monkeypatch):
    import datetime as real_dt

    class Frozen(real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 5, 7, 2)
    monkeypatch.setattr(worker_module.datetime, "datetime", Frozen)
    mocker.patch.object(worker_module, "_send_cmd", AsyncMock(return_value=True))
    mocker.patch.object(worker_module, "_verify_temp_command", AsyncMock(return_value=True))
    d = _device(worker_module, eco_wanted=True)
    _ds(worker_module, mode="COOL", eco=True)
    worker_module._state["schedules"] = [{
        "id": "s", "device_host": "ac1.local", "device_name": "LR", "time": "07:00", "end_time": None,
        "days": [1], "power": None, "mode": "COOL", "temp": 23, "eco": "off", "enabled": True, "last_run": None,
    }]
    await worker_module._check_schedules()
    assert eco_presses == [("off", True)]
    assert d["eco_wanted"] is False


def test_cmd_endpoint_temp_change_reenables_eco(client, auth_headers, api_module, mocker):
    import worker
    api_module._state["devices"].append({"host": "ac1.local", "name": "LR", "eco_wanted": True, "_retry_queue": []})
    api_module._state["device_state"]["ac1.local"] = {"mode": "COOL", "eco": True}
    mocker.patch("routers.devices_control._send_cmd", AsyncMock(return_value=True))
    mocker.patch("routers.devices_control._verify_temp_command", AsyncMock(return_value=True))
    send_eco = mocker.patch.object(worker, "_send_eco", AsyncMock(return_value=True))
    r = client.post("/devices/ac1.local/cmd", headers=auth_headers, json={"params": {"target_temperature": 23}})
    assert r.json()["ok"] is True
    send_eco.assert_awaited_once_with("ac1.local", "on", remember=False)
