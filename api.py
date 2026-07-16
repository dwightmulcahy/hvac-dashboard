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
from fastapi import FastAPI, UploadFile
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
        "max_temp_guard_start": 8,
        "max_temp_guard_end": 22,
    },
    "usage": {
        "daily": {},        # {"2026-07-01": {"host": {runtime_min, est_kwh, ...}}}
        "devices": {},      # {"host": {name, first_seen}}
        "temp_history": {}, # {"host": [{"t": iso_hour, "in": float, "out": float}, ...]}
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
    "watchdog_minutes": 5,
    "lock_temp": False,
    "locked_target_temp": None,
    "_max_temp_active": False,
    "_last_poll": None,
    "_last_seen": None,
    "_stale": False,
    "_last_mode": None,
    "_last_poll_epoch": None,
    "_on_time_minutes": 0.0,
    "_retry_queue": [],
}

SCHEDULE_DEFAULTS = {
    "id": "",
    "device_host": "",
    "device_name": "",
    "time": "07:00",
    "end_time": None,
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
    # wifi signal sensor name varies by firmware version — try both
    wifi_paths = [
        "sensor/air_conditioner_wi-fi_signal",
        "sensor/air_conditioner_wi_fi_signal",
        "sensor/wi-fi_signal",
        "sensor/wifi_signal",
    ]
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
        # try wifi paths until one works
        for wp in wifi_paths:
            try:
                r = await client.get(f"http://{host}/{wp}")
                if r.status_code == 200:
                    out["wifi_signal"] = r.json()
                    break
            except:
                pass
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
        prev_stale = device.get("_stale", False)
        device["_stale"] = True
        if not prev_stale:
            _add_log(f"{name}: 🔴 went offline", "err")
        _state["device_state"][host] = {"error": "unreachable", "host": host,
                                         "last_seen": device.get("_last_seen")}
        return

    # ── watchdog: mark recovered if was stale ─────────────
    if device.get("_stale"):
        device["_stale"] = False
        _add_log(f"{name}: 🟢 back online", "ok")

    device["_last_seen"] = _now_iso()
    device["_stale"] = False

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
    if "wifi_signal" in sensors:
        raw = sensors["wifi_signal"].get("value")
        # ESPHome returns value as number (-43) or string ("-43 dBm")
        if raw is not None:
            try:
                ds["wifi_signal"] = float(str(raw).split()[0])
            except Exception:
                ds["wifi_signal"] = raw
        log.debug(f"{name}: wifi={ds.get('wifi_signal')}dBm")

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
    ds["last_seen"] = device["_last_seen"]
    ds["stale"] = False
    _state["device_state"][host] = ds

    _add_log(f"{name}: {state.get('current_temperature')}°C in, {ds.get('outdoor_temp')}°C out, mode={cur_mode}", "ok")

    # ── temperature history (hourly) ──────────────────────
    _record_temp_history(host, ds)

    # ── override protection ───────────────────────────────
    if device.get("lock_temp") and device.get("locked_target_temp") is not None:
        if cur_mode not in ("OFF", "FAN_ONLY"):
            reported = state.get("target_temperature")
            locked = device["locked_target_temp"]
            try:
                if reported is not None and abs(float(reported) - float(locked)) >= 0.5:
                    _add_log(f"{name}: 🔒 remote override detected ({reported}°C → locking back to {locked}°C)", "warn")
                    ok = await _send_cmd(host, {"target_temperature": locked})
                    if ok:
                        ds["target_temperature"] = str(locked)
                        _add_log(f"{name}: 🔒 temp restored to {locked}°C", "ok")
            except Exception as e:
                _add_log(f"{name}: lock restore failed — {e}", "err")

    # ── drain retry queue ─────────────────────────────────
    queue = device.get("_retry_queue", [])
    if queue:
        retry = queue.pop(0)
        device["_retry_queue"] = queue
        _add_log(f"{name}: retrying queued command {retry}", "info")
        await _send_cmd(host, retry)

# ── Temperature history ───────────────────────────────────

def _record_temp_history(host: str, ds: dict):
    """Store one reading per hour per device, keep 48 hours."""
    indoor = ds.get("current_temperature")
    outdoor = ds.get("outdoor_temp")
    if indoor is None:
        return
    try:
        indoor = round(float(indoor), 1)
        outdoor = round(float(outdoor), 1) if outdoor is not None else None
    except Exception:
        return

    hour_key = datetime.datetime.now().strftime("%Y-%m-%dT%H:00")
    if "temp_history" not in _state["usage"]:
        _state["usage"]["temp_history"] = {}

    history = _state["usage"]["temp_history"].setdefault(host, [])
    # update existing entry for this hour or append
    for entry in history:
        if entry["t"] == hour_key:
            entry["in"] = indoor
            if outdoor is not None:
                entry["out"] = outdoor
            return
    history.append({"t": hour_key, "in": indoor, "out": outdoor})
    # keep only last 48 hours
    if len(history) > 48:
        _state["usage"]["temp_history"][host] = history[-48:]


@app.get("/devices/{host:path}/temp-history")
async def get_temp_history(host: str):
    history = _state["usage"].get("temp_history", {}).get(host, [])
    return {"host": host, "history": history}


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

# ── Watchdog ──────────────────────────────────────────────

def _check_watchdog(device: dict):
    """Mark device stale if last_seen exceeds watchdog_minutes threshold."""
    last_seen = device.get("_last_seen")
    if not last_seen:
        return
    threshold = device.get("watchdog_minutes", 5)
    try:
        last_dt = datetime.datetime.fromisoformat(last_seen)
        elapsed = (datetime.datetime.utcnow() - last_dt).total_seconds() / 60
        was_stale = device.get("_stale", False)
        if elapsed > threshold and not was_stale:
            device["_stale"] = True
            ds = _state["device_state"].get(device["host"], {})
            ds["stale"] = True
            _add_log(f"{device['name']}: ⚠ no response for {int(elapsed)}m (watchdog: {threshold}m)", "warn")
    except Exception:
        pass




async def _check_max_temp(device: dict):
    max_temp = device.get("max_temp")
    if max_temp is None:
        return
    # check guard hours
    now_hour = datetime.datetime.now().hour
    guard_start = _state["settings"].get("max_temp_guard_start", 8)
    guard_end = _state["settings"].get("max_temp_guard_end", 22)
    if not (guard_start <= now_hour < guard_end):
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
    cur_mode = ds.get("mode", "OFF")
    # consider unit "cooling" only if it's in COOL or AUTO mode
    is_cooling = cur_mode in ("COOL", "AUTO", "HEAT_COOL")
    active = device.get("_max_temp_active", False)

    if indoor >= max_temp and not is_cooling and not active:
        device["_max_temp_active"] = True
        # save current state so we can restore it after cooling
        device["_pre_autocool_mode"] = cur_mode
        device["_pre_autocool_temp"] = ds.get("target_temperature")
        # target temp: 2°C below max, clamped to device min
        try:
            target = max(float(ds.get("min_temp", 17)), max_temp - 2)
        except:
            target = max_temp - 2
        _add_log(f"{name}: 🌡 {indoor}°C ≥ max {max_temp}°C — auto cool to {target}°C (was {cur_mode})", "warn")
        ok1 = await _send_cmd(host, {"mode": "COOL"})
        ok2 = await _send_cmd(host, {"target_temperature": target})
        if ok1:
            ds["mode"] = "COOL"
            ds["target_temperature"] = str(target)
        if not ok1 and not ok2:
            device["_max_temp_active"] = False
    elif indoor < max_temp and active:
        device["_max_temp_active"] = False
        prev_mode = device.pop("_pre_autocool_mode", "OFF")
        prev_temp = device.pop("_pre_autocool_temp", None)
        if prev_mode in ("OFF", "FAN_ONLY", None):
            # was off or fan — just turn off
            _add_log(f"{name}: 🌡 {indoor}°C < max {max_temp}°C — auto off (restoring OFF)", "ok")
            ok = await _send_cmd(host, {"mode": "OFF"})
            if ok:
                ds["mode"] = "OFF"
        else:
            # restore previous mode and temp
            _add_log(f"{name}: 🌡 {indoor}°C < max {max_temp}°C — restoring {prev_mode}" +
                     (f" @ {prev_temp}°C" if prev_temp else ""), "ok")
            ok1 = await _send_cmd(host, {"mode": prev_mode})
            if ok1:
                ds["mode"] = prev_mode
            if prev_temp:
                ok2 = await _send_cmd(host, {"target_temperature": float(prev_temp)})
                if ok2:
                    ds["target_temperature"] = str(prev_temp)

# ── Scheduler ─────────────────────────────────────────────

async def _check_schedules():
    now = datetime.datetime.now()
    hhmm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    js_day = now.isoweekday() % 7  # Sun=0, Mon=1 ... Sat=6

    for sch in _state["schedules"]:
        if not sch.get("enabled", True):
            continue
        if sch.get("time") != hhmm:
            continue
        if js_day not in sch.get("days", []):
            continue
        # prevent double-fire within the same day
        last_run = sch.get("last_run", "")
        if last_run and last_run.startswith(today):
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

        # store date+time so the same schedule can fire again tomorrow
        sch["last_run"] = f"{today} {_ts()}"

    # ── Check schedule end times ──────────────────────────────
    for sch in _state["schedules"]:
        end_time = sch.get("end_time")
        if not end_time or not sch.get("enabled", True):
            continue
        if end_time != hhmm:
            continue
        if js_day not in sch.get("days", []):
            continue
        last_end_run = sch.get("_last_end_run", "")
        if last_end_run and last_end_run.startswith(today):
            continue
        host = sch.get("device_host", "")
        device = next((d for d in _state["devices"] if d["host"] == host), None)
        if not device:
            continue
        name = device["name"]
        _add_log(f"Schedule end: {name} @ {hhmm} — auto off", "info")
        await _send_cmd(host, {"mode": "OFF"})
        sch["_last_end_run"] = f"{today} {_ts()}"

# ── Main background worker ────────────────────────────────

async def _background_worker():
    _add_log("Background worker started", "info")
    last_schedule_check = ""
    last_rate_update = ""

    # ── Startup retry with exponential backoff ────────────────
    # Wait for network before first real poll
    for attempt in range(8):
        if not _state["devices"]:
            break
        try:
            first = _state["devices"][0]
            async with httpx.AsyncClient(timeout=4) as client:
                r = await client.get(f"http://{first['host']}/climate/air_conditioner")
                if r.status_code < 500:
                    _add_log("Network ready", "ok")
                    break
        except Exception:
            delay = min(2 ** attempt, 60)
            _add_log(f"Network not ready — retrying in {delay}s (attempt {attempt+1})", "warn")
            await asyncio.sleep(delay)

    while True:
        try:
            interval = _state["settings"].get("poll_interval", 60)

            # Poll all devices
            for device in _state["devices"]:
                await _poll_device(device)
                await _check_max_temp(device)
                _check_watchdog(device)

            # Check schedules (once per minute)
            hhmm = datetime.datetime.now().strftime("%H:%M")
            if hhmm != last_schedule_check:
                last_schedule_check = hhmm
                await _check_schedules()

            # Auto-update exchange rate daily at 06:00
            today = datetime.date.today().isoformat()
            if hhmm == "06:00" and today != last_rate_update:
                last_rate_update = today
                await _fetch_exchange_rate()

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
    watchdog_minutes: int = 5
    lock_temp: bool = False
    locked_target_temp: Optional[float] = None

@app.post("/devices/reorder")
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
        # check for duplicates before adding
        _state["devices"].append({**DEVICE_DEFAULTS, **cfg.dict()})
    async with _lock:
        _save_raw(_state)
    return {"ok": True}

@app.put("/devices/{host:path}")
async def update_device(host: str, cfg: DeviceConfig):
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    if not device:
        # host not found — check if new host already exists (avoid duplicate)
        if not any(d["host"] == cfg.host for d in _state["devices"]):
            _state["devices"].append({**DEVICE_DEFAULTS, **cfg.dict()})
    else:
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
    else:
        # queue for retry when device comes back online
        device = next((d for d in _state["devices"] if d["host"] == host), None)
        if device is not None:
            if "_retry_queue" not in device:
                device["_retry_queue"] = []
            device["_retry_queue"].append(payload.params)
            _add_log(f"{device['name']}: command queued for retry {payload.params}", "warn")
    return {"ok": ok, "queued": not ok}

@app.post("/devices/{host:path}/lock")
async def set_lock_temp(host: str, data: dict):
    """Enable/disable temp lock. body: {lock: bool, target_temp: float|null}"""
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    if not device:
        return {"ok": False, "error": "not found"}
    device["lock_temp"] = data.get("lock", False)
    if data.get("target_temp") is not None:
        device["locked_target_temp"] = float(data["target_temp"])
    elif not device["lock_temp"]:
        device["locked_target_temp"] = None
    async with _lock:
        _save_raw(_state)
    status = "locked" if device["lock_temp"] else "unlocked"
    _add_log(f"{device['name']}: temp {status} at {device.get('locked_target_temp')}°C", "info")
    return {"ok": True, "lock_temp": device["lock_temp"], "locked_target_temp": device.get("locked_target_temp")}

@app.post("/devices/{host:path}/beeper/test")
async def test_beeper(host: str):
    """Send a quick beep without changing the saved beeper state."""
    ok_on = await _send_switch(host, "switch/air_conditioner_beeper/turn_on")
    await asyncio.sleep(0.5)
    ok_off = await _send_switch(host, "switch/air_conditioner_beeper/turn_off")
    # restore to saved state
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    if device:
        saved = device.get("beeper", "OFF")
        endpoint = "turn_on" if saved == "ON" else "turn_off"
        await _send_switch(host, f"switch/air_conditioner_beeper/{endpoint}")
    return {"ok": ok_on and ok_off}

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
    end_time: Optional[str] = None
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

@app.get("/usage/export-csv")
async def export_csv(month: Optional[str] = None):
    """Export monthly usage as CSV."""
    from fastapi.responses import StreamingResponse
    import io
    import csv
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

async def _fetch_exchange_rate() -> Optional[dict]:
    """Fetch live USD/CRC rate from frankfurter.app (ECB data, free, no key)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.frankfurter.app/latest?from=USD&to=CRC")
            if r.status_code == 200:
                data = r.json()
                rate = round(data["rates"]["CRC"])
                date = data.get("date", datetime.date.today().isoformat())
                _state["settings"]["exchangeRate"] = rate
                _state["settings"]["_rate_date"] = date
                async with _lock:
                    _save_raw(_state)
                _add_log(f"Exchange rate updated: ₡{rate}/USD as of {date}", "info")
                return {"rate": rate, "date": date}
    except Exception as e:
        _add_log(f"Exchange rate fetch failed: {e}", "warn")
    return None


@app.get("/exchange-rate")
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


@app.get("/health/push")
async def health_push():
    """
    Uptime Kuma push-compatible endpoint.
    Returns 200 with status=ok when all devices are healthy,
    503 when any device is stale.
    Add to Uptime Kuma as HTTP(s) monitor pointing to /api/health/push
    """
    from fastapi.responses import JSONResponse
    now = datetime.datetime.utcnow()
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


@app.post("/devices/{host:path}/ota-upload")
async def ota_upload(host: str, firmware: UploadFile):
    """Flash .bin firmware to device via ESPHome HTTP OTA."""
    from fastapi.responses import JSONResponse
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    name = device["name"] if device else host
    try:
        data = await firmware.read()
        _add_log(f"{name}: OTA upload started ({len(data)//1024}KB)", "info")
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"http://{host}/update",
                content=data,
                headers={"Content-Type": "application/octet-stream"},
            )
            if r.status_code < 300:
                _add_log(f"{name}: ✓ OTA complete — device rebooting", "ok")
                return {"ok": True, "message": "Firmware uploaded, device rebooting"}
            else:
                _add_log(f"{name}: OTA failed — HTTP {r.status_code}", "err")
                return JSONResponse(status_code=500,
                    content={"ok": False, "error": f"Device returned HTTP {r.status_code}"})
    except Exception as e:
        _add_log(f"{name}: OTA error — {e}", "err")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

@app.get("/health")
async def health():
    """Detailed health check — per-device status and staleness."""
    now = datetime.datetime.utcnow()
    device_health = []
    for d in _state["devices"]:
        last_seen = d.get("_last_seen")
        elapsed = None
        if last_seen:
            try:
                elapsed = round((now - datetime.datetime.fromisoformat(last_seen)).total_seconds() / 60, 1)
            except Exception:
                pass
        device_health.append({
            "host": d["host"],
            "name": d["name"],
            "stale": d.get("_stale", False),
            "last_seen": last_seen,
            "minutes_since_seen": elapsed,
            "watchdog_minutes": d.get("watchdog_minutes", 5),
            "retry_queue_depth": len(d.get("_retry_queue", [])),
        })
    all_ok = all(not d["stale"] for d in device_health) if device_health else False
    return {
        "status": "ok" if all_ok else "degraded",
        "devices": device_health,
        "total": len(_state["devices"]),
        "stale": sum(1 for d in device_health if d["stale"]),
        "timestamp": now.isoformat(),
    }

@app.get("/backup")
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

@app.post("/restore")
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

@app.get("/")
async def root():
    return {"status": "ok", "service": "HVAC Automation API",
            "version": os.environ.get("APP_VERSION", "dev"),
            "build": os.environ.get("BUILD_DATE", ""),
            "devices": len(_state["devices"]), "schedules": len(_state["schedules"])}

@app.delete("/reset")
async def reset():
    _state["usage"] = {"daily": {}, "devices": {}}
    async with _lock:
        _save_raw(_state)
    return {"ok": True, "message": "Usage data cleared"}
