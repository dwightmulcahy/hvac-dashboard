"""Final coverage-gap tests: single untested branches across auth.py,
routers/devices_control.py, routers/system.py, routers/usage.py that
weren't hit by the main test suites — mostly exception branches around
malformed timestamps, and first-time-vs-repeat retry queue paths.
"""

import httpx


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def json(self):
        return {}


# ── auth.py: /me with no users configured ────────────────────


def test_me_returns_open_access_when_no_users_configured(client, api_module):
    api_module._state["users"] = {}
    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == "admin"
    assert r.json()["role"] == "admin"


# ── devices_control.py: first-time retry queue creation ──────


def test_cmd_failure_creates_retry_queue_when_device_has_none(client, auth_headers, api_module, mocker):
    # deliberately omit _retry_queue entirely, unlike test_cmd_failure_queues_retry
    # (which pre-seeds an empty list) — this hits the "not in device" branch
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/ac1.local/cmd", headers=auth_headers, json={"params": {"mode": "COOL"}})
    assert r.status_code == 200
    device = api_module._state["devices"][0]
    assert device["_retry_queue"] == [{"mode": "COOL"}]


# ── system.py: malformed _last_seen / worker_last_poll ────────


def test_health_push_tolerates_malformed_last_seen(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room", "_stale": False,
        "_last_seen": "not-a-valid-timestamp",
    })
    r = client.get("/health/push", headers=auth_headers)
    assert r.status_code == 200
    device_entry = r.json()["devices"][0]
    assert device_entry["minutes_since_seen"] is None  # parse failed, gracefully


def test_health_detailed_tolerates_malformed_last_seen(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room", "_stale": False,
        "_last_seen": "not-a-valid-timestamp",
    })
    r = client.get("/health", headers=auth_headers)
    assert r.status_code == 200
    device_entry = r.json()["devices"][0]
    assert device_entry["minutes_since_seen"] is None


def test_health_detailed_tolerates_malformed_worker_last_poll(client, auth_headers, api_module):
    # a device with a well-formed _last_seen so it becomes worker_last_poll,
    # but then corrupt it via a monkeypatch-free trick: seed a value that
    # parses as a valid last_seen for the device row but fails the second
    # time it's parsed as worker_last_poll is not realistic — instead,
    # directly exercise the branch by making the *max* of last_seen values
    # be a malformed string mixed with a valid one.
    api_module._state["devices"].append({
        "host": "ac1.local", "name": "A", "_stale": False,
        "_last_seen": "zzzz-not-a-real-timestamp",
    })
    r = client.get("/health", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["worker_stale"] is False  # exception caught, defaulted


# ── usage.py: export-csv skips non-matching days ──────────────


def test_export_csv_only_includes_matching_month(client, auth_headers, api_module):
    usage = api_module._state["usage"]
    usage["devices"]["ac1.local"] = {"name": "Living Room"}
    usage["daily"]["2026-01-15"] = {"ac1.local": {
        "runtime_minutes": 60, "est_kwh": 0.5, "peak_watts": 800, "snapshots": 3,
    }}
    usage["daily"]["2026-02-15"] = {"ac1.local": {  # different month, should be skipped
        "runtime_minutes": 30, "est_kwh": 0.2, "peak_watts": 700, "snapshots": 2,
    }}

    r = client.get("/usage/export-csv", headers=auth_headers, params={"month": "2026-01"})
    text = r.text
    lines = [l for l in text.strip().split("\r\n") if l]
    assert len(lines) == 2  # header + one matching data row
    assert "2026-01-15" in text
    assert "2026-02-15" not in text
