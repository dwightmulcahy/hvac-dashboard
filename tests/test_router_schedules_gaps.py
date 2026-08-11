"""Tests filling the remaining gaps in routers/schedules.py not
already covered by test_endpoints.py (create + conflict detection are
tested there) — update, delete, and toggle.
"""


def _add_schedule(client, auth_headers, **overrides):
    payload = {
        "device_host": "ac1.local", "device_name": "Living Room",
        "time": "07:00", "days": [0, 1, 2, 3, 4],
        "power": "on", "mode": "COOL", "temp": 24,
    }
    payload.update(overrides)
    r = client.post("/schedules", headers=auth_headers, json=payload)
    return r.json()["id"]


def test_update_schedule_changes_fields(client, auth_headers, api_module):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    sch_id = _add_schedule(client, auth_headers)

    r = client.put(
        f"/schedules/{sch_id}", headers=auth_headers,
        json={
            "device_host": "ac1.local", "device_name": "Living Room",
            "time": "21:00", "days": [0, 1, 2, 3, 4],
            "power": "off", "mode": None, "temp": None,
        },
    )
    assert r.status_code == 200
    updated = next(s for s in api_module._state["schedules"] if s["id"] == sch_id)
    assert updated["time"] == "21:00"
    assert updated["power"] == "off"


def test_update_schedule_not_found_returns_error(client, auth_headers):
    r = client.put(
        "/schedules/nonexistent", headers=auth_headers,
        json={"device_host": "ac1.local", "device_name": "X", "time": "07:00", "days": [0]},
    )
    assert r.json()["ok"] is False
    assert r.json()["error"] == "not found"


def test_update_schedule_reports_new_conflicts(client, auth_headers, api_module):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    _add_schedule(client, auth_headers, time="07:00", days=[1])  # schedule A, creates the conflict target
    id_b = _add_schedule(client, auth_headers, time="20:00", days=[2])  # no conflict yet

    # move schedule B to overlap with A
    r = client.put(
        f"/schedules/{id_b}", headers=auth_headers,
        json={
            "device_host": "ac1.local", "device_name": "Living Room",
            "time": "07:00", "days": [1],
            "power": "on", "mode": "HEAT", "temp": 20,
        },
    )
    assert len(r.json()["warnings"]) == 1


def test_delete_schedule_removes_it(client, auth_headers, api_module):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    sch_id = _add_schedule(client, auth_headers)
    r = client.delete(f"/schedules/{sch_id}", headers=auth_headers)
    assert r.status_code == 200
    assert api_module._state["schedules"] == []


def test_delete_nonexistent_schedule_is_a_noop(client, auth_headers):
    r = client.delete("/schedules/nonexistent", headers=auth_headers)
    assert r.status_code == 200  # no error, just nothing to remove


def test_toggle_schedule_flips_enabled(client, auth_headers, api_module):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    sch_id = _add_schedule(client, auth_headers)
    assert api_module._state["schedules"][0]["enabled"] is True

    r = client.post(f"/schedules/{sch_id}/toggle", headers=auth_headers)
    assert r.json()["ok"] is True
    assert r.json()["enabled"] is False
    assert api_module._state["schedules"][0]["enabled"] is False

    r2 = client.post(f"/schedules/{sch_id}/toggle", headers=auth_headers)
    assert r2.json()["enabled"] is True


def test_toggle_nonexistent_schedule_returns_ok_false(client, auth_headers):
    r = client.post("/schedules/nonexistent/toggle", headers=auth_headers)
    assert r.json()["ok"] is False
