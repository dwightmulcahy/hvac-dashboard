"""
HVAC Dashboard — Server-Side Automation API
Handles: device polling, schedules, max-temp guard, beeper sync, usage tracking
All automation runs 24/7 in the container regardless of browser state.
"""

import asyncio
import datetime
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import signal
from typing import Optional, List

import httpx
from fastapi import FastAPI, UploadFile, HTTPException, Header
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hvac")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # load persisted logs from disk
    persisted = _load_log_file()
    if persisted:
        _state["logs"] = persisted
        log.info(f"Loaded {len(persisted)} log entries from disk")

    # ensure default admin user exists
    _ensure_default_admin()

    # generate a one-time recovery key and print to logs
    # use this if you forget your password: POST /api/auth/recover
    _state["_recovery_key"] = secrets.token_urlsafe(24)
    log.warning(f"=== RECOVERY KEY: {_state['_recovery_key']} ===")
    log.warning("=== Use POST /api/auth/recover with this key to reset admin password ===")

    # register SIGTERM handler to log clean shutdown
    def _on_sigterm(*_):
        _add_log("HVAC API stopping (SIGTERM)", "warn")
        _save_raw(_state)

    signal.signal(signal.SIGTERM, _on_sigterm)
    asyncio.create_task(_background_worker())
    _add_log("HVAC API started", "info")
    yield
    # shutdown
    _add_log("HVAC API stopped", "warn")
    async with _lock:
        _save_raw(_state)

app = FastAPI(title="HVAC Automation API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def auth_middleware(request, call_next):
    """Enforce auth based on method and path."""
    path = request.url.path
    method = request.method

    # always allow: auth endpoints, health, root status, GET reads
    open_paths = {"/api/", "/", "/health", "/health/push", "/exchange-rate"}
    if path in open_paths or path.startswith("/auth/"):
        return await call_next(request)
    # if no users configured yet, allow everything (first run)
    if not _state.get("users"):
        return await call_next(request)

    authorization = request.headers.get("Authorization")
    info = _get_token_info(authorization)

    if info is None:
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})

    role = info.get("role", "viewer")

    # viewer: GET only (except auth endpoints)
    if method == "GET":
        return await call_next(request)

    # operator: can send commands, toggle beeper, poll
    operator_patterns = ["/cmd", "/beeper/", "/poll", "/display-toggle", "/vacation/"]
    if method == "POST" and any(p in path for p in operator_patterns):
        if ROLE_WEIGHTS.get(role, 0) >= ROLE_WEIGHTS["operator"]:
            return await call_next(request)
        return JSONResponse(status_code=403, content={"detail": "Operator role required"})

    # everything else (settings, devices, schedules, backup, restore, reset) needs admin
    if ROLE_WEIGHTS.get(role, 0) >= ROLE_WEIGHTS["admin"]:
        return await call_next(request)

    return JSONResponse(status_code=403, content={"detail": "Admin role required"})

DATA_FILE = os.environ.get("DATA_FILE", "/data/hvac_state.json")
_lock = asyncio.Lock()

# ── Default state ─────────────────────────────────────────

