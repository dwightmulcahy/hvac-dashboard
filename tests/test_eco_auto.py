"""ECO AUTO (per-device `eco_auto`, replaces KEEP): in COOL, ECO on while the
room is below target and off while it's above; HEAT is mirrored. Acts only
when the decision changes, so a manual ECO toggle holds until the room
crosses target again."""

import json

import pytest


def _device(worker_module, **kw):
    from state import DEVICE_DEFAULTS

    d = {**DEVICE_DEFAULTS, "_retry_queue": [], "host": "ac1.local", "name": "LR", "eco_auto": True, **kw}
    worker_module._state["devices"] = [d]
    return d


def _ds(worker_module, **kw):
    worker_module._state["device_state"]["ac1.local"] = {"target_temperature": "24", **kw}


@pytest.fixture
def eco_presses(worker_module, mocker):
    calls = []

    async def fake(host, state, *, remember=True):
        calls.append(state)
        if remember:
            worker_module._state["devices"][0]["eco_wanted"] = state == "on"
        return True

    mocker.patch.object(worker_module, "_send_eco", side_effect=fake)
    return calls


@pytest.mark.parametrize("mode,indoor,want", [
    ("COOL", "23.5", "on"), ("COOL", "24.5", "off"),
    ("HEAT", "24.5", "on"), ("HEAT", "23.5", "off"),
])
@pytest.mark.asyncio
async def test_direction_per_mode(worker_module, eco_presses, mode, indoor, want):
    d = _device(worker_module, eco_wanted=(want == "off"))
    _ds(worker_module, mode=mode, current_temperature=indoor, eco=(want == "off"))
    await worker_module._check_eco_auto(d)
    assert eco_presses == [want]
    assert d["eco_wanted"] is (want == "on")


@pytest.mark.asyncio
async def test_at_target_no_change(worker_module, eco_presses):
    d = _device(worker_module)
    _ds(worker_module, mode="COOL", current_temperature="24.0", eco=False)
    await worker_module._check_eco_auto(d)
    assert eco_presses == []


@pytest.mark.asyncio
async def test_acts_only_on_change_so_manual_toggle_holds(worker_module, eco_presses):
    d = _device(worker_module)
    _ds(worker_module, mode="COOL", current_temperature="23.0", eco=False)
    await worker_module._check_eco_auto(d)
    assert eco_presses == ["on"]
    d["eco_wanted"] = False  # user turned ECO off manually
    worker_module._state["device_state"]["ac1.local"]["eco"] = False
    await worker_module._check_eco_auto(d)
    assert eco_presses == ["on"]
    worker_module._state["device_state"]["ac1.local"]["current_temperature"] = "25.0"
    await worker_module._check_eco_auto(d)
    worker_module._state["device_state"]["ac1.local"]["current_temperature"] = "23.0"
    await worker_module._check_eco_auto(d)
    assert eco_presses == ["on", "on"]  # already off at 25 → no press; back below → on


@pytest.mark.asyncio
async def test_already_in_wanted_state_records_without_press(worker_module, eco_presses):
    d = _device(worker_module, eco_wanted=None)
    _ds(worker_module, mode="COOL", current_temperature="23.0", eco=True)
    await worker_module._check_eco_auto(d)
    assert eco_presses == []
    assert d["eco_wanted"] is True and d["_eco_auto_last"] is True


@pytest.mark.parametrize("extra,ds_extra", [
    ({"eco_auto": False}, {}),
    ({"_max_temp_active": True}, {}),
    ({"_stale": True}, {}),
    ({}, {"mode": "FAN_ONLY"}),
    ({}, {"mode": "OFF"}),
    ({}, {"error": "timeout"}),
    ({}, {"current_temperature": None}),
])
@pytest.mark.asyncio
async def test_skipped(worker_module, eco_presses, extra, ds_extra):
    d = _device(worker_module, **extra)
    _ds(worker_module, **{"mode": "COOL", "current_temperature": "22.0", "eco": False, **ds_extra})
    await worker_module._check_eco_auto(d)
    assert eco_presses == []


@pytest.mark.asyncio
async def test_skipped_while_away(worker_module, eco_presses):
    d = _device(worker_module)
    worker_module._state["settings"]["away_mode"] = True
    _ds(worker_module, mode="COOL", current_temperature="22.0", eco=False)
    await worker_module._check_eco_auto(d)
    assert eco_presses == []


@pytest.mark.asyncio
async def test_failed_press_retried_next_poll(worker_module, mocker):
    calls = []

    async def fail(host, state, *, remember=True):
        calls.append(state)
        return False
    mocker.patch.object(worker_module, "_send_eco", side_effect=fail)
    d = _device(worker_module)
    _ds(worker_module, mode="COOL", current_temperature="22.0", eco=False)
    await worker_module._check_eco_auto(d)
    await worker_module._check_eco_auto(d)
    assert calls == ["on", "on"]


def test_device_config_accepts_eco_auto(client, auth_headers, api_module):
    r = client.post("/devices", headers=auth_headers,
                    json={"host": "ac1.local", "name": "LR", "eco_auto": True})
    assert r.json()["ok"]
    assert api_module._state["devices"][0]["eco_auto"] is True


def test_load_migrates_keep_mode_to_eco_auto(state_module, temp_data_file):
    temp_data_file.write_text(json.dumps({"devices": [
        {"host": "a", "keep_mode": True, "_keep_paused": True, "_keep_target": 23, "eco_targets": {"COOL": 24}},
        {"host": "b", "keep_mode": False},
    ]}))
    a, b = state_module._load_raw()["devices"]
    assert a == {"host": "a", "eco_auto": True}
    assert b == {"host": "b", "eco_auto": False}


def test_restore_migrates_keep_mode(client, auth_headers, api_module):
    client.post("/restore", headers=auth_headers, json={"devices": [{"host": "a", "name": "A", "keep_mode": True}]})
    d = api_module._state["devices"][0]
    assert d["eco_auto"] is True and "keep_mode" not in d
