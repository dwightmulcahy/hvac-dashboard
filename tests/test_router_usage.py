"""Tests for routers/usage.py: monthly summary, rolling 30-day report,
CSV export, and log filtering/clearing.
"""

import datetime


def _seed_usage_day(api_module, day, host, name, runtime_minutes=60.0, est_kwh=0.5, peak_watts=800.0):
    usage = api_module._state["usage"]
    usage["devices"][host] = {"name": name, "first_seen": "2026-01-01T00:00:00"}
    usage["daily"].setdefault(day, {})[host] = {
        "runtime_minutes": runtime_minutes, "est_kwh": est_kwh,
        "peak_watts": peak_watts, "snapshots": 3,
        "avg_indoor": [24.0, 25.0], "avg_outdoor": [31.0, 32.0],
    }


# ── /usage/summary ────────────────────────────────────────────


def test_usage_summary_empty_when_no_data(client, auth_headers):
    r = client.get("/usage/summary?month=2026-01", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["devices"] == []


def test_usage_summary_aggregates_matching_month(client, auth_headers, api_module):
    _seed_usage_day(api_module, "2026-01-15", "ac1.local", "Living Room")
    r = client.get("/usage/summary?month=2026-01", headers=auth_headers)
    devices = r.json()["devices"]
    assert len(devices) == 1
    assert devices[0]["name"] == "Living Room"
    assert devices[0]["runtime_hours"] == 1.0
    assert devices[0]["days_active"] == 1


def test_usage_summary_excludes_other_months(client, auth_headers, api_module):
    _seed_usage_day(api_module, "2026-02-15", "ac1.local", "Living Room")
    r = client.get("/usage/summary?month=2026-01", headers=auth_headers)
    assert r.json()["devices"] == []


def test_usage_summary_defaults_to_current_month_without_param(client, auth_headers, api_module):
    today_month = datetime.date.today().strftime("%Y-%m")
    _seed_usage_day(api_module, f"{today_month}-05", "ac1.local", "Living Room")
    r = client.get("/usage/summary", headers=auth_headers)
    assert r.json()["month"] == today_month
    assert len(r.json()["devices"]) == 1


def test_usage_summary_computes_avg_indoor_outdoor(client, auth_headers, api_module):
    _seed_usage_day(api_module, "2026-01-15", "ac1.local", "Living Room")
    r = client.get("/usage/summary?month=2026-01", headers=auth_headers)
    d = r.json()["devices"][0]
    assert d["avg_indoor_c"] == 24.5
    assert d["avg_outdoor_c"] == 31.5


def test_usage_summary_multi_day_accumulates_runtime(client, auth_headers, api_module):
    _seed_usage_day(api_module, "2026-01-10", "ac1.local", "Living Room", runtime_minutes=60.0)
    _seed_usage_day(api_module, "2026-01-11", "ac1.local", "Living Room", runtime_minutes=30.0)
    r = client.get("/usage/summary?month=2026-01", headers=auth_headers)
    d = r.json()["devices"][0]
    assert d["runtime_hours"] == 1.5
    assert d["days_active"] == 2


# ── /usage/rolling30 ─────────────────────────────────────────


def test_usage_rolling30_empty_when_no_data(client, auth_headers):
    r = client.get("/usage/rolling30", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["days"] == 30
    assert r.json()["devices"] == []


def test_usage_rolling30_includes_today(client, auth_headers, api_module):
    today = datetime.date.today().isoformat()
    _seed_usage_day(api_module, today, "ac1.local", "Living Room")
    r = client.get("/usage/rolling30", headers=auth_headers)
    devices = r.json()["devices"]
    assert len(devices) == 1
    assert len(devices[0]["daily"]) == 1
    assert devices[0]["daily"][0]["date"] == today


def test_usage_rolling30_excludes_data_older_than_30_days(client, auth_headers, api_module):
    old_day = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()
    _seed_usage_day(api_module, old_day, "ac1.local", "Living Room")
    r = client.get("/usage/rolling30", headers=auth_headers)
    assert r.json()["devices"] == []


def test_usage_rolling30_daily_series_sorted_by_date(client, auth_headers, api_module):
    d1 = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()
    d2 = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    _seed_usage_day(api_module, d2, "ac1.local", "Living Room")  # seed out of order
    _seed_usage_day(api_module, d1, "ac1.local", "Living Room")
    r = client.get("/usage/rolling30", headers=auth_headers)
    daily = r.json()["devices"][0]["daily"]
    assert [d["date"] for d in daily] == sorted([d["date"] for d in daily])


# ── /usage/export-csv ────────────────────────────────────────


def test_export_csv_returns_csv_content_type(client, auth_headers, api_module):
    _seed_usage_day(api_module, "2026-01-15", "ac1.local", "Living Room")
    r = client.get("/usage/export-csv?month=2026-01", headers=auth_headers)
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "hvac-usage-2026-01.csv" in r.headers["content-disposition"]


def test_export_csv_contains_header_and_data_row(client, auth_headers, api_module):
    _seed_usage_day(api_module, "2026-01-15", "ac1.local", "Living Room")
    r = client.get("/usage/export-csv?month=2026-01", headers=auth_headers)
    text = r.text
    assert "Date,Device,Host" in text
    assert "Living Room" in text
    assert "ac1.local" in text


def test_export_csv_empty_month_has_header_only(client, auth_headers):
    r = client.get("/usage/export-csv?month=2026-01", headers=auth_headers)
    lines = [l for l in r.text.strip().split("\r\n") if l]
    assert len(lines) == 1  # header row only


# ── /logs ──────────────────────────────────────────────────────


def test_get_logs_returns_recent_entries(client, auth_headers, api_module):
    api_module._add_log("test message", "info")
    r = client.get("/logs", headers=auth_headers)
    assert r.status_code == 200
    msgs = [l["msg"] for l in r.json()["logs"]]
    assert "test message" in msgs


def test_get_logs_filters_by_exact_level(client, auth_headers, api_module):
    api_module._add_log("info msg", "info")
    api_module._add_log("error msg", "err")
    r = client.get("/logs?level=err", headers=auth_headers)
    logs = r.json()["logs"]
    assert all(l["level"] == "err" for l in logs)
    assert any(l["msg"] == "error msg" for l in logs)


def test_get_logs_filters_by_level_plus_severity(client, auth_headers, api_module):
    api_module._add_log("info msg", "info")
    api_module._add_log("warn msg", "warn")
    api_module._add_log("err msg", "err")
    r = client.get("/logs?level=warn+", headers=auth_headers)
    logs = r.json()["logs"]
    levels = {l["level"] for l in logs}
    assert levels <= {"warn", "err"}
    assert "info" not in levels


def test_get_logs_respects_limit(client, auth_headers, api_module):
    for i in range(10):
        api_module._add_log(f"msg {i}", "info")
    r = client.get("/logs?limit=3", headers=auth_headers)
    assert len(r.json()["logs"]) == 3


def test_delete_logs_clears_all(client, auth_headers, api_module):
    api_module._add_log("will be cleared", "info")
    r = client.delete("/logs", headers=auth_headers)
    assert r.status_code == 200
    assert api_module._state["logs"] == []
