"""
HVAC Dashboard — maintenance status computation.

Pure logic shared between routers/maintenance.py (computes status on
every GET) and worker.py (computes status once per background-loop
cycle to detect overdue items and fire notifications). Split out into
its own module — rather than living in either caller — so worker.py
never has to import from routers/ (see ARCHITECTURE.md's one-way
dependency graph: state.py ← maintenance_logic.py ← {worker, routers}).

Only depends on state.py, so it can never be part of a circular import.
"""

import datetime

from state import _state, _utcnow


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
