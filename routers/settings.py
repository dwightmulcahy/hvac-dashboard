"""Settings, exchange rate, and backup/restore endpoints."""

import datetime

from fastapi import APIRouter

from state import DEVICE_DEFAULTS, _add_log, _load_raw, _lock, _now_iso, _save_raw, _state
from worker import _fetch_exchange_rate

router = APIRouter(tags=["settings"])


@router.get("/settings")
async def get_settings():
    return _state["settings"]


@router.put("/settings")
async def update_settings(settings: dict):
    prev_verbose = _state["settings"].get("verbose_logging", False)
    _state["settings"].update(settings)
    new_verbose = _state["settings"].get("verbose_logging", False)
    if new_verbose != prev_verbose:
        _add_log(f"Verbose logging {'enabled' if new_verbose else 'disabled'}", "info")
    async with _lock:
        _save_raw(_state)
    return {"ok": True}


@router.get("/exchange-rate")
async def get_exchange_rate():
    """Return current exchange rate, fetching live if not cached today."""
    cached_date = _state["settings"].get("_rate_date", "")
    today = datetime.date.today().isoformat()
    if cached_date != today:
        result = await _fetch_exchange_rate()
        if result:
            return result
    rate = _state["settings"].get("exchangeRate", 455)
    return {"rate": rate, "date": cached_date or today}


@router.get("/backup")
async def backup():
    """Export full state as JSON for backup."""
    async with _lock:
        data = _load_raw()
    # strip internal runtime fields before export
    export = {
        "devices": [
            {k: v for k, v in d.items() if not k.startswith("_")}
            for d in data.get("devices", [])
        ],
        "schedules": data.get("schedules", []),
        "settings": data.get("settings", {}),
        "exported_at": _now_iso(),
    }
    return export


@router.post("/restore")
async def restore(data: dict):
    """Restore devices, schedules and settings from a backup."""
    async with _lock:
        if "devices" in data:
            for cfg in data["devices"]:
                existing = next((d for d in _state["devices"] if d["host"] == cfg.get("host")), None)
                if existing:
                    existing.update(cfg)
                else:
                    _state["devices"].append({**DEVICE_DEFAULTS, **cfg})
        if "schedules" in data:
            _state["schedules"] = data["schedules"]
        if "settings" in data:
            _state["settings"].update(data["settings"])
        _save_raw(_state)
    return {"ok": True, "message": "Restore complete"}
