"""Tests for GET /backup and POST /restore.

Three different merge strategies live in restore() and each needs its
own coverage:
  - devices:   merged by host (update existing, append new with defaults)
  - schedules: fully replaced (not merged)
  - settings:  merged (dict.update — untouched keys survive)

backup() also has a subtlety worth pinning down: it reads from disk via
_load_raw(), not from the in-memory _state dict directly. In normal API
usage these stay in sync because every mutating endpoint saves before
returning, but it's worth a test making that assumption explicit.
"""


def test_backup_excludes_internal_fields(client, auth_headers):
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Living Room"})
    # force a poll-less internal field to exist by hitting update_device,
    # which the API populates with runtime fields like _stale, _retry_queue, etc.
    r = client.get("/backup", headers=auth_headers)
    assert r.status_code == 200
    devices = r.json()["devices"]
    assert len(devices) == 1
    for key in devices[0]:
        assert not key.startswith("_"), f"internal field '{key}' leaked into backup export"


def test_backup_never_includes_users(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "somepassword", "role": "viewer"},
    )
    r = client.get("/backup", headers=auth_headers)
    assert "users" not in r.json()


def test_backup_includes_exported_at_timestamp(client, auth_headers):
    r = client.get("/backup", headers=auth_headers)
    assert "exported_at" in r.json()
    assert r.json()["exported_at"]  # non-empty


def test_backup_reflects_persisted_state(client, auth_headers):
    """backup() reads from disk (_load_raw), not in-memory _state directly.
    Since every mutating endpoint saves before returning, a device added
    through the API should always show up in the next backup."""
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Living Room"})
    r = client.get("/backup", headers=auth_headers)
    names = [d["name"] for d in r.json()["devices"]]
    assert "Living Room" in names


# ── Restore: devices (merge by host) ────────────────────────


def test_restore_updates_existing_device_by_host(client, auth_headers):
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Old Name", "btu": 12000})
    r = client.post(
        "/restore", headers=auth_headers,
        json={"devices": [{"host": "ac1.local", "name": "New Name", "btu": 18000}]},
    )
    assert r.status_code == 200

    devices = client.get("/devices", headers=auth_headers).json()["devices"]
    assert len(devices) == 1
    assert devices[0]["name"] == "New Name"
    assert devices[0]["btu"] == 18000


def test_restore_adds_new_device_with_defaults_applied(client, auth_headers):
    r = client.post(
        "/restore", headers=auth_headers,
        json={"devices": [{"host": "ac2.local", "name": "Guest Room"}]},
    )
    assert r.status_code == 200

    devices = client.get("/devices", headers=auth_headers).json()["devices"]
    assert len(devices) == 1
    # DEVICE_DEFAULTS should fill in fields not present in the restore payload
    assert devices[0]["beeper"] == "OFF"
    assert devices[0]["watchdog_minutes"] == 5


def test_restore_devices_does_not_remove_devices_absent_from_payload(client, auth_headers):
    """Restore merges devices in, it doesn't prune ones missing from the backup."""
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Kept"})
    client.post(
        "/restore", headers=auth_headers,
        json={"devices": [{"host": "ac2.local", "name": "Added"}]},
    )
    devices = client.get("/devices", headers=auth_headers).json()["devices"]
    names = {d["name"] for d in devices}
    assert names == {"Kept", "Added"}


# ── Restore: schedules (full replace) ───────────────────────


def test_restore_schedules_fully_replaces_not_merges(client, auth_headers):
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Living Room"})
    client.post(
        "/schedules", headers=auth_headers,
        json={
            "device_host": "ac1.local", "device_name": "Living Room",
            "time": "07:00", "days": [0, 1, 2, 3, 4],
            "power": "on", "mode": "COOL", "temp": 24,
        },
    )
    original_schedules = client.get("/schedules", headers=auth_headers).json()["schedules"]
    assert len(original_schedules) == 1

    # restore with a completely different schedule list
    replacement = [{
        "id": "restored1", "device_host": "ac1.local", "device_name": "Living Room",
        "time": "21:00", "end_time": None, "days": [5, 6],
        "power": "off", "mode": None, "temp": None, "enabled": True,
    }]
    r = client.post("/restore", headers=auth_headers, json={"schedules": replacement})
    assert r.status_code == 200

    schedules = client.get("/schedules", headers=auth_headers).json()["schedules"]
    assert len(schedules) == 1
    assert schedules[0]["id"] == "restored1"
    assert schedules[0]["time"] == "21:00"


