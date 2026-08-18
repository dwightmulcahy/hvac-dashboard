"""Maintenance reminder CRUD endpoints, plus due/overdue status
computation for each item.

A reminder is either device-scoped (device_host set) or whole-house
(device_host is None, e.g. an annual service call). trigger_type picks
which interval matters: "days" counts from last_done_at, "runtime_hours"
counts from a snapshot of the device's own _on_time_minutes taken when
the reminder was last completed — that field already accumulates
forever in worker.py regardless of what the dashboard/kiosk choose to
display, so this reuses it rather than tracking a second counter.
Whole-house items are always days-based; runtime_hours only means
anything when there's a specific device's own runtime to measure
against.

Status (due/overdue/remaining) is computed on every GET, not stored —
same approach schedules.py already uses for next-occurrence, so a
reminder's status is always current relative to *now* rather than
whatever it was the last time something happened to write state.json.
"""

import uuid

from fastapi import APIRouter

from state import MAINTENANCE_DEFAULTS, _lock, _state, _save_raw, _add_log, _now_iso
from models import MaintenanceConfig
from maintenance_logic import _device_on_time_minutes, _maintenance_status

router = APIRouter(tags=["maintenance"])


@router.get("/maintenance")
async def get_maintenance():
    return {"maintenance": [{**item, "status": _maintenance_status(item)} for item in _state["maintenance"]]}


@router.post("/maintenance")
async def add_maintenance(cfg: MaintenanceConfig):
    item = {**MAINTENANCE_DEFAULTS, **cfg.model_dump()}
    item["id"] = cfg.id or str(uuid.uuid4())[:8]
    if not item.get("last_done_at"):
        item["last_done_at"] = _now_iso()
    if item.get("device_host"):
        item["last_done_runtime_minutes"] = _device_on_time_minutes(item["device_host"])
    item["created_at"] = _now_iso()
    _state["maintenance"].append(item)
    async with _lock:
        _save_raw(_state)
    _add_log(f"Maintenance reminder added: {item['name']}", "info")
    return {"ok": True, "id": item["id"]}


@router.put("/maintenance/{item_id}")
async def update_maintenance(item_id: str, cfg: MaintenanceConfig):
    item = next((m for m in _state["maintenance"] if m["id"] == item_id), None)
    if not item:
        return {"ok": False, "error": "not found"}
    item.update(cfg.model_dump())
    item["id"] = item_id
    async with _lock:
        _save_raw(_state)
    return {"ok": True}


@router.delete("/maintenance/{item_id}")
async def delete_maintenance(item_id: str):
    _state["maintenance"] = [m for m in _state["maintenance"] if m["id"] != item_id]
    async with _lock:
        _save_raw(_state)
    return {"ok": True}


@router.post("/maintenance/{item_id}/complete")
async def complete_maintenance(item_id: str):
    item = next((m for m in _state["maintenance"] if m["id"] == item_id), None)
    if not item:
        return {"ok": False, "error": "not found"}
    completed_at = _now_iso()
    runtime_minutes = _device_on_time_minutes(item["device_host"]) if item.get("device_host") else None
    item["last_done_at"] = completed_at
    if item.get("device_host"):
        item["last_done_runtime_minutes"] = runtime_minutes
    # service history — append-only, capped at last 50 entries
    item.setdefault("completed_log", []).insert(0, {
        "completed_at": completed_at,
        "runtime_hours_at_completion": round(runtime_minutes / 60, 1) if runtime_minutes is not None else None,
    })
    item["completed_log"] = item["completed_log"][:50]
    # a completion always clears any pending overdue notification —
    # the worker will re-set this on its next cycle if still overdue
    item["_notified_overdue"] = False
    async with _lock:
        _save_raw(_state)
    _add_log(f"Maintenance completed: {item['name']}", "ok")
    return {"ok": True}
