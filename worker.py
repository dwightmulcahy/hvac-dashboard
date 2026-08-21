"""
HVAC Dashboard — background worker.

Owns everything that runs 24/7 regardless of whether a browser is
open: device polling, the max-temp guard, schedule firing (including
missed-schedule recovery), the watchdog, usage/health tracking, and
the nightly dongle reboot / daily exchange-rate refresh.

Exposes a small set of functions that api.py's REST endpoints call
directly (_send_cmd, _poll_device, _check_max_temp, _fetch_exchange_rate)
for on-demand actions like "poll this device now" or "test this device",
so those endpoints share exactly the same logic as the background loop
rather than duplicating it.
"""

import asyncio
import datetime
import logging
from typing import Optional
from urllib.parse import urlencode

import httpx

from state import (
    _state, _lock, _save_raw, _add_log, _verbose,
    _utcnow, _now_iso, _today, _ts, _est_watts,
)
from maintenance_logic import _maintenance_status
from notify import notify

log = logging.getLogger("hvac")


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
        # ESPHome exposes a switch's current state via GET on its base
        # path (POST .../turn_on and .../turn_off are actions, not
        # reads) — same base paths BEEPER_PATHS uses for writes.
        "beeper": [
            "switch/Air%20Conditioner%20Beeper",
            "switch/air_conditioner_beeper",
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
                except Exception:
                    pass
        for wp in wifi_paths:
            try:
                r = await client.get(f"http://{host}/{wp}")
                if r.status_code == 200:
                    out["wifi_signal"] = r.json()
                    break
            except Exception:
                pass
        for vp in esphome_version_paths:
            try:
                r = await client.get(f"http://{host}/{vp}")
                if r.status_code == 200:
                    out["esphome_version"] = r.json()
                    break
            except Exception:
                pass
    return out

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

async def _send_cmd(host: str, params: dict) -> bool:
    qs = urlencode(params)
    for path in CLIMATE_PATHS:
        url = f"http://{host}/{path}/set?{qs}"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.post(url)
                if r.status_code < 300:
                    # update _last_mode immediately so the next poll doesn't
                    # re-log this as a separate "detected externally"/"turned off" event
                    if "mode" in params:
                        device = next((d for d in _state["devices"] if d["host"] == host), None)
                        if device is not None:
                            device["_last_mode"] = params["mode"]
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
            except Exception:
                pass
        return False
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.post(f"http://{host}/{path}")
            return r.status_code < 300
    except Exception:
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
            except Exception:
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
            except Exception:
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
    now_epoch = _utcnow().timestamp()
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
        except Exception: pass
    if ds.get("outdoor_temp") is not None:
        try: bucket["avg_outdoor"].append(float(ds["outdoor_temp"]))
        except Exception: pass
    bucket["snapshots"] += 1

# ── Watchdog ──────────────────────────────────────────────

async def _check_watchdog(device: dict):
    """Mark device stale if last_seen exceeds watchdog_minutes threshold."""
    last_seen = device.get("_last_seen")
    if not last_seen:
        return
    threshold = device.get("watchdog_minutes", 5)
    try:
        last_dt = datetime.datetime.fromisoformat(last_seen)
        elapsed = (_utcnow() - last_dt).total_seconds() / 60
        was_stale = device.get("_stale", False)
        if elapsed > threshold and not was_stale:
            device["_stale"] = True
            ds = _state["device_state"].get(device["host"], {})
            ds["stale"] = True
            msg = f"{device['name']}: ⚠ no response for {int(elapsed)}m (watchdog: {threshold}m)"
            _add_log(msg, "warn")
            await notify(msg, title="HVAC Device Offline")
    except Exception:
        pass


async def _check_maintenance():
    """Log + notify once per overdue transition (mirrors the watchdog's
    was_stale pattern via the `_notified_overdue` flag) so this doesn't
    re-fire every background-loop cycle. Runs against whole-house items
    too (device_host is None), not just device-scoped ones."""
    for item in _state["maintenance"]:
        status = _maintenance_status(item)
        overdue = status.get("overdue", False)
        already_notified = item.get("_notified_overdue", False)
        if overdue and not already_notified:
            item["_notified_overdue"] = True
            name = item.get("name", "Maintenance item")
            msg = f"🔧 Maintenance overdue: {name}"
            _add_log(msg, "warn")
            await notify(msg, title="HVAC Maintenance Overdue")
        elif not overdue and already_notified:
            item["_notified_overdue"] = False




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
    except Exception:
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
        except Exception:
            target = max_temp - 2
        _add_log(f"{name}: 🌡 {indoor}°C ≥ max {max_temp}°C — auto cool to {target}°C (was {cur_mode})", "warn")
        # mode and temperature are sent as two separate device commands,
        # so they can fail independently — the dashboard's own history
        # of this exact failure mode (mode command succeeds, temperature
        # command silently fails, unit stays at its old high setpoint
        # doing nothing while the dashboard optimistically claims
        # success) is why each one below is checked and recovered from
        # on its own, not just as a combined ok1-and-ok2 pass/fail.
        ok1 = await _send_cmd(host, {"mode": "COOL"})
        if ok1:
            ds["mode"] = "COOL"
        else:
            device.setdefault("_retry_queue", []).append({"mode": "COOL"})
            _add_log(f"{name}: max-temp guard — mode command failed, queued for retry", "warn")

        ok2 = await _send_cmd(host, {"target_temperature": target})
        if ok2:
            ds["target_temperature"] = str(target)
        else:
            device.setdefault("_retry_queue", []).append({"target_temperature": target})
            _add_log(f"{name}: max-temp guard — temperature command failed, queued for retry", "warn")

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
                device.setdefault("_retry_queue", []).append({"mode": "OFF"})
                _add_log(f"{name}: max-temp guard recovery — off command failed, queued for retry", "warn")
        else:
            # restore previous mode and temp
            _add_log(f"{name}: 🌡 {indoor}°C < max {max_temp}°C — restoring {prev_mode}" +
                     (f" @ {prev_temp}°C" if prev_temp else ""), "ok")
            ok1 = await _send_cmd(host, {"mode": prev_mode})
            if ok1:
                ds["mode"] = prev_mode
            else:
                device.setdefault("_retry_queue", []).append({"mode": prev_mode})
                _add_log(f"{name}: max-temp guard recovery — mode restore failed, queued for retry", "warn")
            if prev_temp:
                ok2 = await _send_cmd(host, {"target_temperature": float(prev_temp)})
                if ok2:
                    ds["target_temperature"] = str(prev_temp)
                else:
                    device.setdefault("_retry_queue", []).append({"target_temperature": float(prev_temp)})
                    _add_log(f"{name}: max-temp guard recovery — temperature restore failed, queued for retry", "warn")

# ── Scheduler ─────────────────────────────────────────────

def _build_schedule_commands(sch: dict) -> list:
    """Build the list of command dicts a schedule should send, given its
    power/mode/temp fields. Shared by _check_schedules and
    _check_missed_schedules to avoid the two copies drifting apart."""
    power = sch.get("power")
    mode = sch.get("mode")
    temp = sch.get("temp")
    commands = []
    if power == "off":
        commands.append({"mode": "OFF"})
    else:
        if mode:
            commands.append({"mode": mode})
        if temp:
            commands.append({"target_temperature": temp})
    return commands


async def _check_schedules():
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    js_day = now.isoweekday() % 7  # Sun=0, Mon=1 ... Sat=6

    for sch in _state["schedules"]:
        if not sch.get("enabled", True):
            continue
        if js_day not in sch.get("days", []):
            continue
        try:
            sch_hh, sch_mm = map(int, sch["time"].split(":"))
        except Exception:
            continue
        sch_dt = now.replace(hour=sch_hh, minute=sch_mm, second=0, microsecond=0)
        if now < sch_dt:
            continue  # hasn't happened yet today
        # within a reasonable catch-up window so we don't fire hours-old triggers
        if (now - sch_dt).total_seconds() > 10 * 60:
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
        _add_log(f"Schedule firing: {name} @ {sch['time']}", "info")

        power = sch.get("power")
        commands = _build_schedule_commands(sch)

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
            msg = f"{name}: schedule @ {sch['time']} partially failed — {len(device['_retry_queue'])} cmd(s) queued"
            _add_log(msg, "warn")
            await notify(msg, title="HVAC Schedule Failed")

        sch["last_run"] = f"{today} {_ts()}"

    # ── Check schedule end times ──────────────────────────────
    for sch in _state["schedules"]:
        end_time = sch.get("end_time")
        if not end_time or not sch.get("enabled", True):
            continue
        try:
            end_hh, end_mm = map(int, end_time.split(":"))
        except Exception:
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
        end_dt = now.replace(hour=end_hh, minute=end_mm, second=0, microsecond=0)
        if now < end_dt:
            continue  # hasn't happened yet
        if (now - end_dt).total_seconds() > 10 * 60:
            continue  # catch-up window exceeded
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
            _add_log(f"Schedule end: {name} @ {end_time} — auto off", "ok")
        else:
            if "_retry_queue" not in device:
                device["_retry_queue"] = []
            device["_retry_queue"].append({"mode": "OFF"})
            _add_log(f"Schedule end: {name} @ {end_time} — failed, queued for retry", "warn")
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

        commands = _build_schedule_commands(sch)

        for cmd in commands:
            ok = await _send_cmd(host, cmd)
            if not ok:
                if "_retry_queue" not in device:
                    device["_retry_queue"] = []
                device["_retry_queue"].append(cmd)

        sch["last_run"] = f"{today} {_ts()}"

    # ── Check for missed schedule END times (e.g. overnight shutoff) ──
    for sch in _state["schedules"]:
        end_time = sch.get("end_time")
        if not end_time or not sch.get("enabled", True):
            continue

        try:
            end_hh, end_mm = map(int, end_time.split(":"))
        except Exception:
            continue

        start_time = sch.get("time", "00:00")
        is_overnight = end_time < start_time
        check_day = (js_day - 1) % 7 if is_overnight else js_day
        if check_day not in sch.get("days", []):
            continue

        end_dt = now.replace(hour=end_hh, minute=end_mm, second=0, microsecond=0)
        if end_dt > now:
            continue  # hasn't happened yet

        last_end_run = sch.get("_last_end_run", "")
        if last_end_run and last_end_run.startswith(today):
            continue  # already ran today

        missed_mins = (now - end_dt).total_seconds() / 60
        if missed_mins > MISSED_WINDOW_MINUTES:
            continue

        host = sch.get("device_host", "")
        device = next((d for d in _state["devices"] if d["host"] == host), None)
        if not device:
            continue

        name = device["name"]
        _add_log(f"⚡ Missed schedule end recovered: {name} @ {end_time} ({int(missed_mins)}m late) — auto off", "warn")
        ok = await _send_cmd(host, {"mode": "OFF"})
        if not ok:
            if "_retry_queue" not in device:
                device["_retry_queue"] = []
            device["_retry_queue"].append({"mode": "OFF"})
        sch["_last_end_run"] = f"{today} {_ts()}"

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
        interval = 120  # safe default in case settings lookup fails below
        try:
            interval = _state["settings"].get("poll_interval", 60)

            # Poll all devices with jitter to avoid simultaneous requests
            for device in _state["devices"]:
                await _poll_device(device)
                await _check_max_temp(device)
                await _check_watchdog(device)
                await asyncio.sleep(0.5)  # jitter between devices

            # Check schedules (once per minute)
            hhmm = datetime.datetime.now().strftime("%H:%M")
            if hhmm != last_schedule_check:
                last_schedule_check = hhmm
                await _check_schedules()
                await _check_maintenance()

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
                        elapsed_days = (_utcnow() - started).days
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
