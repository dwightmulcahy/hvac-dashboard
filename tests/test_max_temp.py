"""Tests for _check_max_temp — the server-side max-temp guard.

Covers the exact bugs found and fixed during development:
- hysteresis (turn off at max-1, not max, to prevent rapid cycling)
- guard hours only blocking the trigger, not the recovery/auto-off
- _max_temp_active persistence surviving across calls
"""

import pytest


def _device(host="ac1.local", name="Test AC", max_temp=31.0):
    return {
        "host": host, "name": name, "max_temp": max_temp,
        "_max_temp_active": False,
        "_pre_autocool_mode": None, "_pre_autocool_temp": None,
    }


def _set_device_state(worker_module, host, **kwargs):
    worker_module._state["device_state"][host] = kwargs


@pytest.fixture
def open_guard_hours(worker_module):
    """Make guard hours span the full day so trigger tests aren't
    accidentally time-of-day dependent."""
    worker_module._state["settings"]["max_temp_guard_start"] = 0
    worker_module._state["settings"]["max_temp_guard_end"] = 24


@pytest.mark.asyncio
async def test_no_max_temp_set_does_nothing(worker_module, mock_device_response, open_guard_hours):
    device = _device(max_temp=None)
    _set_device_state(worker_module, "ac1.local", mode="OFF", current_temperature="35")
    await worker_module._check_max_temp(device)
    assert device["_max_temp_active"] is False


@pytest.mark.asyncio
async def test_triggers_auto_cool_when_at_or_above_max(worker_module, mock_device_response, open_guard_hours):
    device = _device(max_temp=31.0)
    _set_device_state(worker_module, "ac1.local", mode="OFF", current_temperature="31.5", target_temperature="30")
    await worker_module._check_max_temp(device)
    assert device["_max_temp_active"] is True
    assert device["_pre_autocool_mode"] == "OFF"


@pytest.mark.asyncio
async def test_does_not_trigger_below_max(worker_module, mock_device_response, open_guard_hours):
    device = _device(max_temp=31.0)
    _set_device_state(worker_module, "ac1.local", mode="OFF", current_temperature="28.0")
    await worker_module._check_max_temp(device)
    assert device["_max_temp_active"] is False


@pytest.mark.asyncio
async def test_does_not_retrigger_while_already_active(worker_module, mock_device_response, open_guard_hours):
    """Once active, hitting max again shouldn't re-fire the trigger log/branch."""
    device = _device(max_temp=31.0)
    device["_max_temp_active"] = True
    _set_device_state(worker_module, "ac1.local", mode="COOL", current_temperature="31.5", target_temperature="29")
    await worker_module._check_max_temp(device)
    # still active, no change in pre_autocool fields (never overwritten a 2nd time)
    assert device["_max_temp_active"] is True
    assert device["_pre_autocool_mode"] is None  # was never set since trigger branch didn't run


@pytest.mark.asyncio
async def test_hysteresis_does_not_turn_off_at_exactly_max_minus_zero(worker_module, mock_device_response, open_guard_hours):
    """Regression test: turn-off threshold is max-1, not max. Sitting
    right at max_temp while active should NOT trigger auto-off yet."""
    device = _device(max_temp=31.0)
    device["_max_temp_active"] = True
    device["_pre_autocool_mode"] = "OFF"
    _set_device_state(worker_module, "ac1.local", mode="COOL", current_temperature="31.0", target_temperature="29")
    await worker_module._check_max_temp(device)
    assert device["_max_temp_active"] is True  # still active — hysteresis not yet satisfied


@pytest.mark.asyncio
async def test_hysteresis_turns_off_one_degree_below_max(worker_module, mock_device_response, open_guard_hours):
    device = _device(max_temp=31.0)
    device["_max_temp_active"] = True
    device["_pre_autocool_mode"] = "OFF"
    _set_device_state(worker_module, "ac1.local", mode="COOL", current_temperature="29.5", target_temperature="29")
    await worker_module._check_max_temp(device)
    assert device["_max_temp_active"] is False


@pytest.mark.asyncio
async def test_restores_previous_mode_when_it_was_not_off(worker_module, mock_device_response, open_guard_hours):
    """If the unit was already in HEAT before the guard kicked in,
    turning off the guard should restore HEAT, not just OFF."""
    device = _device(max_temp=31.0)
    device["_max_temp_active"] = True
    device["_pre_autocool_mode"] = "HEAT"
    device["_pre_autocool_temp"] = "20"
    _set_device_state(worker_module, "ac1.local", mode="COOL", current_temperature="29.0", target_temperature="29")
    await worker_module._check_max_temp(device)
    ds = worker_module._state["device_state"]["ac1.local"]
    assert ds["mode"] == "HEAT"


@pytest.mark.asyncio
async def test_guard_hours_block_trigger_outside_window(worker_module, mock_device_response, monkeypatch):
    """Regression test: guard hours should prevent the *trigger* (auto-on)
    outside the configured window."""
    worker_module._state["settings"]["max_temp_guard_start"] = 8
    worker_module._state["settings"]["max_temp_guard_end"] = 22

    import datetime as real_datetime

    class FrozenDateTime(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 3, 0, 0)  # 3am — outside 8-22 window

    monkeypatch.setattr(worker_module.datetime, "datetime", FrozenDateTime)

    device = _device(max_temp=31.0)
    _set_device_state(worker_module, "ac1.local", mode="OFF", current_temperature="32.0")
    await worker_module._check_max_temp(device)
    assert device["_max_temp_active"] is False  # blocked by guard hours


@pytest.mark.asyncio
async def test_guard_hours_never_block_the_auto_off_recovery(worker_module, mock_device_response, monkeypatch):
    """Regression test: the exact bug we hit in production — guard hours
    must NEVER block turning the unit back off once it cooled down,
    regardless of time of day."""
    worker_module._state["settings"]["max_temp_guard_start"] = 8
    worker_module._state["settings"]["max_temp_guard_end"] = 22

    import datetime as real_datetime

    class FrozenDateTime(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 6, 30, 0)  # 6:30am — outside 8-22 window

    monkeypatch.setattr(worker_module.datetime, "datetime", FrozenDateTime)

    device = _device(max_temp=31.0)
    device["_max_temp_active"] = True
    device["_pre_autocool_mode"] = "OFF"
    _set_device_state(worker_module, "ac1.local", mode="COOL", current_temperature="27.5", target_temperature="29")
    await worker_module._check_max_temp(device)
    # must turn off even though it's 6:30am, outside guard hours
    assert device["_max_temp_active"] is False
    ds = worker_module._state["device_state"]["ac1.local"]
    assert ds["mode"] == "OFF"