def test_restore_empty_schedules_list_clears_all_schedules(client, auth_headers):
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Living Room"})
    client.post(
        "/schedules", headers=auth_headers,
        json={
            "device_host": "ac1.local", "device_name": "Living Room",
            "time": "07:00", "days": [0], "power": "on", "mode": "COOL", "temp": 24,
        },
    )
    assert len(client.get("/schedules", headers=auth_headers).json()["schedules"]) == 1

    client.post("/restore", headers=auth_headers, json={"schedules": []})
    assert client.get("/schedules", headers=auth_headers).json()["schedules"] == []


# ── Restore: settings (dict merge) ──────────────────────────


def test_restore_settings_merges_leaves_other_keys_untouched(client, auth_headers):
    client.put("/settings", headers=auth_headers, json={"poll_interval": 90, "provider": "TestCo"})
    r = client.post("/restore", headers=auth_headers, json={"settings": {"poll_interval": 180}})
    assert r.status_code == 200

    settings = client.get("/settings", headers=auth_headers).json()
    assert settings["poll_interval"] == 180  # updated by restore
    assert settings["provider"] == "TestCo"  # untouched, survives the merge


# ── Role enforcement ─────────────────────────────────────────


def test_viewer_can_read_backup(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "watcher", "password": "watcherpass", "role": "viewer"},
    )
    r = client.post("/auth/login", json={"username": "watcher", "password": "watcherpass"})
    viewer_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    r = client.get("/backup", headers=viewer_headers)
    assert r.status_code == 200


def test_viewer_cannot_restore(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "watcher2", "password": "watcherpass", "role": "viewer"},
    )
    r = client.post("/auth/login", json={"username": "watcher2", "password": "watcherpass"})
    viewer_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    r = client.post("/restore", headers=viewer_headers, json={"settings": {"poll_interval": 60}})
    assert r.status_code == 403


# ── Full round trip ──────────────────────────────────────────


def test_full_backup_restore_round_trip(client, auth_headers):
    """Set up a realistic config, export it, blow away the state with
    something else, restore from the export, and confirm we're back to
    (functionally) where we started."""
    client.post("/devices", headers=auth_headers, json={
        "host": "ac1.local", "name": "Living Room", "btu": 18000, "seer": 20, "max_temp": 31.0,
    })
    client.post("/schedules", headers=auth_headers, json={
        "device_host": "ac1.local", "device_name": "Living Room",
        "time": "20:45", "end_time": "06:45", "days": [0, 1, 2, 3, 4],
        "power": "on", "mode": "COOL", "temp": 24,
    })
    client.put("/settings", headers=auth_headers, json={"provider": "Coopeguanacaste", "monthly_kwh": 350})

    backup_data = client.get("/backup", headers=auth_headers).json()

    # blow away current state with something unrelated
    client.delete("/devices/ac1.local", headers=auth_headers)
    client.post("/devices", headers=auth_headers, json={"host": "unrelated.local", "name": "Unrelated"})
    client.put("/settings", headers=auth_headers, json={"provider": "SomeoneElse", "monthly_kwh": 999})

    # restore from the earlier backup
    r = client.post("/restore", headers=auth_headers, json=backup_data)
    assert r.status_code == 200

    devices = client.get("/devices", headers=auth_headers).json()["devices"]
    device_hosts = {d["host"] for d in devices}
    assert "ac1.local" in device_hosts
    restored_device = next(d for d in devices if d["host"] == "ac1.local")
    assert restored_device["name"] == "Living Room"
    assert restored_device["btu"] == 18000
    assert restored_device["max_temp"] == 31.0
    # restore merges rather than prunes, so the unrelated device added
    # after the backup is still present — this is expected, not a bug
    assert "unrelated.local" in device_hosts

    schedules = client.get("/schedules", headers=auth_headers).json()["schedules"]
    assert len(schedules) == 1
    assert schedules[0]["time"] == "20:45"
    assert schedules[0]["end_time"] == "06:45"

    settings = client.get("/settings", headers=auth_headers).json()
    assert settings["provider"] == "Coopeguanacaste"
    assert settings["monthly_kwh"] == 350
