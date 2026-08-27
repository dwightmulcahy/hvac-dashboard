"""Tests for models.py — request/response shape validation."""

import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, ".")
from models import CommandPayload, DeviceConfig, ScheduleConfig


def test_device_config_requires_host_and_name():
    with pytest.raises(ValidationError):
        DeviceConfig()


def test_device_config_defaults():
    d = DeviceConfig(host="ac1.local", name="Living Room")
    assert d.btu == 24000
    assert d.seer == 20
    assert d.max_temp is None
    assert d.beeper == "OFF"
    assert d.watchdog_minutes == 5
    assert d.lock_temp is False
    assert d.has_ir_emitter is False


def test_device_config_accepts_overrides():
    d = DeviceConfig(host="ac1.local", name="Bedroom", btu=12000, seer=18, max_temp=31.5, has_ir_emitter=True)
    assert d.btu == 12000
    assert d.max_temp == 31.5
    assert d.has_ir_emitter is True


def test_command_payload_requires_params():
    with pytest.raises(ValidationError):
        CommandPayload()


def test_command_payload_accepts_arbitrary_dict():
    p = CommandPayload(params={"mode": "COOL", "target_temperature": 24})
    assert p.params["mode"] == "COOL"


def test_schedule_config_requires_core_fields():
    with pytest.raises(ValidationError):
        ScheduleConfig(device_host="ac1.local")  # missing device_name, time, days


def test_schedule_config_valid():
    s = ScheduleConfig(
        device_host="ac1.local",
        device_name="Living Room",
        time="20:45",
        end_time="06:45",
        days=[0, 1, 2, 3, 4, 5, 6],
        power="on",
        mode="COOL",
        temp=24.0,
    )
    assert s.time == "20:45"
    assert s.end_time == "06:45"
    assert s.enabled is True  # default


def test_schedule_config_days_must_be_list_of_ints():
    with pytest.raises(ValidationError):
        ScheduleConfig(
            device_host="ac1.local",
            device_name="Living Room",
            time="20:45",
            days="not-a-list",
        )
