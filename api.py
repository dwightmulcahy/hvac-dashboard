"""
HVAC Dashboard — Server-Side Automation API
Handles: device polling, schedules, max-temp guard, beeper sync, usage tracking
All automation runs 24/7 in the container regardless of browser state.
"""

import asyncio
import datetime
import json
import logging
import os
from typing import Optional, List

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hvac")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_background_worker())
    _add_log("HVAC API started", "info")
    yield

app = FastAPI(title="HVAC Automation API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATA_FILE = os.environ.get("DATA_FILE", "/data/hvac_state.json")
_lock = asyncio.Lock()

# ── Default state ─────────────────────────────────────────

DEFAULT_STATE = {
    "devices": [],          # list of device configs
    "schedules": [],        # list of schedule configs
    "settings": {
        "poll_interval": 60,
        "provider": "Coopeguanacaste",
        "exchange_rate": 530,
        "monthly_kwh": 400,
        "daily_hrs": 8,
        "tiered": True,
        "tiers": [
            {"up_to": 200,    "colones_per_kwh": 62},
            {"up_to": 500,    "colones_per_kwh": 82},
            {"up_to": 999999, "colones_per_kwh": 140},
        ],
        "flat_rate": 0.14,
    },
    "usage": {
        "daily": {},        # {"2026-07-01": {"host": {runtime_min, est_kwh, ...}}}
        "devices": {},      # {"host": {name, first_seen}}
    },
    "device_state": {},     # {"host": {last polled climate state + extras}}
    "logs": [],             # recent automation log entries
}

DEVICE_DEFAULTS = {
    "host": "",
    "name": "Unknown",
    "btu": 24000,
    "seer": 20,
    "max_temp": None,
    "beeper": "OFF",
    "_max_temp_active": False,
    "_last_poll": None,
    "_last_mode": None,
    "_last_poll_epoch": None,
    "_on_time_minutes": 0.0,
}

SCHEDULE_DEFAULTS = {
    "id": "",
    "device_host": "",
    "device_name": "",
    "time": "07:00",
    "days": [0,1,2,3,4,5,6],
    "power": None,
    "mode": None,
    "temp": None,
    "enabled": True,
    "last_run": None,
}

# ── State persistence ─────────────────────────────────────

def _load_raw() -> dict:
    if not os.path.exists(DATA_FILE):
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
        # ensure all top-level keys exist
        for k, v in DEFAULT_STATE.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data
    except Exception as e:
        log.error(f"Failed to load state: {e}")
        return json.loads(json.dumps(DEFAULT_STATE))

def _save_raw(data: dict):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_FILE)

# in-memory state (loaded once at startup, saved on every mutation)
_state: dict = _load_raw()

# ── Helpers ───────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()

def _today() -> str:
    return datetime.date.today().isoformat()

def _month() -> str:
    return datetime.date.today().strftime("%Y-%m")

def _ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")

def _effective_rate() -> float:
    s = _state["settings"]
    if not s.get("tiered", True):
        return s.get("flat_rate", 0.14)
    kwh = s.get("monthly_kwh", 400)
    xr = s.get("exchange_rate", 530)
    for tier in s.get("tiers", DEFAULT_STATE["settings"]["tiers"]):
        if kwh <= tier["up_to"]:
            return tier["colones_per_kwh"] / xr
    return s["tiers"][-1]["colones_per_kwh"] / xr

def _est_watts(device_state: dict, btu: int, seer: int) -> Optional[float]:
    s = device_state
    mode = s.get("mode", "OFF")
    if mode in ("OFF", "FAN_ONLY"):
        return 0.0
    indoor = s.get("current_temperature")
    target = s.get("target_temperature")
    outdoor = s.get("outdoor_temp")
    if indoor is None or target is None:
        return None
    try:
        indoor = float(indoor); target = float(target)
    except:
        return None
    eer = (seer or 20) * 0.875
    max_w = btu / eer
    delta = abs(indoor - target)
    penalty = max(0, (float(outdoor) - 25) / 20) if outdoor and mode != "HEAT" else 0
    load = min(1.0, 0.2 + (delta / 8) * 0.8 + penalty * 0.3)
    return round(max_w * load)

def _add_log(msg: str, level: str = "info"):
    entry = {"time": _ts(), "msg": msg, "level": level}
    log.info(f"[{level.upper()}] {msg}")
    _state["logs"].insert(0, entry)
    if len(_state["logs"]) > 200:
        _state["logs"] = _state["logs"][:200]

# ── AC communication ──────────────────────────────────────

ENTITY = "air_conditioner"

