"""Sleep curve: worker._sleep_target (pure), _start_sleep_curve,
_check_sleep_curve, the /cmd stop, schedule validation, and AWAY replay.

COOL drifts warmer, HEAT cooler; the rise takes n intervals, the return
n+1 (peak held ≥1 interval, wake temp reached one interval before end);
ramps scale down to fit short windows; manual temp/mode stops it.
"""

import datetime as real_datetime
from unittest.mock import AsyncMock

import pytest

D = real_datetime.datetime


def _freeze(monkeypatch, worker_module, when: D):
    class Frozen(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(when.year, when.month, when.day, when.hour, when.minute, when.second)
    monkeypatch.setattr(worker_module.datetime, "datetime", Frozen)


def _sch(mode="COOL", temp=21.0, time="21:00", end_time="07:30", **sleep):
    cfg = {"rise": 2.0, "rise_hours": 2.0, "wake_hours": 2.0, "wake_temp": None, "step": 0.5, **sleep}
    return {"id": "s1", "device_host": "ac1.local", "time": time, "end_time": end_time,
            "days": [0, 1, 2, 3, 4, 5, 6], "power": "on", "mode": mode, "temp": temp,
            "eco": None, "sleep": cfg, "enabled": True, "last_run": None}


def _target(worker_module, sch, hhmm, day=5):
    start, end = worker_module._sleep_window(sch, D(2026, 1, 5, 21, 0))
    h, m = map(int, hhmm.split(":"))
    now = D(2026, 1, day, h, m)
    return worker_module._sleep_target(sch, start, end, now)


# ── _sleep_target ──

@pytest.mark.parametrize("hhmm,day,expected", [
    ("21:00", 5, 21.0), ("21:29", 5, 21.0), ("21:30", 5, 21.5), ("22:30", 5, 22.5),
    ("23:00", 5, 23.0), ("03:00", 6, 23.0), ("05:30", 6, 23.0), ("05:53", 6, 23.0),
    ("05:54", 6, 22.5), ("06:18", 6, 22.0), ("06:42", 6, 21.5), ("07:06", 6, 21.0), ("07:29", 6, 21.0),
])
def test_cool_standard_curve(worker_module, hhmm, day, expected):
    assert _target(worker_module, _sch(), hhmm, day) == expected


def test_outside_window_is_none(worker_module):
    assert _target(worker_module, _sch(), "07:30", 6) is None
    assert _target(worker_module, _sch(), "20:59", 5) is None


def test_heat_drifts_cooler(worker_module):
    sch = _sch(mode="HEAT", temp=22.0)
    assert _target(worker_module, sch, "23:00") == 20.0
    assert _target(worker_module, sch, "07:10", 6) == 22.0


def test_one_degree_steps(worker_module):
    sch = _sch(step=1.0)
    assert _target(worker_module, sch, "21:59") == 21.0
    assert _target(worker_module, sch, "22:00") == 22.0
    assert _target(worker_module, sch, "23:00") == 23.0
    assert _target(worker_module, sch, "06:20", 6) == 22.0
    assert _target(worker_module, sch, "06:50", 6) == 21.0


def test_custom_wake_temp(worker_module):
    sch = _sch(wake_temp=20.0)
    assert _target(worker_module, sch, "07:15", 6) == 20.0


def test_zero_wake_hours_holds_until_end(worker_module):
    sch = _sch(wake_hours=0)
    assert _target(worker_module, sch, "07:29", 6) == 23.0


def test_short_window_scales_ramps(worker_module):
    sch = _sch(time="21:00", end_time="23:00")  # 2h window, 4h of ramps → halved
    start, end = worker_module._sleep_window(sch, D(2026, 1, 5, 21, 0))
    vals = [worker_module._sleep_target(sch, start, end, D(2026, 1, 5, 21, 0) + real_datetime.timedelta(minutes=m))
            for m in range(0, 120)]
    assert vals[0] == 21.0 and max(vals) == 23.0 and vals[-1] == 21.0


def test_clamped_to_unit_range(worker_module):
    assert _target(worker_module, _sch(temp=29.5), "23:00") == 30.0
    assert _target(worker_module, _sch(mode="HEAT", temp=17.5), "23:00") == 17.0


# ── runtime ──

def _device(worker_module, **kw):
    from state import DEVICE_DEFAULTS
    d = {**DEVICE_DEFAULTS, "_retry_queue": [], "host": "ac1.local", "name": "Master BR", **kw}
    worker_module._state["devices"] = [d]
    worker_module._state["device_state"]["ac1.local"] = {"mode": "COOL", "target_temperature": "21.0"}
    return d


@pytest.fixture
def sent(worker_module, mocker):
    calls = []

    async def fake(host, params, *, keep=False):
        calls.append(params)
        return True
    mocker.patch.object(worker_module, "_send_cmd", side_effect=fake)
    mocker.patch.object(worker_module, "_verify_temp_command", AsyncMock(return_value=True))
    return calls


def _start(worker_module, d, sch, when=D(2026, 1, 5, 21, 0)):
    worker_module._state["schedules"] = [sch]
    worker_module._start_sleep_curve(d, sch, when, [{"mode": sch["mode"]}, {"target_temperature": sch["temp"]}])


@pytest.mark.asyncio
async def test_check_sends_step_once(worker_module, sent, monkeypatch):
    d = _device(worker_module)
    _start(worker_module, d, _sch())
    _freeze(monkeypatch, worker_module, D(2026, 1, 5, 21, 10))
    await worker_module._check_sleep_curve(d)
    assert sent == []  # still at base
    _freeze(monkeypatch, worker_module, D(2026, 1, 5, 21, 31))
    await worker_module._check_sleep_curve(d)
    await worker_module._check_sleep_curve(d)
    assert sent == [{"target_temperature": 21.5}]
    assert worker_module._verify_temp_command.await_count == 1


@pytest.mark.asyncio
async def test_catches_up_after_restart(worker_module, sent, monkeypatch):
    d = _device(worker_module)
    _start(worker_module, d, _sch())
    _freeze(monkeypatch, worker_module, D(2026, 1, 6, 2, 0))
    await worker_module._check_sleep_curve(d)
    assert sent == [{"target_temperature": 23.0}]


@pytest.mark.asyncio
async def test_failed_send_retries_next_poll(worker_module, mocker, monkeypatch):
    d = _device(worker_module)
    _start(worker_module, d, _sch())
    send = mocker.patch.object(worker_module, "_send_cmd", AsyncMock(return_value=False))
    mocker.patch.object(worker_module, "_verify_temp_command", AsyncMock(return_value=True))
    _freeze(monkeypatch, worker_module, D(2026, 1, 5, 22, 0))
    await worker_module._check_sleep_curve(d)
    await worker_module._check_sleep_curve(d)
    assert send.await_count == 2
    assert d["_sleep"]["last"] == 21.0


@pytest.mark.asyncio
async def test_mode_change_after_seen_stops(worker_module, sent, monkeypatch):
    d = _device(worker_module)
    _start(worker_module, d, _sch())
    _freeze(monkeypatch, worker_module, D(2026, 1, 5, 21, 10))
    await worker_module._check_sleep_curve(d)
    worker_module._state["device_state"]["ac1.local"]["mode"] = "OFF"
    _freeze(monkeypatch, worker_module, D(2026, 1, 5, 23, 0))
    await worker_module._check_sleep_curve(d)
    assert d["_sleep"]["stopped"] is True
    assert sent == []


@pytest.mark.asyncio
async def test_mode_not_yet_seen_does_not_stop(worker_module, sent, monkeypatch):
    """Schedule's mode command failed and is queued — not a user change."""
    d = _device(worker_module)
    worker_module._state["device_state"]["ac1.local"]["mode"] = "OFF"
    _start(worker_module, d, _sch())
    _freeze(monkeypatch, worker_module, D(2026, 1, 5, 22, 0))
    await worker_module._check_sleep_curve(d)
    assert d["_sleep"]["stopped"] is False
    assert sent == []


@pytest.mark.asyncio
async def test_keep_paused_updates_keep_target(worker_module, sent, monkeypatch):
    d = _device(worker_module, _keep_paused=True, _keep_resume_mode="COOL", _keep_target=21.0)
    worker_module._state["device_state"]["ac1.local"]["mode"] = "FAN_ONLY"
    _start(worker_module, d, _sch())
    _freeze(monkeypatch, worker_module, D(2026, 1, 5, 22, 0))
    await worker_module._check_sleep_curve(d)
    assert sent == []
    assert d["_keep_target"] == 22.0
    assert d["_sleep"]["stopped"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"lock_temp": True}, {"_max_temp_active": True}])
