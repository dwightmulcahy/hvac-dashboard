"""Schedule CRUD endpoints, plus conflict detection between schedules."""

import uuid

from fastapi import APIRouter

from models import ScheduleConfig
from state import SCHEDULE_DEFAULTS, _add_log, _lock, _save_raw, _state

router = APIRouter(tags=["schedules"])


def _detect_schedule_conflicts(new_sch: dict, exclude_id: str = None) -> list:
    """Return list of conflict descriptions for a schedule against existing ones."""
    conflicts = []
    new_host = new_sch.get("device_host")
    new_time = new_sch.get("time")
    new_days = set(new_sch.get("days", []))
    for s in _state["schedules"]:
        if not s.get("enabled", True):
            continue
        if s.get("id") == exclude_id:
            continue
        if s.get("device_host") != new_host:
            continue
        if s.get("time") != new_time:
            continue
        overlap = new_days & set(s.get("days", []))
        if overlap:
            day_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
            days_str = ",".join(day_names[d] for d in sorted(overlap))
            conflicts.append(f"conflicts with schedule {s['id']} @ {s['time']} on {days_str}")
    return conflicts


@router.get("/schedules")
async def get_schedules():
    return {"schedules": _state["schedules"]}


@router.post("/schedules")
async def add_schedule(cfg: ScheduleConfig):
    sch = {**SCHEDULE_DEFAULTS, **cfg.model_dump()}
    sch["id"] = cfg.id or str(uuid.uuid4())[:8]
    conflicts = _detect_schedule_conflicts(sch)
    if conflicts:
        for c in conflicts:
            _add_log(f"⚠ Schedule conflict: {c}", "warn")
    _state["schedules"].append(sch)
    async with _lock:
        _save_raw(_state)
    return {"ok": True, "id": sch["id"], "warnings": conflicts}


@router.put("/schedules/{sch_id}")
async def update_schedule(sch_id: str, cfg: ScheduleConfig):
    sch = next((s for s in _state["schedules"] if s["id"] == sch_id), None)
    if not sch:
        return {"ok": False, "error": "not found"}
    sch.update(cfg.model_dump())
    sch["id"] = sch_id
    conflicts = _detect_schedule_conflicts(sch, exclude_id=sch_id)
    if conflicts:
        for c in conflicts:
            _add_log(f"⚠ Schedule conflict: {c}", "warn")
    async with _lock:
        _save_raw(_state)
    return {"ok": True, "warnings": conflicts}


@router.delete("/schedules/{sch_id}")
async def delete_schedule(sch_id: str):
    _state["schedules"] = [s for s in _state["schedules"] if s["id"] != sch_id]
    async with _lock:
        _save_raw(_state)
    return {"ok": True}


@router.post("/schedules/{sch_id}/toggle")
async def toggle_schedule(sch_id: str):
    sch = next((s for s in _state["schedules"] if s["id"] == sch_id), None)
    if not sch:
        return {"ok": False}
    sch["enabled"] = not sch.get("enabled", True)
    async with _lock:
        _save_raw(_state)
    return {"ok": True, "enabled": sch["enabled"]}