DEFAULT_STATE = {
    "devices": [],          # list of device configs
    "schedules": [],        # list of schedule configs
    "settings": {
        "poll_interval": 120,
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
        "vacation_mode": False,
        "vacation_max_temp": 32,
        "vacation_started_at": None,   # ISO timestamp when vacation was enabled
        "vacation_days_limit": 14,     # auto-end vacation after this many days (0=disabled)
        "verbose_logging": False,
        "temp_unit": "both",          # "C", "F", or "both"
        "watchtower_webhook": "",     # optional webhook URL for update notifications
        "nightly_reboot_time": "03:00",  # HH:MM to reboot all dongles, "" to disable
    },
    "usage": {
        "daily": {},        # {"2026-07-01": {"host": {runtime_min, est_kwh, ...}}}
        "devices": {},      # {"host": {name, first_seen}}
        "temp_history": {}, # {"host": [{"t": iso_hour, "in": float, "out": float}, ...]}
    },
    "device_state": {},     # {"host": {last polled climate state + extras}}
    "logs": [],             # recent automation log entries
    "users": {},            # {"username": {hash, salt, role, must_change_password}}
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
    "_pre_autocool_mode": None,
    "_pre_autocool_temp": None,
    "_last_poll": None,
    "_last_seen": None,
    "_stale": False,
    "_last_mode": None,
    "_last_poll_epoch": None,
    "_on_time_minutes": 0.0,
    "_retry_queue": [],
    "_health_history": [],    # [{ts, event: "online"|"offline", uptime?}] last 50
    "_firmware_version": None,
    "_consecutive_failures": 0,
    "_fail_notified": False,
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

# ── Auth ──────────────────────────────────────────────────

ROLES = ["admin", "operator", "viewer"]
ROLE_WEIGHTS = {"viewer": 0, "operator": 1, "admin": 2}

# in-memory token store: {token: {username, role, expires}}
_tokens: dict = {}
TOKEN_TTL_HOURS = 24

def _hash_password(password: str, salt: str = None) -> tuple:
    """Return (hash, salt) for a password."""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return h.hex(), salt

def _verify_password(password: str, stored_hash: str, salt: str) -> bool:
    h, _ = _hash_password(password, salt)
    return secrets.compare_digest(h, stored_hash)

def _create_token(username: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.datetime.utcnow() + datetime.timedelta(hours=TOKEN_TTL_HOURS)
    _tokens[token] = {"username": username, "role": role, "expires": expires.isoformat()}
    # clean expired tokens
    now = datetime.datetime.utcnow().isoformat()
    expired = [t for t, v in _tokens.items() if v["expires"] < now]
    for t in expired:
        del _tokens[t]
    return token

def _get_token_info(authorization: str = None) -> Optional[dict]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    info = _tokens.get(token)
    if not info:
        return None
    if info["expires"] < datetime.datetime.utcnow().isoformat():
        del _tokens[token]
        return None
    return info

def _require_role(role: str, authorization: str = None):
    """Raise 401/403 if token doesn't meet required role level."""
    # if no users configured yet, allow all (first-run)
    if not _state.get("users"):
        return {"username": "admin", "role": "admin"}
    info = _get_token_info(authorization)
    if not info:
        raise HTTPException(status_code=401, detail="Authentication required")
    if ROLE_WEIGHTS.get(info["role"], 0) < ROLE_WEIGHTS.get(role, 0):
        raise HTTPException(status_code=403, detail=f"Requires {role} role")
    return info

def _ensure_default_admin():
    """Create default admin/admin user if no users exist."""
    if not _state["users"]:
        h, s = _hash_password("admin")
        _state["users"]["admin"] = {
            "hash": h, "salt": s,
            "role": "admin",
            "must_change_password": True,
        }
        log.info("Created default admin/admin user — password change required on first login")



def _load_raw() -> dict:
    if not os.path.exists(DATA_FILE):
        log.info("No state file found — starting fresh")
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
        log.error(f"Failed to load state: {e} — falling back to defaults")
        # save corrupt file for inspection
        try:
            import shutil
            shutil.copy(DATA_FILE, DATA_FILE + ".corrupt")
        except Exception:
            pass
        return json.loads(json.dumps(DEFAULT_STATE))

def _save_raw(data: dict):
    os.makedirs(os.path.dirname(os.path.abspath(DATA_FILE)), exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    # exclude logs from JSON state — persisted separately in log file
    save_data = {k: v for k, v in data.items() if k != "logs"}
    with open(tmp, "w") as f:
        json.dump(save_data, f, indent=2)
    os.replace(tmp, DATA_FILE)
    # daily backup rotation — keep last 3
    try:
        today = datetime.date.today().isoformat()
        backup = DATA_FILE + f".bak.{today}"
        if not os.path.exists(backup):
            import shutil
            shutil.copy(DATA_FILE, backup)
            # remove backups older than 3 days
            bak_dir = os.path.dirname(os.path.abspath(DATA_FILE))
            base = os.path.basename(DATA_FILE)
            backups = sorted([
                f for f in os.listdir(bak_dir)
                if f.startswith(base + ".bak.")
            ])
            for old in backups[:-3]:
                os.remove(os.path.join(bak_dir, old))
    except Exception:
        pass

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

def _ts_iso() -> str:
    return datetime.datetime.now().isoformat(timespec='seconds')

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

LOG_FILE = os.environ.get("LOG_FILE", DATA_FILE.replace(".json", "_log.jsonl"))
_LOG_MAX_LINES = 2000

def _append_log_file(entry: dict):
    """Append a log entry to the persistent JSONL log file."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(LOG_FILE)), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
        # rotate: keep last _LOG_MAX_LINES lines
        _rotate_log_file()
    except Exception as e:
        log.warning(f"Log file write failed: {e}")

def _rotate_log_file():
    """Keep log file under _LOG_MAX_LINES by trimming oldest entries."""
    try:
        if not os.path.exists(LOG_FILE):
            return
        with open(LOG_FILE) as f:
            lines = f.readlines()
        if len(lines) > _LOG_MAX_LINES:
            with open(LOG_FILE, "w") as f:
                f.writelines(lines[-_LOG_MAX_LINES:])
    except Exception:
        pass

def _load_log_file() -> list:
    """Load recent log entries from the persistent log file."""
    try:
        if not os.path.exists(LOG_FILE):
            return []
        with open(LOG_FILE) as f:
            lines = f.readlines()
        entries = []
        for line in reversed(lines[-500:]):  # load last 500 into memory
            try:
                entries.append(json.loads(line.strip()))
            except Exception:
                pass
        return entries
    except Exception as e:
        log.warning(f"Log file load failed: {e}")
        return []

def _clear_log_file():
    """Clear the persistent log file."""
    try:
        open(LOG_FILE, "w").close()
    except Exception:
        pass

def _add_log(msg: str, level: str = "info"):
    entry = {"time": _ts(), "iso": _ts_iso(), "msg": msg, "level": level}
    log.info(f"[{level.upper()}] {msg}")
    _state["logs"].insert(0, entry)
    if len(_state["logs"]) > 500:
        _state["logs"] = _state["logs"][:500]
    _append_log_file(entry)

def _verbose(msg: str, level: str = "info"):
    """Only log if verbose_logging is enabled."""
    if _state["settings"].get("verbose_logging", False):
        _add_log(msg, level)

# ── Health history ────────────────────────────────────────

def _record_health_event(device: dict, event: str):
    """Record online/offline/reboot event, keep last 50."""
    if "_health_history" not in device:
        device["_health_history"] = []
    device["_health_history"].insert(0, {
        "ts": _now_iso(),
        "event": event,
    })
    device["_health_history"] = device["_health_history"][:50]


# ── AC communication ──────────────────────────────────────

# ESPHome entity paths — new firmware uses friendly name with spaces,
# old firmware uses underscored slugs. Try both.
CLIMATE_PATHS = [
    "climate/Air%20Conditioner",
    "climate/air_conditioner",
]

async def _fetch_state(host: str) -> Optional[dict]:
    for path in CLIMATE_PATHS:
        url = f"http://{host}/{path}"
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(url)
                    if r.status_code == 200:
                        return r.json()
                    elif r.status_code == 404:
                        break  # try next path
            except Exception as e:
                if attempt == 2:
                    log.warning(f"{host} fetch failed: {e}")
                await asyncio.sleep(0.8 * (attempt + 1))
    return None

async def _fetch_sensors(host: str) -> dict:
    # Try new firmware (space-encoded) and old firmware (underscored) paths
    path_candidates = {
        "outdoor_temp": [
            "sensor/Air%20Conditioner%20Outdoor%20Temperature",
            "sensor/air_conditioner_outdoor_temperature",
        ],
        "uptime_days": [
            "sensor/Air%20Conditioner%20Uptime%20Days",
            "sensor/air_conditioner_uptime_days",
        ],
        "power_usage": [
            "sensor/Air%20Conditioner%20Power%20Usage",
            "sensor/air_conditioner_power_usage",
        ],
    }
    wifi_paths = [
        "sensor/Air%20Conditioner%20Wi-Fi%20Signal",
        "sensor/air_conditioner_wi-fi_signal",
        "sensor/air_conditioner_wi_fi_signal",
        "sensor/wifi_signal",
    ]
    esphome_version_paths = [
        "text_sensor/Air%20Conditioner%20ESPHome%20Version",
        "text_sensor/air_conditioner_esphome_version",
        "text_sensor/esphome_version",
    ]
    out = {}
    async with httpx.AsyncClient(timeout=3) as client:
        for key, paths in path_candidates.items():
            for path in paths:
                try:
                    r = await client.get(f"http://{host}/{path}")
                    if r.status_code == 200:
                        out[key] = r.json()
                        break
                except:
                    pass
        for wp in wifi_paths:
            try:
                r = await client.get(f"http://{host}/{wp}")
                if r.status_code == 200:
                    out["wifi_signal"] = r.json()
                    break
            except:
                pass
        for vp in esphome_version_paths:
            try:
                r = await client.get(f"http://{host}/{vp}")
                if r.status_code == 200:
                    out["esphome_version"] = r.json()
                    break
            except:
                pass
    return out

async def _send_cmd(host: str, params: dict) -> bool:
    from urllib.parse import urlencode
    qs = urlencode(params)
    for path in CLIMATE_PATHS:
        url = f"http://{host}/{path}/set?{qs}"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.post(url)
                if r.status_code < 300:
                    return True
                elif r.status_code == 404:
                    continue
        except Exception as e:
            log.warning(f"{host} cmd failed: {e}")
    return False

BEEPER_PATHS = [
    "switch/Air%20Conditioner%20Beeper",
    "switch/air_conditioner_beeper",
]

async def _send_switch(host: str, path: str) -> bool:
    # if path contains a known beeper slug, try both variants
    if "beeper" in path.lower():
        action = path.split("/")[-1]  # turn_on / turn_off
        for base in BEEPER_PATHS:
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    r = await client.post(f"http://{host}/{base}/{action}")
                    if r.status_code < 300:
                        return True
                    elif r.status_code == 404:
                        continue
            except:
                pass
        return False
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
        prev_stale = device.get("_stale", False)
        device["_stale"] = True
        device["_consecutive_failures"] = device.get("_consecutive_failures", 0) + 1
        failures = device["_consecutive_failures"]
        if not prev_stale:
            _add_log(f"{name}: 🔴 went offline", "err")
            _record_health_event(device, "offline")
        elif failures % 10 == 0:
            # log every 10th consecutive failure
            _add_log(f"{name}: still unreachable ({failures} consecutive failures)", "warn")
        # cap retry queue at 10 entries
        if len(device.get("_retry_queue", [])) > 10:
            dropped = len(device["_retry_queue"]) - 10
            device["_retry_queue"] = device["_retry_queue"][-10:]
            _verbose(f"{name}: retry queue capped — dropped {dropped} oldest entries", "warn")
        _state["device_state"][host] = {"error": "unreachable", "host": host,
                                         "last_seen": device.get("_last_seen")}
        return

    # ── watchdog: mark recovered if was stale ─────────────
    if device.get("_stale"):
        failures = device.get("_consecutive_failures", 0)
        device["_stale"] = False
        device["_consecutive_failures"] = 0
        device["_fail_notified"] = False
        _add_log(f"{name}: 🟢 back online (was down {failures} poll(s))", "ok")
        _record_health_event(device, "online")

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
                    _record_health_event(device, "reboot")
            except:
                pass
        ds["uptime_days"] = new_uptime
    if "wifi_signal" in sensors:
        raw = sensors["wifi_signal"].get("value")
        if raw is not None:
            try:
                ds["wifi_signal"] = float(str(raw).split()[0])
            except Exception:
                ds["wifi_signal"] = raw
        log.debug(f"{name}: wifi={ds.get('wifi_signal')}dBm")
    if "esphome_version" in sensors:
        fw = sensors["esphome_version"].get("state") or \
             sensors["esphome_version"].get("value")
        ds["esphome_version"] = fw
        # store short version (strip build hash if present)
        if fw:
            device["_firmware_version"] = fw.split(" ")[0] if fw else None
    # actual power_usage — use if non-zero, otherwise fall back to estimation
    if "power_usage" in sensors:
        raw_power = sensors["power_usage"].get("value")
        if raw_power is not None:
            try:
                pw = float(str(raw_power).split()[0])
                ds["actual_power_watts"] = pw if pw > 0 else None
            except:
                ds["actual_power_watts"] = None

    # beeper sync — read device state as source of truth, update saved if different
    if "beeper" in sensors:
        raw = sensors["beeper"].get("value")
        if raw is not None:
            device_beeper = "ON" if raw is True or str(raw).upper() == "ON" else "OFF"
            ds["beeper"] = device_beeper
            if device.get("beeper") != device_beeper:
                device["beeper"] = device_beeper
                _verbose(f"{name}: beeper state updated → {device_beeper.lower()}", "info")

    # on-time tracking
    now_epoch = datetime.datetime.utcnow().timestamp()
    last_epoch = device.get("_last_poll_epoch")
    last_mode = device.get("_last_mode")
    if last_epoch and last_mode and last_mode != "OFF":
        elapsed_mins = (now_epoch - last_epoch) / 60
        device["_on_time_minutes"] = device.get("_on_time_minutes", 0) + elapsed_mins
        _record_usage(device, ds, elapsed_mins)

    # log mode changes — skip first poll after restart (last_mode=None means no prior state)
    cur_mode = state.get("mode", "OFF")
    if last_mode is not None and last_mode != cur_mode:
        if cur_mode == "OFF":
            prev = f" (was {last_mode})" if last_mode != "OFF" else ""
            _add_log(f"{name}: turned off{prev}", "info")
        else:
            _add_log(f"{name}: turned on ({cur_mode}) — detected externally", "ok")

    device["_last_mode"] = cur_mode
    device["_last_poll_epoch"] = now_epoch
    device["_last_poll"] = _now_iso()
    ds["last_seen"] = device["_last_seen"]
    ds["stale"] = False
    _state["device_state"][host] = ds

    _verbose(f"{name}: {state.get('current_temperature')}°C in, {ds.get('outdoor_temp')}°C out, mode={cur_mode}", "ok")

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

    # guard hours only apply to triggering auto-on — auto-off always allowed
    now_hour = datetime.datetime.now().hour
    guard_start = _state["settings"].get("max_temp_guard_start", 8)
    guard_end = _state["settings"].get("max_temp_guard_end", 22)
    within_guard_hours = guard_start <= now_hour < guard_end

    if indoor >= max_temp and not is_cooling and not active and within_guard_hours:
        device["_max_temp_active"] = True
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
    elif indoor < (max_temp - 1) and active:
        # hysteresis: turn off 1°C below max to prevent rapid cycling
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

        # build list of commands to send
        commands = []
        if power == "off":
            commands.append({"mode": "OFF"})
        else:
            if power == "on" and mode:
                commands.append({"mode": mode})
            elif mode:
                commands.append({"mode": mode})
            if temp:
                commands.append({"target_temperature": temp})

        # send commands — queue any that fail for retry on next poll
        all_ok = True
        for cmd in commands:
            ok = await _send_cmd(host, cmd)
            if ok:
                if "mode" in cmd:
                    m = cmd["mode"]
                    if m == "OFF":
                        _add_log(f"{name}: scheduled off", "ok")
                    elif power == "on":
                        _add_log(f"{name}: scheduled on → {m}", "ok")
                    else:
                        _add_log(f"{name}: scheduled mode → {m}", "ok")
                if "target_temperature" in cmd:
                    _add_log(f"{name}: scheduled temp → {cmd['target_temperature']}°C", "ok")
            else:
                all_ok = False
                if "_retry_queue" not in device:
                    device["_retry_queue"] = []
                device["_retry_queue"].append(cmd)
                _add_log(f"{name}: schedule command failed — queued for retry: {cmd}", "warn")

        if not all_ok:
            _add_log(f"{name}: schedule @ {hhmm} partially failed — {len(device['_retry_queue'])} cmd(s) queued", "warn")

        sch["last_run"] = f"{today} {_ts()}"

    # ── Check schedule end times ──────────────────────────────
    for sch in _state["schedules"]:
        end_time = sch.get("end_time")
        if not end_time or not sch.get("enabled", True):
            continue
        if end_time != hhmm:
            continue
        # for end times, check if yesterday's day is in the schedule
        # (handles overnight schedules like 20:45–06:45)
        start_time = sch.get("time", "00:00")
        is_overnight = end_time < start_time  # e.g. end=06:45 < start=20:45
        if is_overnight:
            check_day = (js_day - 1) % 7
        else:
            check_day = js_day
        if check_day not in sch.get("days", []):
            continue
        last_end_run = sch.get("_last_end_run", "")
        if last_end_run and last_end_run.startswith(today):
            continue
        host = sch.get("device_host", "")
        device = next((d for d in _state["devices"] if d["host"] == host), None)
        if not device:
            continue
        name = device["name"]
        ok = await _send_cmd(host, {"mode": "OFF"})
        if ok:
            _add_log(f"Schedule end: {name} @ {hhmm} — auto off", "ok")
        else:
            if "_retry_queue" not in device:
                device["_retry_queue"] = []
            device["_retry_queue"].append({"mode": "OFF"})
            _add_log(f"Schedule end: {name} @ {hhmm} — failed, queued for retry", "warn")
        sch["_last_end_run"] = f"{today} {_ts()}"

async def _check_missed_schedules():
    """Fire any schedules that were missed while the API was down.
    Only fires if missed within the last MISSED_WINDOW_MINUTES minutes."""
    MISSED_WINDOW_MINUTES = 30
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    js_day = now.isoweekday() % 7

    for sch in _state["schedules"]:
        if not sch.get("enabled", True):
            continue
        if js_day not in sch.get("days", []):
            continue

        # parse schedule time
        try:
            sch_hh, sch_mm = map(int, sch["time"].split(":"))
        except Exception:
            continue

        sch_dt = now.replace(hour=sch_hh, minute=sch_mm, second=0, microsecond=0)
        if sch_dt > now:
            continue  # hasn't happened yet today

        # check if it was missed — last_run not today or before scheduled time
        last_run = sch.get("last_run", "")
        if last_run and last_run.startswith(today):
            continue  # already ran today

        # only fire if within the missed window
        missed_mins = (now - sch_dt).total_seconds() / 60
        if missed_mins > MISSED_WINDOW_MINUTES:
            continue

        host = sch.get("device_host", "")
        device = next((d for d in _state["devices"] if d["host"] == host), None)
        if not device:
            continue

        name = device["name"]
        _add_log(f"⚡ Missed schedule recovered: {name} @ {sch['time']} ({int(missed_mins)}m late)", "warn")

        power = sch.get("power")
        mode = sch.get("mode")
        temp = sch.get("temp")
        commands = []
        if power == "off":
            commands.append({"mode": "OFF"})
        else:
            if power == "on" and mode:
                commands.append({"mode": mode})
            elif mode:
                commands.append({"mode": mode})
            if temp:
                commands.append({"target_temperature": temp})

        for cmd in commands:
            ok = await _send_cmd(host, cmd)
            if not ok:
                if "_retry_queue" not in device:
                    device["_retry_queue"] = []
                device["_retry_queue"].append(cmd)

        sch["last_run"] = f"{today} {_ts()}"

    async with _lock:
        _save_raw(_state)


# ── Main background worker ────────────────────────────────

async def _background_worker():
    _add_log("Background worker started", "info")
    last_schedule_check = ""
    last_rate_update = ""
    last_nightly_reboot = ""

    # ── Startup retry with exponential backoff ────────────────
    # Wait for network before first real poll
    for attempt in range(8):
        if not _state["devices"]:
            break
        try:
            first = _state["devices"][0]
            async with httpx.AsyncClient(timeout=4) as client:
                r = await client.get(f"http://{first['host']}/{CLIMATE_PATHS[0]}")
                if r.status_code < 500:
                    _add_log("Network ready", "ok")
                    break
        except Exception:
            delay = min(2 ** attempt, 60)
            _add_log(f"Network not ready — retrying in {delay}s (attempt {attempt+1})", "warn")
            await asyncio.sleep(delay)

    # ── Check for missed schedules during downtime ────────────
    await _check_missed_schedules()

    while True:
        try:
            interval = _state["settings"].get("poll_interval", 60)

            # Poll all devices with jitter to avoid simultaneous requests
            for device in _state["devices"]:
                await _poll_device(device)
                await _check_max_temp(device)
                _check_watchdog(device)
                await asyncio.sleep(0.5)  # jitter between devices

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

            # Nightly dongle reboot
            nightly_reboot_time = _state["settings"].get("nightly_reboot_time", "03:00")
            if nightly_reboot_time and hhmm == nightly_reboot_time and today != last_nightly_reboot:
                last_nightly_reboot = today
                _add_log("🔄 Nightly dongle reboot starting…", "info")
                for device in _state["devices"]:
                    host = device["host"]
                    name = device["name"]
                    paths = [
                        f"http://{host}/button/air_conditioner_restart/press",
                        f"http://{host}/button/Air%20Conditioner%20Restart/press",
                    ]
                    rebooted = False
                    for path in paths:
                        try:
                            async with httpx.AsyncClient(timeout=5) as client:
                                r = await client.post(path)
                                if r.status_code < 300:
                                    rebooted = True
                                    break
                        except Exception:
                            pass
                    if rebooted:
                        _verbose(f"{name}: nightly reboot sent", "info")
                    else:
                        _add_log(f"{name}: nightly reboot failed — unreachable", "warn")
                    await asyncio.sleep(5)  # stagger 5s apart
                _add_log("🔄 Nightly dongle reboot complete", "ok")

            # Auto-end vacation mode if time limit exceeded
            s = _state["settings"]
            if s.get("vacation_mode") and s.get("vacation_started_at"):
                days_limit = s.get("vacation_days_limit", 14)
                if days_limit > 0:
                    try:
                        started = datetime.datetime.fromisoformat(s["vacation_started_at"])
                        elapsed_days = (datetime.datetime.now() - started).days
                        if elapsed_days >= days_limit:
                            _add_log(f"🌴 Vacation mode auto-ended after {elapsed_days} days", "warn")
                            # inline vacation off logic
                            s["vacation_mode"] = False
                            s["vacation_started_at"] = None
                            for d in _state["devices"]:
                                d["max_temp"] = d.pop("_pre_vacation_max_temp", None)
                                d.pop("_pre_vacation_mode", None)
                            for sch in _state["schedules"]:
                                if sch.pop("_vacation_paused", False):
                                    sch["enabled"] = True
                    except Exception:
                        pass

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
        if not any(d["host"] == cfg.host for d in _state["devices"]):
            _state["devices"].append({**DEVICE_DEFAULTS, **cfg.dict()})
    else:
        host_changed = cfg.host and cfg.host != host
        device.update(cfg.dict())
        if host_changed:
            # clear stale state and retry queue when host changes
            device["_retry_queue"] = []
            device["_stale"] = False
            device["_consecutive_failures"] = 0
            _state["device_state"].pop(host, None)
            name = device.get("name", host)
            _add_log(f"{name}: host changed to {cfg.host} — retry queue cleared", "info")
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

@app.post("/devices/{host:path}/poll")
async def poll_device_now(host: str):
    """Poll a single device immediately and return its current state."""
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    await _poll_device(device)
    await _check_max_temp(device)
    async with _lock:
        _save_raw(_state)
    ds = _state["device_state"].get(host, {})
    return {
        "ok": not device.get("_stale", False),
        "stale": device.get("_stale", False),
        "firmware_version": device.get("_firmware_version"),
        "state": ds,
    }

@app.post("/devices/{host:path}/cmd")
async def send_device_cmd(host: str, payload: CommandPayload, authorization: Optional[str] = Header(None)):
    info = _get_token_info(authorization)
    user = info["username"] if info else "api"
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    name = device["name"] if device else host
    ok = await _send_cmd(host, payload.params)
    if ok:
        ds = _state["device_state"].get(host, {})
        ds.update(payload.params)
        # log dashboard-initiated mode changes
        if "mode" in payload.params:
            m = payload.params["mode"]
            if m == "OFF":
                _add_log(f"{name}: turned off by {user}", "info")
            else:
                _add_log(f"{name}: turned on ({m}) by {user}", "ok")
        if "target_temperature" in payload.params:
            _add_log(f"{name}: set → {payload.params['target_temperature']}°C by {user}", "info")
    else:
        if device is not None:
            if "_retry_queue" not in device:
                device["_retry_queue"] = []
            device["_retry_queue"].append(payload.params)
            _add_log(f"{name}: command queued for retry {payload.params}", "warn")
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
    if device["lock_temp"]:
        _add_log(f"{device['name']}: temp 🔒 locked at {device.get('locked_target_temp')}°C", "info")
    else:
        _add_log(f"{device['name']}: temp 🔓 unlocked", "info")
    return {"ok": True, "lock_temp": device["lock_temp"], "locked_target_temp": device.get("locked_target_temp")}

@app.post("/devices/{host:path}/display-toggle")
async def display_toggle(host: str):
    """Toggle the AC unit display on/off."""
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    name = device["name"] if device else host
    paths = [
        "button/Air%20Conditioner%20Display%20Toggle/press",
        "button/air_conditioner_display_toggle/press",
    ]
    for path in paths:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.post(f"http://{host}/{path}")
                if r.status_code < 300:
                    _add_log(f"{name}: display toggled", "info")
                    return {"ok": True}
        except:
            pass
    return {"ok": False, "error": "display toggle not supported"}

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
            day_names = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]
            days_str = ",".join(day_names[d] for d in sorted(overlap))
            conflicts.append(f"conflicts with schedule {s['id']} @ {s['time']} on {days_str}")
    return conflicts

@app.get("/schedules")
async def get_schedules():
    return {"schedules": _state["schedules"]}

@app.post("/schedules")
async def add_schedule(cfg: ScheduleConfig):
    import uuid
    sch = {**SCHEDULE_DEFAULTS, **cfg.dict()}
    sch["id"] = cfg.id or str(uuid.uuid4())[:8]
    conflicts = _detect_schedule_conflicts(sch)
    if conflicts:
        for c in conflicts:
            _add_log(f"⚠ Schedule conflict: {c}", "warn")
    _state["schedules"].append(sch)
    async with _lock:
        _save_raw(_state)
    return {"ok": True, "id": sch["id"], "warnings": conflicts}

@app.put("/schedules/{sch_id}")
async def update_schedule(sch_id: str, cfg: ScheduleConfig):
    sch = next((s for s in _state["schedules"] if s["id"] == sch_id), None)
    if not sch:
        return {"ok": False, "error": "not found"}
    sch.update(cfg.dict())
    sch["id"] = sch_id
    conflicts = _detect_schedule_conflicts(sch, exclude_id=sch_id)
    if conflicts:
        for c in conflicts:
            _add_log(f"⚠ Schedule conflict: {c}", "warn")
    async with _lock:
        _save_raw(_state)
    return {"ok": True, "warnings": conflicts}

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
    prev_verbose = _state["settings"].get("verbose_logging", False)
    _state["settings"].update(settings)
    new_verbose = _state["settings"].get("verbose_logging", False)
    if new_verbose != prev_verbose:
        _add_log(f"Verbose logging {'enabled' if new_verbose else 'disabled'}", "info")
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

@app.delete("/logs")
async def clear_logs():
    _state["logs"] = []
    _clear_log_file()
    async with _lock:
        _save_raw(_state)
    return {"ok": True}

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

@app.post("/auth/recover")
async def recover_password(data: dict):
    """Reset admin password using the recovery key printed to Docker logs on startup."""
    key = data.get("recovery_key", "")
    new_password = data.get("new_password", "")
    stored_key = _state.get("_recovery_key", "")
    if not stored_key or not secrets.compare_digest(key, stored_key):
        raise HTTPException(status_code=403, detail="Invalid recovery key")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    h, s = _hash_password(new_password)
    _state["users"]["admin"] = {"hash": h, "salt": s, "role": "admin", "must_change_password": False}
    # invalidate recovery key after use
    _state["_recovery_key"] = secrets.token_urlsafe(24)
    # revoke all existing tokens
    _tokens.clear()
    async with _lock:
        _save_raw(_state)
    _add_log("Admin password reset via recovery key", "warn")
    return {"ok": True, "message": "Admin password reset — please log in with new password"}

@app.post("/auth/login")
async def login(data: dict):
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    user = _state["users"].get(username)
    if not user or not _verify_password(password, user["hash"], user["salt"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = _create_token(username, user["role"])
    return {
        "ok": True,
        "token": token,
        "username": username,
        "role": user["role"],
        "must_change_password": user.get("must_change_password", False),
    }

@app.post("/auth/logout")
async def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        _tokens.pop(token, None)
    return {"ok": True}

@app.post("/auth/change-password")
async def change_password(data: dict, authorization: Optional[str] = Header(None)):
    info = _require_role("viewer", authorization)
    username = info["username"]
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "")
    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    user = _state["users"].get(username)
    if not user or not _verify_password(old_pw, user["hash"], user["salt"]):
        raise HTTPException(status_code=401, detail="Current password incorrect")
    h, s = _hash_password(new_pw)
    user["hash"] = h
    user["salt"] = s
    user["must_change_password"] = False
    async with _lock:
        _save_raw(_state)
    _add_log(f"Password changed for user '{username}'", "info")
    return {"ok": True}

@app.get("/auth/users")
async def list_users(authorization: Optional[str] = Header(None)):
    _require_role("admin", authorization)
    return {"users": [
        {"username": u, "role": v["role"], "must_change_password": v.get("must_change_password", False)}
        for u, v in _state["users"].items()
    ]}

@app.post("/auth/users")
async def add_user(data: dict, authorization: Optional[str] = Header(None)):
    _require_role("admin", authorization)
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    role = data.get("role", "viewer")
    if not username or len(password) < 6:
        raise HTTPException(status_code=400, detail="Username and password (6+ chars) required")
    if role not in ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of {ROLES}")
    if username in _state["users"]:
        raise HTTPException(status_code=409, detail="User already exists")
    h, s = _hash_password(password)
    _state["users"][username] = {"hash": h, "salt": s, "role": role, "must_change_password": True}
    async with _lock:
        _save_raw(_state)
    _add_log(f"User '{username}' added with role '{role}'", "info")
    return {"ok": True}

@app.delete("/auth/users/{username}")
async def delete_user(username: str, authorization: Optional[str] = Header(None)):
    info = _require_role("admin", authorization)
    if username == info["username"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if username not in _state["users"]:
        raise HTTPException(status_code=404, detail="User not found")
    del _state["users"][username]
    # revoke any active tokens for this user
    to_revoke = [t for t, v in _tokens.items() if v["username"] == username]
    for t in to_revoke:
        del _tokens[t]
    async with _lock:
        _save_raw(_state)
    _add_log(f"User '{username}' deleted", "warn")
    return {"ok": True}

@app.post("/auth/users/{username}/force-reset")
async def force_password_reset(username: str, authorization: Optional[str] = Header(None)):
    _require_role("admin", authorization)
    user = _state["users"].get(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user["must_change_password"] = True
    async with _lock:
        _save_raw(_state)
    _add_log(f"Password reset forced for user '{username}'", "warn")
    return {"ok": True}

@app.put("/auth/users/{username}/role")
async def set_user_role(username: str, data: dict, authorization: Optional[str] = Header(None)):
    info = _require_role("admin", authorization)
    if username == info["username"]:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    user = _state["users"].get(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    role = data.get("role")
    if role not in ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of {ROLES}")
    old_role = user.get("role", "viewer")
    user["role"] = role
    async with _lock:
        _save_raw(_state)
    _add_log(f"User '{username}' role changed: {old_role} → {role} (by {info['username']})", "info")
    return {"ok": True}

@app.get("/auth/me")
async def get_me(authorization: Optional[str] = Header(None)):
    # if no users, return open access
    if not _state.get("users"):
        return {"username": "admin", "role": "admin", "must_change_password": False}
    info = _get_token_info(authorization)
    if not info:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = _state["users"].get(info["username"], {})
    return {
        "username": info["username"],
        "role": info["role"],
        "must_change_password": user.get("must_change_password", False),
    }


@app.get("/discover")
async def discover_devices(subnet: Optional[str] = None):
    """Scan network for ESPHome devices running Midea climate control."""
    if not subnet:
        for d in _state["devices"]:
            host = d["host"]
            try:
                ipaddress.ip_address(host)
                parts = host.rsplit(".", 1)
                subnet = parts[0] + ".0/24"
                break
            except ValueError:
                pass
    if not subnet:
        return {"ok": False, "error": "Cannot determine subnet — provide ?subnet=192.168.27.0/24"}
    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid subnet: {subnet}")

    _add_log(f"🔍 Scanning {subnet} for ESPHome devices…", "info")
    found = []
    existing_hosts = {d["host"] for d in _state["devices"]}

    async def _probe(ip: str):
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                r = await client.get(f"http://{ip}/")
                if r.status_code != 200:
                    return
                rc = await client.get(f"http://{ip}/climate/air_conditioner")
                if rc.status_code == 200:
                    data = rc.json()
                    fw = None
                    try:
                        rv = await client.get(f"http://{ip}/text_sensor/air_conditioner_esphome_version")
                        if rv.status_code == 200:
                            fw = rv.json().get("value", "").split(" ")[0]
                    except Exception:
                        pass
                    found.append({
                        "ip": ip,
                        "mode": data.get("mode"),
                        "current_temperature": data.get("current_temperature"),
                        "firmware": fw,
                        "already_configured": ip in existing_hosts,
                    })
        except Exception:
            pass

    hosts = [str(ip) for ip in network.hosts()]
    batch_size = 32
    for i in range(0, len(hosts), batch_size):
        await asyncio.gather(*[_probe(ip) for ip in hosts[i:i+batch_size]])

    _add_log(f"🔍 Discovery complete — {len(found)} ESPHome device(s) found in {subnet}", "ok")
    return {
        "ok": True,
        "subnet": subnet,
        "scanned": len(hosts),
        "found": found,
        "new": [f for f in found if not f["already_configured"]],
    }

@app.get("/health")
async def health():
    """Detailed health check — per-device status, worker health, system info."""
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

@app.post("/vacation/{state}")
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

@app.get("/vacation")
async def get_vacation():
    s = _state["settings"]
    return {
        "vacation_mode": s.get("vacation_mode", False),
        "vacation_max_temp": s.get("vacation_max_temp", 32),
        "vacation_started_at": s.get("vacation_started_at"),
        "vacation_days_limit": s.get("vacation_days_limit", 14),
    }

@app.get("/devices/{host:path}/health-history")
async def get_health_history(host: str):
    device = next((d for d in _state["devices"] if d["host"] == host), None)
    if not device:
        return {"host": host, "history": []}
    return {"host": host, "name": device.get("name"), "history": device.get("_health_history", [])}

@app.post("/watchtower-notify")
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
