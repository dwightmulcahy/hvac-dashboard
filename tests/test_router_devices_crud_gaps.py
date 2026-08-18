"""Tests filling the remaining gaps in routers/devices_crud.py not
already covered by test_endpoints.py (basic add/list/delete/duplicate-
host CRUD is tested there) — temp-history, reorder, and update_device
when the host itself changes (which clears retry queue / stale state).
"""


def test_temp_history_unknown_device_returns_empty(client, auth_headers):
    r = client.get("/devices/nonexistent.local/temp-history", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["history"] == []


def test_temp_history_returns_seeded_data(client, auth_headers, api_module):
    api_module._state["usage"]["temp_history"] = {
        "ac1.local": [{"t": "2026-01-01T12:00", "in": 24.0, "out": 31.0}]
    }
    r = client.get("/devices/ac1.local/temp-history", headers=auth_headers)
    assert len(r.json()["history"]) == 1
    assert r.json()["history"][0]["in"] == 24.0


def test_reorder_devices_applies_new_order(client, auth_headers, api_module):
    api_module._state["devices"] = [
        {"host": "a.local", "name": "A"},
        {"host": "b.local", "name": "B"},
        {"host": "c.local", "name": "C"},
    ]
    r = client.post("/devices/reorder", headers=auth_headers, json={"hosts": ["c.local", "a.local", "b.local"]})
    assert r.status_code == 200
    hosts = [d["host"] for d in api_module._state["devices"]]
    assert hosts == ["c.local", "a.local", "b.local"]


def test_reorder_appends_devices_missing_from_host_list(client, auth_headers, api_module):
    api_module._state["devices"] = [
        {"host": "a.local", "name": "A"},
        {"host": "b.local", "name": "B"},
    ]
    # only reference "a.local" — "b.local" should still survive, appended at the end
    r = client.post("/devices/reorder", headers=auth_headers, json={"hosts": ["a.local"]})
    assert r.status_code == 200
    hosts = [d["host"] for d in api_module._state["devices"]]
    assert hosts == ["a.local", "b.local"]


def test_update_device_host_change_clears_retry_state(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "old.local", "name": "Living Room", "btu": 24000, "seer": 20,
        "_retry_queue": [{"mode": "COOL"}], "_stale": True, "_consecutive_failures": 5,
    })
    api_module._state["device_state"]["old.local"] = {"mode": "COOL"}

    r = client.put(
        "/devices/old.local", headers=auth_headers,
        json={"host": "new.local", "name": "Living Room", "btu": 24000, "seer": 20},
    )
    assert r.status_code == 200

    device = api_module._state["devices"][0]
    assert device["_retry_queue"] == []
    assert device["_stale"] is False
    assert device["_consecutive_failures"] == 0
    assert "old.local" not in api_module._state["device_state"]
    logs = [l["msg"] for l in api_module._state["logs"]]
    assert any("host changed to new.local" in m for m in logs)


def test_update_device_host_change_repoints_matching_maintenance_items(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "old.local", "name": "Living Room", "btu": 24000, "seer": 20,
        "_on_time_minutes": 600.0,
    })
    api_module._state["maintenance"].append({
        "id": "m1", "name": "Coil clean", "device_host": "old.local",
        "trigger_type": "runtime_hours", "interval_hours": 500,
        "last_done_runtime_minutes": 0.0, "_notified_overdue": False,
        "completed_log": [],
    })
    # an item for a different device shouldn't be touched
    api_module._state["maintenance"].append({
        "id": "m2", "name": "Filters", "device_host": "other.local",
        "trigger_type": "days", "interval_days": 30,
        "_notified_overdue": False, "completed_log": [],
    })

    r = client.put(
        "/devices/old.local", headers=auth_headers,
        json={"host": "new.local", "name": "Living Room", "btu": 24000, "seer": 20},
    )
    assert r.status_code == 200

    m1 = next(i for i in api_module._state["maintenance"] if i["id"] == "m1")
    m2 = next(i for i in api_module._state["maintenance"] if i["id"] == "m2")
    assert m1["device_host"] == "new.local"
    assert m2["device_host"] == "other.local"  # unrelated item untouched
    logs = [l["msg"] for l in api_module._state["logs"]]
    assert any("1 maintenance reminder(s) repointed to new.local" in m for m in logs)


