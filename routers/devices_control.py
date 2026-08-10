"""Device control: on-demand poll, commands, temp lock, display toggle,
beeper. Everything here sends a live request to a physical dongle."""

import asyncio
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Header

from state import _lock, _state, _save_raw, _add_log
from auth import _get_token_info
from worker import _poll_device, _check_max_temp, _send_cmd, _send_switch
from models import CommandPayload

router = APIRouter(tags=["devices"])


@router.post("/devices/{host:path}/poll")
async def poll_device_now(host: str):
    """Poll a single device immediately and return its current state."""
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    await _poll_device(device)
    await _check_max_temp(device)
    async with _lock:
        _save_raw(_state)
    ds = _state["device_state"].get(host, {})
    return {
        "ok": not device.get("_stale", False),
        "stale": device.get("_stale", False),
        "firmware_version": device.get("_firmware_version"),
        "state": ds,
    }


@router.post("/devices/{host:path}/cmd")
async def send_device_cmd(host: str, payload: CommandPayload, authorization: Optional[str] = Header(None)):
    info = _get_token_info(authorization)
    user = info["username"] if info else "api"
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    name = device["name"] if device else host
    ok = await _send_cmd(host, payload.params)
    if ok:
        ds = _state["device_state"].get(host, {})
        ds.update(payload.params)
        # log dashboard-initiated mode changes
        if "mode" in payload.params:
            m = payload.params["mode"]
            if m == "OFF":
                _add_log(f"{name}: turned off by {user}", "info")
            else:
                _add_log(f"{name}: turned on ({m}) by {user}", "ok")
        if "target_temperature" in payload.params:
            _add_log(f"{name}: set → {payload.params['target_temperature']}°C by {user}", "info")
    else:
        if device is not None:
            if "_retry_queue" not in device:
                device["_retry_queue"] = []
            device["_retry_queue"].append(payload.params)
            _add_log(f"{name}: command queued for retry {payload.params}", "warn")
    return {"ok": ok, "queued": not ok}


@router.post("/devices/{host:path}/lock")
async def set_lock_temp(host: str, data: dict):
    """Enable/disable temp lock. body: {lock: bool, target_temp: float|null}"""
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    if not device:
        return {"ok": False, "error": "not found"}
    device["lock_temp"] = data.get("lock", False)
    if data.get("target_temp") is not None:
        device["locked_target_temp"] = float(data["target_temp"])
    elif not device["lock_temp"]:
        device["locked_target_temp"] = None
    async with _lock:
        _save_raw(_state)
    if device["lock_temp"]:
        _add_log(f"{device['name']}: temp 🔒 locked at {device.get('locked_target_temp')}°C", "info")
    else:
        _add_log(f"{device['name']}: temp 🔓 unlocked", "info")
    return {"ok": True, "lock_temp": device["lock_temp"], "locked_target_temp": device.get("locked_target_temp")}


@router.post("/devices/{host:path}/display-toggle")
async def display_toggle(host: str):
    """Toggle the AC unit display on/off."""
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    name = device["name"] if device else host
    paths = [
        "button/Air%20Conditioner%20Display%20Toggle/press",
        "button/air_conditioner_display_toggle/press",
    ]
    for path in paths:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.post(f"http://{host}/{path}")
                if r.status_code < 300:
                    _add_log(f"{name}: display toggled", "info")
                    return {"ok": True}
        except Exception:
            pass
    return {"ok": False, "error": "display toggle not supported"}


@router.post("/devices/{host:path}/beeper/test")
async def test_beeper(host: str):
    """Send a quick beep without changing the saved beeper state."""
    ok_on = await _send_switch(host, "switch/air_conditioner_beeper/turn_on")
    await asyncio.sleep(0.5)
    ok_off = await _send_switch(host, "switch/air_conditioner_beeper/turn_off")
    # restore to saved state
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    if device:
        saved = device.get("beeper", "OFF")
        endpoint = "turn_on" if saved == "ON" else "turn_off"
        await _send_switch(host, f"switch/air_conditioner_beeper/{endpoint}")
    return {"ok": ok_on and ok_off}


@router.post("/devices/{host:path}/beeper/{state}")
async def set_beeper(host: str, state: str):
    val = "ON" if state == "on" else "OFF"
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    if device:
        device["beeper"] = val
    endpoint = f"switch/air_conditioner_beeper/turn_{state}"
    ok = await _send_switch(host, endpoint)
    async with _lock:
        _save_raw(_state)
    return {"ok": ok}
