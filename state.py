"""
HVAC Dashboard — shared application state.

This module owns:
- DEFAULT_STATE / DEVICE_DEFAULTS / SCHEDULE_DEFAULTS
- the in-memory _state dict (loaded once at import time)
- _lock — the asyncio.Lock guarding writes to _state
- JSON persistence (load/save, atomic writes, backup rotation)
- the persistent JSONL log file (separate from _state["logs"] in memory)
- small stateless helpers (timestamps, rate calc, watt estimate)

Every other module imports _state, _lock, and the helpers it needs
from here. Nothing in this file imports from worker.py or any router,
so it can never be part of a circular import.
"""

import asyncio
import datetime
import json
import logging
import os
import shutil
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hvac")

DATA_FILE = os.environ.get("DATA_FILE", "/data/hvac_state.json")
LOG_FILE = os.environ.get("LOG_FILE", DATA_FILE.replace(".json", "_log.jsonl"))
_LOG_MAX_LINES = 2000

_lock = asyncio.Lock()

# ── Default state ─────────────────────────────────────────

DEFAULT_STATE = {
    "devices": [],          # list of device configs
    "schedules": [],        # list of schedule configs
    "maintenance": [],      # list of maintenance reminder configs
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
    "has_ir_emitter": False,  # gates IR-dependent features (display toggle, etc.) — most units don't have one soldered on
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

MAINTENANCE_DEFAULTS = {
    "id": "",
    "name": "",
    "device_host": None,
    "trigger_type": "days",
    "interval_days": 90,
    "interval_hours": None,
    "last_done_at": None,
    "last_done_runtime_minutes": None,
    "notes": None,
}

SCHEDULE_DEFAULTS = {
    "id": "",
    "device_host": "",
    "device_name": "",
    "time": "07:00",
    "end_time": None,
    "days": [0, 1, 2, 3, 4, 5, 6],
    "power": None,
    "mode": None,
    "temp": None,
    "enabled": True,
    "last_run": None,
}

# ── JSON state persistence ─────────────────────────────────


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
    except Exception:
        log.exception("Failed to load state — falling back to defaults")
        # save corrupt file for inspection
        try:
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


async def save_state():
    """Acquire the lock and persist _state. Prefer this over calling
    _save_raw directly so every caller goes through the same lock."""
    async with _lock:
        _save_raw(_state)


# in-memory state (loaded once at startup, saved on every mutation)
_state: dict = _load_raw()

# ── Time helpers ────────────────────────────────────────────


def _utcnow() -> datetime.datetime:
    """Naive UTC datetime — behaves identically to the deprecated
    datetime.datetime.utcnow(), safe drop-in replacement everywhere
    we already store/compare naive-UTC ISO strings."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _now_iso() -> str:
    return _utcnow().isoformat()


def _today() -> str:
    return datetime.date.today().isoformat()


def _month() -> str:
    return datetime.date.today().strftime("%Y-%m")


def _ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _ts_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ── Rate / usage helpers ────────────────────────────────────


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
        indoor = float(indoor)
        target = float(target)
    except Exception:
        return None
    eer = (seer or 20) * 0.875
    max_w = btu / eer
    delta = abs(indoor - target)
    try:
        penalty = max(0, (float(outdoor) - 25) / 20) if outdoor and mode != "HEAT" else 0
    except Exception:
        # malformed outdoor reading — treat the same as if it were
        # missing, rather than crashing the whole watts estimate
        penalty = 0
    load = min(1.0, 0.2 + (delta / 8) * 0.8 + penalty * 0.3)
    return round(max_w * load)


# ── Persistent JSONL log file ───────────────────────────────


def _append_log_file(entry: dict):
    """Append a log entry to the persistent JSONL log file."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(LOG_FILE)), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
        # rotate: keep last _LOG_MAX_LINES lines
        _rotate_log_file()
    except Exception:
        log.exception("Log file write failed")


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
    except Exception:
        log.exception("Log file load failed")
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