async def test_skipped_while_lock_or_guard(worker_module, sent, monkeypatch, extra):
    d = _device(worker_module, **extra)
    _start(worker_module, d, _sch())
    _freeze(monkeypatch, worker_module, D(2026, 1, 5, 23, 0))
    await worker_module._check_sleep_curve(d)
    assert sent == []


@pytest.mark.asyncio
async def test_skipped_while_away(worker_module, sent, monkeypatch):
    d = _device(worker_module)
    _start(worker_module, d, _sch())
    worker_module._state["settings"]["away_mode"] = True
    _freeze(monkeypatch, worker_module, D(2026, 1, 5, 23, 0))
    await worker_module._check_sleep_curve(d)
    assert sent == []


@pytest.mark.asyncio
async def test_cleared_after_end_and_when_schedule_removed(worker_module, sent, monkeypatch):
    d = _device(worker_module)
    _start(worker_module, d, _sch())
    _freeze(monkeypatch, worker_module, D(2026, 1, 6, 7, 30))
    await worker_module._check_sleep_curve(d)
    assert d["_sleep"] is None
    _start(worker_module, d, _sch())
    worker_module._state["schedules"] = []
    _freeze(monkeypatch, worker_module, D(2026, 1, 5, 23, 0))
    await worker_module._check_sleep_curve(d)
    assert d["_sleep"] is None


