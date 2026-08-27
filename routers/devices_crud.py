"""Device CRUD: list, add, update, delete, reorder, temperature history."""

from fastapi import APIRouter

from models import DeviceConfig
from state import DEVICE_DEFAULTS, _add_log, _lock, _save_raw, _state

router = APIRouter(tags=["devices"])


@router.get("/devices/{host:path}/temp-history")
async def get_temp_history(host: str):
    history = _state["usage"].get("temp_history", {}).get(host, [])
    return {"host": host, "history": history}


@router.post("/devices/reorder")
async def reorder_devices(data: dict):
    """Reorder devices. body: {hosts: ["host1", "host2", ...]}"""
    hosts = data.get("hosts", [])
    lookup = {d["host"]: d for d in _state["devices"]}
    reordered = [lookup[h] for h in hosts if h in lookup]
    # append any not in the list (shouldn't happen, but be safe)
    seen = set(hosts)
    for d in _state["devices"]:
        if d["host"] not in seen:
            reordered.append(d)
    _state["devices"] = reordered
    async with _lock:
        _save_raw(_state)
    return {"ok": True}


@router.get("/devices")
async def get_devices():
    devices = []
    for d in _state["devices"]:
        ds = _state["device_state"].get(d["host"], {})
        devices.append({**d, "state": ds})
    return {"devices": devices}


@router.post("/devices")
async def add_device(cfg: DeviceConfig):
    existing = next((d for d in _state["devices"] if d["host"] == cfg.host), None)
    if existing:
        existing.update(cfg.model_dump())
        was_new = False
    else:
        _state["devices"].append({**DEVICE_DEFAULTS, **cfg.model_dump()})
        was_new = True
    async with _lock:
        _save_raw(_state)
    return {"ok": True, "new": was_new}


@router.put("/devices/{host:path}")
async def update_device(host: str, cfg: DeviceConfig):
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    if not device:
        if not any(d["host"] == cfg.host for d in _state["devices"]):
            _state["devices"].append({**DEVICE_DEFAULTS, **cfg.model_dump()})
    else:
        host_changed = cfg.host and cfg.host != host
        device.update(cfg.model_dump())
        if host_changed:
            # clear stale state and retry queue when host changes
            device["_retry_queue"] = []
            device["_stale"] = False
            device["_consecutive_failures"] = 0
            _state["device_state"].pop(host, None)
            name = device.get("name", host)
            _add_log(f"{name}: host changed to {cfg.host} — retry queue cleared", "info")
            # runtime_hours-based maintenance reminders reference a
            # device by host string, not by the device dict itself —
            # without this, _device_on_time_minutes(old_host) stops
            # matching any device after the rename, reads as 0
            # accumulated hours, and the reminder's overdue countdown
            # silently resets instead of carrying forward (the device
            # object itself — and its _on_time_minutes accumulator —
            # is unaffected by the rename; only the maintenance item's
            # stored reference goes stale).
            repointed = 0
            for item in _state["maintenance"]:
                if item.get("device_host") == host:
                    item["device_host"] = cfg.host
                    repointed += 1
            if repointed:
                _add_log(f"{name}: {repointed} maintenance reminder(s) repointed to {cfg.host}", "info")
    async with _lock:
        _save_raw(_state)
    return {"ok": True}


@router.delete("/devices/{host:path}")
async def delete_device(host: str):
    _state["devices"] = [d for d in _state["devices"] if d["host"] != host]
    async with _lock:
        _save_raw(_state)
    return {"ok": True}
