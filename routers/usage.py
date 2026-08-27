"""Usage reporting (monthly summary, rolling 30-day, CSV export) and logs."""

import csv
import datetime
import io

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from state import _clear_log_file, _lock, _month, _save_raw, _state

router = APIRouter(tags=["usage"])


@router.get("/usage/summary")
async def usage_summary(month: str | None = None):
    target = month or _month()
    usage = _state["usage"]
    result = {}
    for day, devices in usage["daily"].items():
        if not day.startswith(target):
            continue
        for host, bucket in devices.items():
            name = usage["devices"].get(host, {}).get("name", host)
            if host not in result:
                result[host] = {"name": name, "host": host,
                    "runtime_hours": 0.0, "est_kwh": 0.0,
                    "peak_watts": 0.0, "days_active": 0,
                    "avg_indoor": [], "avg_outdoor": []}
            r = result[host]
            r["runtime_hours"] += bucket["runtime_minutes"] / 60
            r["est_kwh"] += bucket["est_kwh"]
            if bucket["peak_watts"] > r["peak_watts"]:
                r["peak_watts"] = bucket["peak_watts"]
            if bucket["runtime_minutes"] > 0:
                r["days_active"] += 1
            r["avg_indoor"] += bucket.get("avg_indoor", [])
            r["avg_outdoor"] += bucket.get("avg_outdoor", [])
    for r in result.values():
        r["avg_indoor_c"] = round(sum(r["avg_indoor"]) / len(r["avg_indoor"]), 1) if r["avg_indoor"] else None
        r["avg_outdoor_c"] = round(sum(r["avg_outdoor"]) / len(r["avg_outdoor"]), 1) if r["avg_outdoor"] else None
        del r["avg_indoor"], r["avg_outdoor"]
        r["runtime_hours"] = round(r["runtime_hours"], 2)
        r["est_kwh"] = round(r["est_kwh"], 3)
    return {"month": target, "devices": list(result.values())}


@router.get("/usage/rolling30")
async def usage_rolling30():
    today = datetime.date.today()
    days_back = [(today - datetime.timedelta(days=i)).isoformat() for i in range(30)]
    usage = _state["usage"]
    result = {}
    daily_series = {}
    for day in days_back:
        if day not in usage["daily"]:
            continue
        for host, bucket in usage["daily"][day].items():
            name = usage["devices"].get(host, {}).get("name", host)
            if host not in result:
                result[host] = {"name": name, "host": host,
                    "runtime_hours": 0.0, "est_kwh": 0.0,
                    "peak_watts": 0.0, "days_active": 0}
                daily_series[host] = []
            r = result[host]
            day_kwh = bucket["est_kwh"]
            day_h = bucket["runtime_minutes"] / 60
            r["runtime_hours"] += day_h
            r["est_kwh"] += day_kwh
            if bucket["peak_watts"] > r["peak_watts"]:
                r["peak_watts"] = bucket["peak_watts"]
            if bucket["runtime_minutes"] > 0:
                r["days_active"] += 1
            daily_series[host].append({"date": day, "kwh": round(day_kwh, 3), "runtime_h": round(day_h, 2)})
    for host, r in result.items():
        r["runtime_hours"] = round(r["runtime_hours"], 2)
        r["est_kwh"] = round(r["est_kwh"], 3)
        r["daily"] = sorted(daily_series[host], key=lambda x: x["date"])
    return {"days": 30, "from": days_back[-1], "to": days_back[0], "devices": list(result.values())}


@router.get("/usage/export-csv")
async def export_csv(month: str | None = None):
    """Export monthly usage as CSV."""
    target = month or _month()
    usage = _state["usage"]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Device", "Host", "Runtime (hrs)", "Est kWh", "Peak W", "Snapshots"])
    for day in sorted(usage["daily"].keys()):
        if not day.startswith(target):
            continue
        for host, bucket in usage["daily"][day].items():
            name = usage["devices"].get(host, {}).get("name", host)
            writer.writerow([
                day, name, host,
                round(bucket["runtime_minutes"] / 60, 2),
                round(bucket["est_kwh"], 3),
                round(bucket["peak_watts"], 0),
                bucket["snapshots"],
            ])
    output.seek(0)
    filename = f"hvac-usage-{target}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.delete("/logs")
async def clear_logs():
    _state["logs"] = []
    _clear_log_file()
    async with _lock:
        _save_raw(_state)
    return {"ok": True}


@router.get("/logs")
async def get_logs(level: str | None = None, limit: int = 100):
    logs = _state["logs"]
    level_order = {"err": 3, "warn": 2, "ok": 1, "info": 0}
    if level and level.endswith("+"):
        min_level = level_order.get(level[:-1], 0)
        logs = [entry for entry in logs if level_order.get(entry["level"], 0) >= min_level]
    elif level and level != "all":
        logs = [entry for entry in logs if entry["level"] == level]
    return {"logs": logs[:limit]}
