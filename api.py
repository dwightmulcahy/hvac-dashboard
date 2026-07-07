"""
HVAC Dashboard — Usage Persistence API
Endpoints:
  POST /record        — record a usage snapshot from the dashboard
  GET  /summary       — monthly summary per device
  GET  /rolling30     — rolling 30-day per device
  GET  /history       — raw daily records (optional ?device=&days=)
  DELETE /reset       — clear all data (admin)
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import json, os, datetime, threading

app = FastAPI(title="HVAC Usage API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_FILE = os.environ.get("DATA_FILE", "/data/hvac_usage.json")
_lock = threading.Lock()


# ── Data model ────────────────────────────────────────────

def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"devices": {}, "daily": {}}
    with open(DATA_FILE) as f:
        return json.load(f)

def save_data(data: dict):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def today_key() -> str:
    return datetime.date.today().isoformat()

def month_key() -> str:
    return datetime.date.today().strftime("%Y-%m")


# ── Schemas ───────────────────────────────────────────────

class UsageSnapshot(BaseModel):
    host: str
    name: str
    mode: str                    # OFF, COOL, HEAT, etc.
    indoor_temp: Optional[float] = None
    outdoor_temp: Optional[float] = None
    target_temp: Optional[float] = None
    est_watts: Optional[float] = None
    on_time_minutes: Optional[float] = None   # cumulative from dashboard
    interval_minutes: Optional[float] = None  # minutes since last snapshot
    timestamp: Optional[str] = None           # ISO8601, filled server-side if absent


# ── Helpers ───────────────────────────────────────────────

def kwh_from_watts_minutes(watts: float, minutes: float) -> float:
    return (watts * minutes / 60) / 1000


# ── Endpoints ─────────────────────────────────────────────

@app.post("/record")
def record(snap: UsageSnapshot):
    with _lock:
        data = load_data()
        now = datetime.datetime.utcnow().isoformat()
        day = today_key()

        # initialise device registry
        if snap.host not in data["devices"]:
            data["devices"][snap.host] = {"name": snap.name, "first_seen": now}
        else:
            data["devices"][snap.host]["name"] = snap.name  # update if renamed

        # daily bucket: data["daily"][day][host]
        if day not in data["daily"]:
            data["daily"][day] = {}
        if snap.host not in data["daily"][day]:
            data["daily"][day][snap.host] = {
                "runtime_minutes": 0.0,
                "est_kwh": 0.0,
                "snapshots": 0,
                "peak_watts": 0.0,
                "avg_indoor": [],
                "avg_outdoor": [],
            }

        bucket = data["daily"][day][snap.host]

        # accumulate only when AC is ON and we have interval data
        is_on = snap.mode not in ("OFF", "off", "")
        interval = snap.interval_minutes or 0.0

        if is_on and interval > 0:
            watts = snap.est_watts or 0.0
            kwh = kwh_from_watts_minutes(watts, interval)
            bucket["runtime_minutes"] += interval
            bucket["est_kwh"] += kwh
            if watts > bucket["peak_watts"]:
                bucket["peak_watts"] = watts

        if snap.indoor_temp is not None:
            bucket["avg_indoor"].append(snap.indoor_temp)
        if snap.outdoor_temp is not None:
            bucket["avg_outdoor"].append(snap.outdoor_temp)
        bucket["snapshots"] += 1

        save_data(data)
    return {"ok": True, "day": day}


@app.get("/summary")
def monthly_summary(month: Optional[str] = None):
    """Return per-device totals for a given month (default: current)."""
    target_month = month or month_key()
    with _lock:
        data = load_data()

    result = {}
    for day, devices in data["daily"].items():
        if not day.startswith(target_month):
            continue
        for host, bucket in devices.items():
            name = data["devices"].get(host, {}).get("name", host)
            if host not in result:
                result[host] = {
                    "name": name,
                    "host": host,
                    "runtime_hours": 0.0,
                    "est_kwh": 0.0,
                    "peak_watts": 0.0,
                    "days_active": 0,
                    "avg_indoor": [],
                    "avg_outdoor": [],
                }
            r = result[host]
            r["runtime_hours"] += bucket["runtime_minutes"] / 60
            r["est_kwh"] += bucket["est_kwh"]
            if bucket["peak_watts"] > r["peak_watts"]:
                r["peak_watts"] = bucket["peak_watts"]
            if bucket["runtime_minutes"] > 0:
                r["days_active"] += 1
            r["avg_indoor"] += bucket["avg_indoor"]
            r["avg_outdoor"] += bucket["avg_outdoor"]

    # finalise averages
    for r in result.values():
        r["avg_indoor_c"] = round(sum(r["avg_indoor"]) / len(r["avg_indoor"]), 1) if r["avg_indoor"] else None
        r["avg_outdoor_c"] = round(sum(r["avg_outdoor"]) / len(r["avg_outdoor"]), 1) if r["avg_outdoor"] else None
        del r["avg_indoor"], r["avg_outdoor"]
        r["runtime_hours"] = round(r["runtime_hours"], 2)
        r["est_kwh"] = round(r["est_kwh"], 3)
        r["peak_watts"] = round(r["peak_watts"], 0)

    return {"month": target_month, "devices": list(result.values())}


@app.get("/rolling30")
def rolling_30():
    """Return per-device totals for the last 30 calendar days."""
    today = datetime.date.today()
    days_back = [(today - datetime.timedelta(days=i)).isoformat() for i in range(30)]

    with _lock:
        data = load_data()

    result = {}
    daily_series = {}  # host -> [{date, kwh, runtime_h}]

    for day in days_back:
        if day not in data["daily"]:
            continue
        for host, bucket in data["daily"][day].items():
            name = data["devices"].get(host, {}).get("name", host)
            if host not in result:
                result[host] = {
                    "name": name, "host": host,
                    "runtime_hours": 0.0, "est_kwh": 0.0,
                    "peak_watts": 0.0, "days_active": 0,
                }
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
        r["peak_watts"] = round(r["peak_watts"], 0)
        r["daily"] = sorted(daily_series[host], key=lambda x: x["date"])

    return {"days": 30, "from": days_back[-1], "to": days_back[0], "devices": list(result.values())}


@app.get("/history")
def history(device: Optional[str] = None, days: int = 30):
    """Raw daily records, optionally filtered by device host."""
    today = datetime.date.today()
    cutoff = (today - datetime.timedelta(days=days)).isoformat()

    with _lock:
        data = load_data()

    out = []
    for day, devices in sorted(data["daily"].items(), reverse=True):
        if day < cutoff:
            continue
        for host, bucket in devices.items():
            if device and host != device:
                continue
            out.append({
                "date": day,
                "host": host,
                "name": data["devices"].get(host, {}).get("name", host),
                "runtime_minutes": round(bucket["runtime_minutes"], 1),
                "est_kwh": round(bucket["est_kwh"], 3),
                "peak_watts": bucket["peak_watts"],
                "snapshots": bucket["snapshots"],
            })
    return {"records": out}


@app.delete("/reset")
def reset():
    with _lock:
        save_data({"devices": {}, "daily": {}})
    return {"ok": True, "message": "All usage data cleared"}


@app.get("/")
def root():
    return {"status": "ok", "service": "HVAC Usage API"}
