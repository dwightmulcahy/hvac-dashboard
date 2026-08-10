"""Tests for schedule logic: _build_schedule_commands and
_detect_schedule_conflicts. These cover the exact bugs found and
fixed during development (duplicate on/off branches, overlap
detection) so they can't silently regress.
"""


def test_build_commands_power_off_ignores_mode_and_temp(worker_module):
    sch = {"power": "off", "mode": "COOL", "temp": 24}
    commands = worker_module._build_schedule_commands(sch)
    assert commands == [{"mode": "OFF"}]


def test_build_commands_mode_only(worker_module):
    sch = {"power": None, "mode": "COOL", "temp": None}
    commands = worker_module._build_schedule_commands(sch)
    assert commands == [{"mode": "COOL"}]


def test_build_commands_temp_only(worker_module):
    sch = {"power": None, "mode": None, "temp": 22.5}
    commands = worker_module._build_schedule_commands(sch)
    assert commands == [{"target_temperature": 22.5}]


def test_build_commands_mode_and_temp_together(worker_module):
    sch = {"power": "on", "mode": "COOL", "temp": 24}
    commands = worker_module._build_schedule_commands(sch)
    assert commands == [{"mode": "COOL"}, {"target_temperature": 24}]


def test_build_commands_power_on_without_mode_sends_nothing_for_mode(worker_module):
    """power=on with no explicit mode shouldn't invent a mode command."""
    sch = {"power": "on", "mode": None, "temp": None}
    commands = worker_module._build_schedule_commands(sch)
    assert commands == []


def test_build_commands_empty_schedule_produces_no_commands(worker_module):
    sch = {"power": None, "mode": None, "temp": None}
    assert worker_module._build_schedule_commands(sch) == []


# ── Conflict detection ──────────────────────────────────────


def _sch(id_, host, time, days, enabled=True):
    return {
        "id": id_, "device_host": host, "device_name": "Test",
        "time": time, "end_time": None, "days": days,
        "power": "on", "mode": "COOL", "temp": 24, "enabled": enabled,
    }


def test_no_conflict_different_devices(schedules_router_module):
    schedules_router_module._state["schedules"] = [_sch("a", "ac1.local", "07:00", [0, 1, 2, 3, 4])]
    new = _sch("b", "ac2.local", "07:00", [0, 1, 2, 3, 4])
    conflicts = schedules_router_module._detect_schedule_conflicts(new)
    assert conflicts == []


def test_no_conflict_different_times(schedules_router_module):
    schedules_router_module._state["schedules"] = [_sch("a", "ac1.local", "07:00", [0, 1, 2, 3, 4])]
    new = _sch("b", "ac1.local", "20:00", [0, 1, 2, 3, 4])
    conflicts = schedules_router_module._detect_schedule_conflicts(new)
    assert conflicts == []


def test_no_conflict_different_days(schedules_router_module):
    schedules_router_module._state["schedules"] = [_sch("a", "ac1.local", "07:00", [0, 6])]  # weekend
    new = _sch("b", "ac1.local", "07:00", [1, 2, 3, 4, 5])  # weekday
    conflicts = schedules_router_module._detect_schedule_conflicts(new)
    assert conflicts == []


def test_conflict_same_device_time_and_overlapping_day(schedules_router_module):
    schedules_router_module._state["schedules"] = [_sch("a", "ac1.local", "07:00", [0, 1, 2, 3, 4])]
    new = _sch("b", "ac1.local", "07:00", [1])  # Monday overlaps
    conflicts = schedules_router_module._detect_schedule_conflicts(new)
    assert len(conflicts) == 1
    assert "Mon" in conflicts[0]


def test_disabled_schedule_does_not_conflict(schedules_router_module):
    schedules_router_module._state["schedules"] = [_sch("a", "ac1.local", "07:00", [0, 1, 2, 3, 4], enabled=False)]
    new = _sch("b", "ac1.local", "07:00", [1])
    conflicts = schedules_router_module._detect_schedule_conflicts(new)
    assert conflicts == []


def test_exclude_id_lets_editing_same_schedule_not_self_conflict(schedules_router_module):
    schedules_router_module._state["schedules"] = [_sch("a", "ac1.local", "07:00", [0, 1, 2, 3, 4])]
    edited = _sch("a", "ac1.local", "07:00", [0, 1, 2, 3, 4])
    conflicts = schedules_router_module._detect_schedule_conflicts(edited, exclude_id="a")
    assert conflicts == []
