"""System-level endpoints: health checks (including Uptime Kuma push
target), vacation mode, Watchtower update notifications, root status,
and usage reset."""

import datetime
import os

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from state import _state, _lock, _save_raw, _add_log, _utcnow, _now_iso
from worker import _send_cmd

router = APIRouter(tags=["system"])


@router.get("/health/push")
async def health_push():
    """
    Uptime Kuma push-compatible endpoint.
    Returns 200 with status=ok when all devices are healthy,
    503 when any device is stale.
    Add to Uptime Kuma as HTTP(s) monitor pointing to /api/health/push
    """
    now = _utcnow()
    device_health = []
    any_stale = False
    for d in _state["devices"]:
        last_seen = d.get("_last_seen")
        elapsed = None
        if last_seen:
            try:
                elapsed = round((now - datetime.datetime.fromisoformat(last_seen)).total_seconds() / 60, 1)
            except Exception:
                pass
        stale = d.get("_stale", False)
        if stale:
            any_stale = True
        device_health.append({
            "name": d["name"],
            "stale": stale,
            "minutes_since_seen": elapsed,
        })

    status_code = 503 if any_stale else 200
    stale_names = [d["name"] for d in device_health if d["stale"]]
    msg = f"OK — {len(_state['devices'])} devices online" if not any_stale \
          else f"DEGRADED — stale: {', '.join(stale_names)}"

    return JSONResponse(status_code=status_code, content={
        "status": "ok" if not any_stale else "degraded",
        "msg": msg,
        "devices": device_health,
        "timestamp": now.isoformat(),
    })


@router.get("/health")
async def health():
    """Detailed health check — per-device status, worker health, system info."""
    now = _utcnow()
    device_health = []
    for d in _state["devices"]:
        last_seen = d.get("_last_seen")
        elapsed = None
        if last_seen:
            try:
                elapsed = round((now - datetime.datetime.fromisoformat(last_seen)).total_seconds() / 60, 1)
            except Exception:
                pass
        ds = _state["device_state"].get(d["host"], {})
        device_health.append({
            "host": d["host"],
            "name": d["name"],
            "stale": d.get("_stale", False),
            "last_seen": last_seen,
            "minutes_since_seen": elapsed,
            "watchdog_minutes": d.get("watchdog_minutes", 5),
            "retry_queue_depth": len(d.get("_retry_queue", [])),
            "consecutive_failures": d.get("_consecutive_failures", 0),
            "mode": ds.get("mode"),
            "indoor_temp": ds.get("current_temperature"),
            "firmware": d.get("_firmware_version"),
            "max_temp_active": d.get("_max_temp_active", False),
        })
    stale_count = sum(1 for d in device_health if d["stale"])
    all_ok = stale_count == 0 and len(_state["devices"]) > 0
    # check if worker has been polling recently
    last_polls = [d.get("_last_seen") for d in _state["devices"] if d.get("_last_seen")]
    worker_last_poll = max(last_polls) if last_polls else None
    worker_stale = False
    if worker_last_poll:
        try:
            worker_age_mins = (now - datetime.datetime.fromisoformat(worker_last_poll)).total_seconds() / 60
            worker_stale = worker_age_mins > (_state["settings"].get("poll_interval", 120) / 60 * 3)
        except Exception:
            pass
    return {
        "status": "ok" if all_ok and not worker_stale else "degraded",
        "devices": device_health,
        "total": len(_state["devices"]),
        "online": sum(1 for d in device_health if not d["stale"]),
        "stale": stale_count,
        "vacation_mode": _state["settings"].get("vacation_mode", False),
        "schedules_active": sum(1 for s in _state["schedules"] if s.get("enabled")),
        "worker_stale": worker_stale,
        "worker_last_poll": worker_last_poll,
        "timestamp": now.isoformat(),
    }


@router.post("/vacation/{state}")
async def set_vacation(state: str):
    """Enable or disable vacation mode server-side."""
    enabled = state == "on"
    s = _state["settings"]
    s["vacation_mode"] = enabled
    vac_temp = s.get("vacation_max_temp", 32)

    if enabled:
        s["vacation_started_at"] = _now_iso()
        # save each device's current max_temp and pause schedules
        for d in _state["devices"]:
            d["_pre_vacation_max_temp"] = d.get("max_temp")
            d["_pre_vacation_mode"] = _state["device_state"].get(d["host"], {}).get("mode", "OFF")
            d["max_temp"] = vac_temp
            # turn off if on
            ds = _state["device_state"].get(d["host"], {})
            if ds.get("mode", "OFF") not in ("OFF", None):
                await _send_cmd(d["host"], {"mode": "OFF"})
                ds["mode"] = "OFF"
        for sch in _state["schedules"]:
            if sch.get("enabled", True):
                sch["_vacation_paused"] = True
                sch["enabled"] = False
        _add_log(f"🌴 Vacation mode ON — all units off, max temp {vac_temp}°C, schedules paused", "warn")
    else:
        s["vacation_started_at"] = None
        # restore saved max_temp per device and re-enable schedules
        for d in _state["devices"]:
            d["max_temp"] = d.pop("_pre_vacation_max_temp", None)
            d.pop("_pre_vacation_mode", None)
        for sch in _state["schedules"]:
            if sch.pop("_vacation_paused", False):
                sch["enabled"] = True
        _add_log("🌴 Vacation mode OFF — settings restored", "ok")

    async with _lock:
        _save_raw(_state)
    return {"ok": True, "vacation_mode": enabled}


@router.get("/vacation")
async def get_vacation():
    s = _state["settings"]
    return {
        "vacation_mode": s.get("vacation_mode", False),
        "vacation_max_temp": s.get("vacation_max_temp", 32),
        "vacation_started_at": s.get("vacation_started_at"),
        "vacation_days_limit": s.get("vacation_days_limit", 14),
    }


@router.post("/watchtower-notify")
async def watchtower_notify(data: dict):
    """Called by Watchtower webhook when a new image is pulled."""
    webhook_url = _state["settings"].get("watchtower_webhook", "")
    image = data.get("container", data.get("image", "hvac-dashboard"))
    tag = data.get("tag", "latest")
    msg = f"🐳 {image}:{tag} updated and restarted"
    _add_log(msg, "ok")
    # forward to user's webhook if configured
    if webhook_url:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(webhook_url, json={"text": msg, "title": "HVAC Dashboard Updated"})
        except Exception as e:
            _add_log(f"Watchtower webhook forward failed: {e}", "warn")
    return {"ok": True}


@router.get("/")
async def root():
    return {"status": "ok", "service": "HVAC Automation API",
            "version": os.environ.get("APP_VERSION", "dev"),
            "build": os.environ.get("BUILD_DATE", ""),
            "devices": len(_state["devices"]), "schedules": len(_state["schedules"])}


@router.delete("/reset")
async def reset():
    _state["usage"] = {"daily": {}, "devices": {}}
    async with _lock:
        _save_raw(_state)
    return {"ok": True, "message": "Usage data cleared"}