async def _fetch_state(host: str) -> Optional[dict]:
    url = f"http://{host}/climate/{ENTITY}"
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    return r.json()
        except Exception as e:
            if attempt == 2:
                log.warning(f"{host} fetch failed: {e}")
            await asyncio.sleep(0.8 * (attempt + 1))
    return None

async def _fetch_sensors(host: str) -> dict:
    paths = {
        "outdoor_temp": "sensor/air_conditioner_outdoor_temperature",
        "uptime_days":  "sensor/air_conditioner_uptime_days",
        "beeper":       "switch/air_conditioner_beeper",
    }
    out = {}
    async with httpx.AsyncClient(timeout=3) as client:
        for key, path in paths.items():
            for attempt in range(2):
                try:
                    r = await client.get(f"http://{host}/{path}")
                    if r.status_code == 200:
                        out[key] = r.json()
                    break
                except:
                    await asyncio.sleep(0.5)
    return out

async def _send_cmd(host: str, params: dict) -> bool:
    from urllib.parse import urlencode
    url = f"http://{host}/climate/{ENTITY}/set?{urlencode(params)}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(url)
            return r.status_code < 300
    except Exception as e:
        log.warning(f"{host} cmd failed: {e}")
        return False

async def _send_switch(host: str, path: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.post(f"http://{host}/{path}")
            return r.status_code < 300
    except:
        return False

# ── Poll a single device ──────────────────────────────────

async def _poll_device(device: dict):
    host = device["host"]
    name = device["name"]

    state = await _fetch_state(host)
    if state is None:
        _add_log(f"{name}: unreachable", "err")
        _state["device_state"][host] = {"error": "unreachable", "host": host}
        return

    sensors = await _fetch_sensors(host)

    # merge into device_state
    ds = {**state, "host": host}
    if "outdoor_temp" in sensors:
        ds["outdoor_temp"] = sensors["outdoor_temp"].get("value")
    if "uptime_days" in sensors:
        prev_uptime = _state["device_state"].get(host, {}).get("uptime_days")
        new_uptime = sensors["uptime_days"].get("value")
        if prev_uptime is not None and new_uptime is not None:
            try:
                if float(new_uptime) < float(prev_uptime) - 0.001:
                    _add_log(f"{name}: ⚠ dongle rebooted (uptime reset)", "warn")
            except:
                pass
        ds["uptime_days"] = new_uptime

    # beeper sync — push saved state if device disagrees
    saved_beeper = device.get("beeper", "OFF")
    if "beeper" in sensors:
        device_beeper = sensors["beeper"].get("value", "OFF")
        ds["beeper"] = saved_beeper
        if device_beeper != saved_beeper:
            endpoint = "turn_on" if saved_beeper == "ON" else "turn_off"
            ok = await _send_switch(host, f"switch/air_conditioner_beeper/{endpoint}")
            if ok:
                _add_log(f"{name}: beeper synced → {saved_beeper.lower()}", "info")

    # on-time tracking
    now_epoch = datetime.datetime.utcnow().timestamp()
    last_epoch = device.get("_last_poll_epoch")
    last_mode = device.get("_last_mode")
    if last_epoch and last_mode and last_mode != "OFF":
        elapsed_mins = (now_epoch - last_epoch) / 60
        device["_on_time_minutes"] = device.get("_on_time_minutes", 0) + elapsed_mins
        _record_usage(device, ds, elapsed_mins)

    # log mode changes
    cur_mode = state.get("mode", "OFF")
    if last_mode and last_mode != cur_mode:
        if cur_mode != "OFF":
            _add_log(f"{name}: turned on ({cur_mode})", "ok")
        else:
            _add_log(f"{name}: turned off", "info")

    device["_last_mode"] = cur_mode
    device["_last_poll_epoch"] = now_epoch
    device["_last_poll"] = _now_iso()
    _state["device_state"][host] = ds

    _add_log(f"{name}: {state.get('current_temperature')}°C in, {ds.get('outdoor_temp')}°C out, mode={cur_mode}", "ok")

# ── Usage recording ───────────────────────────────────────

def _record_usage(device: dict, ds: dict, interval_mins: float):
    host = device["host"]
    name = device["name"]
    btu = device.get("btu", 24000)
    seer = device.get("seer", 20)
    day = _today()
    usage = _state["usage"]

    if host not in usage["devices"]:
        usage["devices"][host] = {"name": name, "first_seen": _now_iso()}
    else:
        usage["devices"][host]["name"] = name

    if day not in usage["daily"]:
        usage["daily"][day] = {}
    if host not in usage["daily"][day]:
        usage["daily"][day][host] = {
            "runtime_minutes": 0.0, "est_kwh": 0.0,
            "peak_watts": 0.0, "snapshots": 0,
            "avg_indoor": [], "avg_outdoor": [],
        }

    bucket = usage["daily"][day][host]
    watts = _est_watts(ds, btu, seer) or 0
    kwh = (watts * interval_mins / 60) / 1000
    bucket["runtime_minutes"] += interval_mins
    bucket["est_kwh"] += kwh
    if watts > bucket["peak_watts"]:
        bucket["peak_watts"] = watts
    if ds.get("current_temperature") is not None:
        try: bucket["avg_indoor"].append(float(ds["current_temperature"]))
        except: pass
    if ds.get("outdoor_temp") is not None:
        try: bucket["avg_outdoor"].append(float(ds["outdoor_temp"]))
        except: pass
    bucket["snapshots"] += 1

# ── Max temp guard ────────────────────────────────────────

async def _check_max_temp(device: dict):
    max_temp = device.get("max_temp")
    if max_temp is None:
        return
    host = device["host"]
    name = device["name"]
    ds = _state["device_state"].get(host, {})
    if ds.get("error"):
        return
    try:
        indoor = float(ds.get("current_temperature", 0))
    except:
        return
    is_on = ds.get("mode", "OFF") != "OFF"
    active = device.get("_max_temp_active", False)

    if indoor >= max_temp and not is_on and not active:
        device["_max_temp_active"] = True
        _add_log(f"{name}: 🌡 {indoor}°C ≥ max {max_temp}°C — auto on", "warn")
        ok = await _send_cmd(host, {"mode": "COOL"})
        if ok:
            ds["mode"] = "COOL"
        else:
            device["_max_temp_active"] = False
    elif indoor < max_temp and active:
        device["_max_temp_active"] = False
        _add_log(f"{name}: 🌡 {indoor}°C < max {max_temp}°C — auto off", "ok")
        ok = await _send_cmd(host, {"mode": "OFF"})
        if ok:
            ds["mode"] = "OFF"

# ── Scheduler ─────────────────────────────────────────────

async def _check_schedules():
    now = datetime.datetime.now()
    hhmm = now.strftime("%H:%M")
    js_day = now.isoweekday() % 7  # Sun=0, Mon=1 ... Sat=6

    for sch in _state["schedules"]:
        if not sch.get("enabled", True):
            continue
        if sch.get("time") != hhmm:
            continue
        if js_day not in sch.get("days", []):
            continue
        # prevent double-fire
        last_run = sch.get("last_run", "")
        if last_run and last_run.startswith(hhmm[:5]):
            continue

        host = sch.get("device_host", "")
        device = next((d for d in _state["devices"] if d["host"] == host), None)
        if not device:
            _add_log(f"Schedule: device {host} not found", "warn")
            continue

        name = device["name"]
        _add_log(f"Schedule firing: {name} @ {hhmm}", "info")

        power = sch.get("power")
        mode = sch.get("mode")
        temp = sch.get("temp")

        if power == "off":
            await _send_cmd(host, {"mode": "OFF"})
            _add_log(f"{name}: scheduled off", "ok")
        else:
            if power == "on" and mode:
                await _send_cmd(host, {"mode": mode})
                _add_log(f"{name}: scheduled on → {mode}", "ok")
            elif mode:
                await _send_cmd(host, {"mode": mode})
                _add_log(f"{name}: scheduled mode → {mode}", "ok")
            if temp:
                await _send_cmd(host, {"target_temperature": temp})
                _add_log(f"{name}: scheduled temp → {temp}°C", "ok")

        sch["last_run"] = _ts()

# ── Main background worker ────────────────────────────────

async def _background_worker():
    _add_log("Background worker started", "info")
    last_schedule_check = ""

    while True:
        try:
            interval = _state["settings"].get("poll_interval", 60)

            # Poll all devices
            for device in _state["devices"]:
                await _poll_device(device)
                await _check_max_temp(device)

            # Check schedules (once per minute)
            hhmm = datetime.datetime.now().strftime("%H:%M")
            if hhmm != last_schedule_check:
                last_schedule_check = hhmm
                await _check_schedules()

            # Save state after each cycle
            async with _lock:
                _save_raw(_state)

        except Exception as e:
            log.error(f"Worker error: {e}", exc_info=True)

        await asyncio.sleep(interval)



# ═══════════════════════════════════════════════════════════
# REST API
# ═══════════════════════════════════════════════════════════

# ── Devices ───────────────────────────────────────────────

class DeviceConfig(BaseModel):
    host: str
    name: str
    btu: int = 24000
    seer: int = 20
    max_temp: Optional[float] = None
    beeper: str = "OFF"

@app.get("/devices")
async def get_devices():
    devices = []
    for d in _state["devices"]:
        ds = _state["device_state"].get(d["host"], {})
        devices.append({**d, "state": ds})
    return {"devices": devices}

@app.post("/devices")
async def add_device(cfg: DeviceConfig):
    existing = next((d for d in _state["devices"] if d["host"] == cfg.host), None)
    if existing:
        existing.update(cfg.dict())
    else:
        _state["devices"].append({**DEVICE_DEFAULTS, **cfg.dict()})
    async with _lock:
        _save_raw(_state)
    return {"ok": True}

@app.put("/devices/{host:path}")
async def update_device(host: str, cfg: DeviceConfig):
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    if not device:
        return {"ok": False, "error": "not found"}
    device.update(cfg.dict())
    async with _lock:
        _save_raw(_state)
    return {"ok": True}

@app.delete("/devices/{host:path}")
async def delete_device(host: str):
    _state["devices"] = [d for d in _state["devices"] if d["host"] != host]
    async with _lock:
        _save_raw(_state)
    return {"ok": True}

# ── Device control ────────────────────────────────────────

class CommandPayload(BaseModel):
    params: dict

@app.post("/devices/{host:path}/cmd")
async def send_device_cmd(host: str, payload: CommandPayload):
    ok = await _send_cmd(host, payload.params)
    if ok:
        ds = _state["device_state"].get(host, {})
        ds.update(payload.params)
    return {"ok": ok}

@app.post("/devices/{host:path}/beeper/{state}")
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

# ── Schedules ─────────────────────────────────────────────

class ScheduleConfig(BaseModel):
    id: Optional[str] = None
    device_host: str
    device_name: str
    time: str
    days: List[int]
    power: Optional[str] = None
    mode: Optional[str] = None
    temp: Optional[float] = None
    enabled: bool = True

@app.get("/schedules")
async def get_schedules():
    return {"schedules": _state["schedules"]}

@app.post("/schedules")
async def add_schedule(cfg: ScheduleConfig):
    import uuid
    sch = {**SCHEDULE_DEFAULTS, **cfg.dict()}
    sch["id"] = cfg.id or str(uuid.uuid4())[:8]
    _state["schedules"].append(sch)
    async with _lock:
        _save_raw(_state)
    return {"ok": True, "id": sch["id"]}

@app.put("/schedules/{sch_id}")
async def update_schedule(sch_id: str, cfg: ScheduleConfig):
    sch = next((s for s in _state["schedules"] if s["id"] == sch_id), None)
    if not sch:
        return {"ok": False, "error": "not found"}
    sch.update(cfg.dict())
    sch["id"] = sch_id
    async with _lock:
        _save_raw(_state)
    return {"ok": True}

@app.delete("/schedules/{sch_id}")
async def delete_schedule(sch_id: str):
    _state["schedules"] = [s for s in _state["schedules"] if s["id"] != sch_id]
    async with _lock:
        _save_raw(_state)
    return {"ok": True}

@app.post("/schedules/{sch_id}/toggle")
async def toggle_schedule(sch_id: str):
    sch = next((s for s in _state["schedules"] if s["id"] == sch_id), None)
    if not sch:
        return {"ok": False}
    sch["enabled"] = not sch.get("enabled", True)
    async with _lock:
        _save_raw(_state)
    return {"ok": True, "enabled": sch["enabled"]}

# ── Settings ──────────────────────────────────────────────

@app.get("/settings")
async def get_settings():
    return _state["settings"]

@app.put("/settings")
async def update_settings(settings: dict):
    _state["settings"].update(settings)
    async with _lock:
        _save_raw(_state)
    return {"ok": True}

# ── Usage / reporting ─────────────────────────────────────

@app.get("/usage/summary")
async def usage_summary(month: Optional[str] = None):
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

@app.get("/usage/rolling30")
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

# ── Logs ──────────────────────────────────────────────────

@app.get("/logs")
async def get_logs(level: Optional[str] = None, limit: int = 100):
    logs = _state["logs"]
    level_order = {"err": 3, "warn": 2, "ok": 1, "info": 0}
    if level and level.endswith("+"):
        min_level = level_order.get(level[:-1], 0)
        logs = [l for l in logs if level_order.get(l["level"], 0) >= min_level]
    elif level and level != "all":
        logs = [l for l in logs if l["level"] == level]
    return {"logs": logs[:limit]}

# ── System ────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "ok", "service": "HVAC Automation API", "devices": len(_state["devices"]), "schedules": len(_state["schedules"])}

@app.delete("/reset")
async def reset():
    _state["usage"] = {"daily": {}, "devices": {}}
    async with _lock:
        _save_raw(_state)
    return {"ok": True, "message": "Usage data cleared"}
