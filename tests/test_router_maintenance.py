"""Tests for routers/maintenance.py: CRUD, days-based and
runtime_hours-based status computation, /complete (including the
append-only completed_log service history), and operator-vs-admin
role gating.
"""

import datetime


def _utcnow():
    """Local stand-in for state._utcnow() — api_module doesn't re-export
    it, and pulling in a second `state` import here would fight with
    the api_module fixture's own module-reload lifecycle."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# ── CRUD basics ──────────────────────────────────────────────


def test_add_maintenance_defaults_and_returns_id(client, auth_headers):
    r = client.post("/maintenance", headers=auth_headers, json={"name": "Change filters", "interval_days": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["id"]


def test_get_maintenance_lists_items_with_status(client, auth_headers):
    client.post("/maintenance", headers=auth_headers, json={"name": "Annual service", "interval_days": 365})
    r = client.get("/maintenance", headers=auth_headers)
    assert r.status_code == 200
    items = r.json()["maintenance"]
    assert len(items) == 1
    assert items[0]["name"] == "Annual service"
    assert "status" in items[0]
    assert "overdue" in items[0]["status"]


def test_update_maintenance_edits_fields_without_wiping_history(client, auth_headers):
    add = client.post("/maintenance", headers=auth_headers, json={"name": "Filters", "interval_days": 30})
    item_id = add.json()["id"]
    client.post(f"/maintenance/{item_id}/complete", headers=auth_headers)

    r = client.put(f"/maintenance/{item_id}", headers=auth_headers, json={"name": "Filters (renamed)", "interval_days": 45})
    assert r.status_code == 200

    items = client.get("/maintenance", headers=auth_headers).json()["maintenance"]
    item = next(i for i in items if i["id"] == item_id)
    assert item["name"] == "Filters (renamed)"
    assert item["interval_days"] == 45
    assert len(item["completed_log"]) == 1  # not wiped by the edit


def test_update_maintenance_missing_item_returns_error(client, auth_headers):
    r = client.put("/maintenance/does-not-exist", headers=auth_headers, json={"name": "X", "interval_days": 1})
    assert r.json()["ok"] is False


def test_delete_maintenance_removes_item(client, auth_headers):
    add = client.post("/maintenance", headers=auth_headers, json={"name": "Filters", "interval_days": 30})
    item_id = add.json()["id"]
    r = client.delete(f"/maintenance/{item_id}", headers=auth_headers)
    assert r.status_code == 200
    items = client.get("/maintenance", headers=auth_headers).json()["maintenance"]
    assert not any(i["id"] == item_id for i in items)


# ── Status computation: days-based ──────────────────────────


def test_days_based_item_not_overdue_when_recently_done(client, auth_headers):
    now_iso = _utcnow().isoformat()
    add = client.post("/maintenance", headers=auth_headers, json={
        "name": "Filters", "interval_days": 30,
        "last_done_at": now_iso,
    })
    items = client.get("/maintenance", headers=auth_headers).json()["maintenance"]
    item = next(i for i in items if i["id"] == add.json()["id"])
    assert item["status"]["overdue"] is False


def test_days_based_item_overdue_when_interval_elapsed(client, auth_headers):
    long_ago = (_utcnow() - datetime.timedelta(days=100)).isoformat()
    add = client.post("/maintenance", headers=auth_headers, json={
        "name": "Filters", "interval_days": 30, "last_done_at": long_ago,
    })
    items = client.get("/maintenance", headers=auth_headers).json()["maintenance"]
    item = next(i for i in items if i["id"] == add.json()["id"])
    assert item["status"]["overdue"] is True
    assert item["status"]["days_remaining"] < 0


# ── Status computation: runtime_hours-based ─────────────────


def test_runtime_hours_item_overdue_based_on_device_on_time(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room", "_on_time_minutes": 0.0,
    })
    add = client.post("/maintenance", headers=auth_headers, json={
        "name": "Coil clean", "device_host": "ac1.local",
        "trigger_type": "runtime_hours", "interval_hours": 5,
    })
    item_id = add.json()["id"]
    # baseline is snapshotted to the device's on-time *at creation* (0.0
    # here), so simulate 10 hours of runtime accruing afterward
    device = next(d for d in api_module._state["devices"] if d["host"] == "ac1.local")
    device["_on_time_minutes"] = 600.0  # 10 hours

    items = client.get("/maintenance", headers=auth_headers).json()["maintenance"]
    item = next(i for i in items if i["id"] == item_id)
    assert item["status"]["overdue"] is True
    assert item["status"]["hours_since_done"] == 10.0


def test_runtime_hours_item_not_overdue_before_interval(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room", "_on_time_minutes": 0.0,
    })
    add = client.post("/maintenance", headers=auth_headers, json={
        "name": "Coil clean", "device_host": "ac1.local",
        "trigger_type": "runtime_hours", "interval_hours": 5,
    })
    item_id = add.json()["id"]
    device = next(d for d in api_module._state["devices"] if d["host"] == "ac1.local")
    device["_on_time_minutes"] = 60.0  # 1 hour since creation

    items = client.get("/maintenance", headers=auth_headers).json()["maintenance"]
    item = next(i for i in items if i["id"] == item_id)
    assert item["status"]["overdue"] is False


# ── /complete and service history ───────────────────────────


def test_complete_updates_last_done_at_and_appends_history(client, auth_headers):
    add = client.post("/maintenance", headers=auth_headers, json={"name": "Filters", "interval_days": 30})
    item_id = add.json()["id"]

    r = client.post(f"/maintenance/{item_id}/complete", headers=auth_headers)
    assert r.status_code == 200

    items = client.get("/maintenance", headers=auth_headers).json()["maintenance"]
    item = next(i for i in items if i["id"] == item_id)
    assert len(item["completed_log"]) == 1
    assert item["completed_log"][0]["completed_at"] == item["last_done_at"]


def test_complete_missing_item_returns_error(client, auth_headers):
    r = client.post("/maintenance/does-not-exist/complete", headers=auth_headers)
    assert r.json()["ok"] is False


def test_complete_records_runtime_hours_for_device_scoped_item(client, auth_headers, api_module):
    api_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room", "_on_time_minutes": 300.0,  # 5 hours
    })
    add = client.post("/maintenance", headers=auth_headers, json={
        "name": "Coil clean", "device_host": "ac1.local", "trigger_type": "runtime_hours", "interval_hours": 100,
    })
    item_id = add.json()["id"]
    client.post(f"/maintenance/{item_id}/complete", headers=auth_headers)

    items = client.get("/maintenance", headers=auth_headers).json()["maintenance"]
    item = next(i for i in items if i["id"] == item_id)
    assert item["completed_log"][0]["runtime_hours_at_completion"] == 5.0


def test_complete_clears_notified_overdue_flag(client, auth_headers, api_module):
    long_ago = (_utcnow() - datetime.timedelta(days=100)).isoformat()
    add = client.post("/maintenance", headers=auth_headers, json={
        "name": "Filters", "interval_days": 30, "last_done_at": long_ago,
    })
    item_id = add.json()["id"]
    item = next(i for i in api_module._state["maintenance"] if i["id"] == item_id)
    item["_notified_overdue"] = True  # simulate the worker having already notified

    client.post(f"/maintenance/{item_id}/complete", headers=auth_headers)
    assert item["_notified_overdue"] is False


def test_completed_log_capped_at_50_entries(client, auth_headers, api_module):
    add = client.post("/maintenance", headers=auth_headers, json={"name": "Filters", "interval_days": 30})
    item_id = add.json()["id"]
    item = next(i for i in api_module._state["maintenance"] if i["id"] == item_id)
    item["completed_log"] = [{"completed_at": "x", "runtime_hours_at_completion": None}] * 50

    client.post(f"/maintenance/{item_id}/complete", headers=auth_headers)
    assert len(item["completed_log"]) == 50


# ── Role gating ──────────────────────────────────────────────


def test_operator_can_complete_maintenance(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "op", "password": "operatorpass", "role": "operator"},
    )
    add = client.post("/maintenance", headers=auth_headers, json={"name": "Filters", "interval_days": 30})
    item_id = add.json()["id"]

    r = client.post("/auth/login", json={"username": "op", "password": "operatorpass"})
    op_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    r = client.post(f"/maintenance/{item_id}/complete", headers=op_headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_operator_cannot_create_maintenance_item(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "op2", "password": "operatorpass", "role": "operator"},
    )
    r = client.post("/auth/login", json={"username": "op2", "password": "operatorpass"})
    op_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    r = client.post("/maintenance", headers=op_headers, json={"name": "Filters", "interval_days": 30})
    assert r.status_code == 403


def test_operator_cannot_delete_maintenance_item(client, auth_headers):
    add = client.post("/maintenance", headers=auth_headers, json={"name": "Filters", "interval_days": 30})
    item_id = add.json()["id"]
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "op3", "password": "operatorpass", "role": "operator"},
    )
    r = client.post("/auth/login", json={"username": "op3", "password": "operatorpass"})
    op_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    r = client.delete(f"/maintenance/{item_id}", headers=op_headers)
    assert r.status_code == 403


def test_viewer_cannot_complete_maintenance(client, auth_headers):
    add = client.post("/maintenance", headers=auth_headers, json={"name": "Filters", "interval_days": 30})
    item_id = add.json()["id"]
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "viewer1", "password": "viewerpass", "role": "viewer"},
    )
    r = client.post("/auth/login", json={"username": "viewer1", "password": "viewerpass"})
    viewer_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    r = client.post(f"/maintenance/{item_id}/complete", headers=viewer_headers)
    assert r.status_code == 403
