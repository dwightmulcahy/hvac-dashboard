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

import datetime
import uuid

from fastapi import APIRouter

from state import MAINTENANCE_DEFAULTS, _lock, _state, _save_raw, _add_log, _now_iso, _utcnow
from models import MaintenanceConfig

router = APIRouter(tags=["maintenance"])


def _device_on_time_minutes(host: str) -> float:
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    return device.get("_on_time_minutes", 0.0) if device else 0.0


def _maintenance_status(item: dict) -> dict:
    """Pure given _state (reads current device on-time for
    runtime-based items) — no side effects, safe to call on every read."""
    now = _utcnow()
    trigger = item.get("trigger_type", "days")

    if trigger == "runtime_hours" and item.get("device_host"):
        current = _device_on_time_minutes(item["device_host"])
        baseline = item.get("last_done_runtime_minutes") or 0.0
        hours_since = max(0.0, (current - baseline) / 60)
        interval = item.get("interval_hours") or 1
        remaining = interval - hours_since
        return {
            "hours_since_done": round(hours_since, 1),
            "hours_remaining": round(remaining, 1),
            "overdue": remaining <= 0,
            "due_soon": 0 < remaining <= max(1, interval * 0.1),
        }

    last_done = item.get("last_done_at")
    try:
        last_dt = datetime.datetime.fromisoformat(last_done) if last_done else now
    except (ValueError, TypeError):
        last_dt = now
    interval = item.get("interval_days") or 1
    due_at = last_dt + datetime.timedelta(days=interval)
    remaining_days = (due_at - now).total_seconds() / 86400
    return {
        "days_remaining": round(remaining_days, 1),
        "overdue": remaining_days <= 0,
        "due_soon": 0 < remaining_days <= max(1, interval * 0.1),
    }


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
    item["last_done_at"] = _now_iso()
    if item.get("device_host"):
        item["last_done_runtime_minutes"] = _device_on_time_minutes(item["device_host"])
    async with _lock:
        _save_raw(_state)
    _add_log(f"Maintenance completed: {item['name']}", "ok")
    return {"ok": True}