def test_update_device_host_change_repoints_multiple_maintenance_items(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "old.local", "name": "Living Room", "btu": 24000, "seer": 20,
    })
    api_module._state["maintenance"].append({
        "id": "m1", "name": "Coil clean", "device_host": "old.local",
        "trigger_type": "runtime_hours", "interval_hours": 500,
        "_notified_overdue": False, "completed_log": [],
    })
    api_module._state["maintenance"].append({
        "id": "m2", "name": "Filter check", "device_host": "old.local",
        "trigger_type": "days", "interval_days": 30,
        "_notified_overdue": False, "completed_log": [],
    })

    client.put(
        "/devices/old.local", headers=auth_headers,
        json={"host": "new.local", "name": "Living Room", "btu": 24000, "seer": 20},
    )

    for item_id in ("m1", "m2"):
        item = next(i for i in api_module._state["maintenance"] if i["id"] == item_id)
        assert item["device_host"] == "new.local"


def test_update_device_host_change_preserves_runtime_baseline_after_repoint(client, auth_headers, api_module):
    """The whole point of repointing: after the rename, the item's
    overdue countdown should continue from where it was, not silently
    reset to 'never done' because _device_on_time_minutes(old_host)
    stopped matching anything."""
    api_module._state["devices"].append({
        "host": "old.local", "name": "Living Room", "btu": 24000, "seer": 20,
        "_on_time_minutes": 600.0,  # 10 hours accumulated
    })
    api_module._state["maintenance"].append({
        "id": "m1", "name": "Coil clean", "device_host": "old.local",
        "trigger_type": "runtime_hours", "interval_hours": 5,
        "last_done_runtime_minutes": 0.0, "_notified_overdue": False,
        "completed_log": [],
    })

    client.put(
        "/devices/old.local", headers=auth_headers,
        json={"host": "new.local", "name": "Living Room", "btu": 24000, "seer": 20},
    )

    items = client.get("/maintenance", headers=auth_headers).json()["maintenance"]
    item = next(i for i in items if i["id"] == "m1")
    # 10 hours since baseline, 5-hour interval — still correctly overdue
    # after the rename, not reset to "0 hours since done"
    assert item["status"]["overdue"] is True
    assert item["status"]["hours_since_done"] == 10.0


def test_update_device_host_change_with_no_maintenance_items_logs_nothing_extra(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "old.local", "name": "Living Room", "btu": 24000, "seer": 20,
    })
    client.put(
        "/devices/old.local", headers=auth_headers,
        json={"host": "new.local", "name": "Living Room", "btu": 24000, "seer": 20},
    )
    logs = [l["msg"] for l in api_module._state["logs"]]
    assert not any("maintenance reminder(s) repointed" in m for m in logs)


def test_update_device_same_host_does_not_clear_retry_state(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room", "btu": 24000, "seer": 20,
        "_retry_queue": [{"mode": "COOL"}],
    })
    r = client.put(
        "/devices/ac1.local", headers=auth_headers,
        json={"host": "ac1.local", "name": "Renamed", "btu": 18000, "seer": 20},
    )
    assert r.status_code == 200
    device = api_module._state["devices"][0]
    assert device["_retry_queue"] == [{"mode": "COOL"}]  # untouched
    assert device["name"] == "Renamed"


def test_update_device_not_found_and_not_duplicate_adds_new(client, auth_headers, api_module):
    r = client.put(
        "/devices/nonexistent.local", headers=auth_headers,
        json={"host": "nonexistent.local", "name": "New Device", "btu": 24000, "seer": 20},
    )
    assert r.status_code == 200
    hosts = [d["host"] for d in api_module._state["devices"]]
    assert "nonexistent.local" in hosts