def test_other_schedule_ends_curve(worker_module):
    d = _device(worker_module)
    _start(worker_module, d, _sch())
    other = {**_sch(), "id": "s2", "sleep": None}
    worker_module._start_sleep_curve(d, other, D(2026, 1, 6, 1, 0), [{"mode": "COOL"}])
    assert d["_sleep"] is None


def test_eco_only_schedule_leaves_curve(worker_module):
    d = _device(worker_module)
    _start(worker_module, d, _sch())
    other = {**_sch(), "id": "s2", "sleep": None}
    worker_module._start_sleep_curve(d, other, D(2026, 1, 6, 1, 0), [{"eco": "off"}])
    assert d["_sleep"] is not None


@pytest.mark.asyncio
async def test_schedule_fire_starts_curve(worker_module, mocker, monkeypatch):
    d = _device(worker_module)
    worker_module._state["schedules"] = [_sch()]
    mocker.patch.object(worker_module, "_send_action", AsyncMock(return_value=True))
    mocker.patch.object(worker_module, "_verify_temp_command", AsyncMock(return_value=True))
    _freeze(monkeypatch, worker_module, D(2026, 1, 5, 21, 1))
    await worker_module._check_schedules()
    assert d["_sleep"]["start"] == "2026-01-05T21:00:00"
    assert d["_sleep"]["end"] == "2026-01-06T07:30:00"


@pytest.mark.asyncio
async def test_away_return_starts_curve_at_schedule_time(worker_module, sent, monkeypatch, mocker):
    d = _device(worker_module)
    worker_module._state["device_state"]["ac1.local"]["mode"] = "OFF"
    worker_module._state["schedules"] = [_sch()]
    mocker.patch.object(worker_module, "_send_eco", AsyncMock(return_value=True))
    _freeze(monkeypatch, worker_module, D(2026, 1, 5, 20, 0))
    await worker_module._away_on()
    _freeze(monkeypatch, worker_module, D(2026, 1, 5, 23, 30))
    await worker_module._away_off()
    assert d["_sleep"]["start"] == "2026-01-05T21:00:00"
    worker_module._state["device_state"]["ac1.local"]["mode"] = "COOL"
    sent.clear()
    await worker_module._check_sleep_curve(d)
    assert sent == [{"target_temperature": 23.0}]  # current point, not the base temp


# ── /cmd stop + validation ──

def test_manual_temp_stops_curve(client, auth_headers, api_module, mocker):
    from routers import devices_control
    device = {"host": "ac1.local", "name": "Master BR",
              "_sleep": {"sch_id": "s1", "stopped": False}}
    api_module._state["devices"].append(device)
    mocker.patch.object(devices_control, "_send_cmd", AsyncMock(return_value=True))
    mocker.patch.object(devices_control, "_verify_temp_command", AsyncMock(return_value=True))
    r = client.post("/devices/ac1.local/cmd", json={"params": {"target_temperature": 22}}, headers=auth_headers)
    assert r.status_code == 200
    assert device["_sleep"]["stopped"] is True


def _payload(**kw):
    p = {"device_host": "ac1.local", "device_name": "Master BR", "time": "21:00", "end_time": "07:30",
         "days": [1], "power": "on", "mode": "COOL", "temp": 21, "sleep": {"rise": 2}}
    p.update(kw)
    return p


def test_schedule_with_sleep_saves(client, auth_headers, api_module):
    r = client.post("/schedules", json=_payload(), headers=auth_headers)
    assert r.status_code == 200
    sch = api_module._state["schedules"][-1]
    assert sch["sleep"]["rise"] == 2 and sch["sleep"]["step"] == 0.5


@pytest.mark.parametrize("bad", [
    {"end_time": None}, {"mode": "FAN_ONLY"}, {"temp": None}, {"power": "off"}, {"eco": "on"},
    {"sleep": {"rise": 5}}, {"sleep": {"step": 0.25}},
])
def test_schedule_sleep_validation(client, auth_headers, bad):
    r = client.post("/schedules", json=_payload(**bad), headers=auth_headers)
    assert r.status_code == 422
