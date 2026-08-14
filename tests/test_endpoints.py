"""Integration tests hitting real FastAPI endpoints through TestClient.
Device network calls are mocked (see conftest.mock_device_response),
so these tests never touch a real AC dongle.
"""


def test_root_status(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_root_status_git_sha_defaults_to_unknown_without_env_var(client, monkeypatch):
    monkeypatch.delenv("GIT_SHA", raising=False)
    r = client.get("/")
    assert r.json()["git_sha"] == "unknown"


def test_root_status_git_sha_truncated_to_short_form(client, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "a1b2c3d4e5f6789012345678901234567890abcd")
    r = client.get("/")
    assert r.json()["git_sha"] == "a1b2c3d"


def test_health_endpoint_with_no_devices(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    # zero devices configured is intentionally reported as "degraded" —
    # useful signal to an uptime monitor that the deployment has no
    # devices set up yet / lost its config
    assert body["status"] == "degraded"


def test_health_endpoint_is_ok_once_a_device_is_healthy(client, auth_headers, mock_device_response):
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Living Room"})
    # force a poll so the device has a non-stale _last_seen
    client.post("/devices/ac1.local/poll", headers=auth_headers)
    r = client.get("/health")
    body = r.json()
    assert body["total"] == 1
    assert body["status"] == "ok"


# ── Auth flow ────────────────────────────────────────────────


def test_first_login_forces_password_change(client):
    r = client.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "admin"
    assert body["must_change_password"] is True


def test_login_wrong_password_fails(client):
    r = client.post("/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_login_unknown_user_fails(client):
    r = client.post("/auth/login", json={"username": "nosuchuser", "password": "whatever"})
    assert r.status_code == 401


def test_repeated_failed_logins_eventually_locked(client):
    for _ in range(5):
        client.post("/auth/login", json={"username": "admin", "password": "wrong"})
    # the 6th attempt (even with correct creds) should now be rate-limited
    r = client.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 429


def test_change_password_then_relogin(client, auth_headers):
    r = client.post(
        "/auth/change-password",
        headers=auth_headers,
        json={"old_password": "admin", "new_password": "newpassword123"},
    )
    assert r.status_code == 200

    # old password should no longer work
    r = client.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 401

    # new password should work and no longer force a change
    r = client.post("/auth/login", json={"username": "admin", "password": "newpassword123"})
    assert r.status_code == 200
    assert r.json()["must_change_password"] is False


def test_recovery_key_resets_admin_password(client, api_module):
    recovery_key = api_module._state["_recovery_key"]
    r = client.post(
        "/auth/recover",
        json={"recovery_key": recovery_key, "new_password": "recovered123"},
    )
    assert r.status_code == 200

    r = client.post("/auth/login", json={"username": "admin", "password": "recovered123"})
    assert r.status_code == 200


def test_recovery_key_wrong_key_rejected(client):
    r = client.post(
        "/auth/recover",
        json={"recovery_key": "totally-wrong-key", "new_password": "whatever123"},
    )
    assert r.status_code == 403


# ── Role enforcement ─────────────────────────────────────────


def test_viewer_cannot_add_device(client, auth_headers):
    r = client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "watcher", "password": "watcherpass", "role": "viewer"},
    )
    assert r.status_code == 200

    r = client.post("/auth/login", json={"username": "watcher", "password": "watcherpass"})
    viewer_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    r = client.post(
        "/devices", headers=viewer_headers,
        json={"host": "ac1.local", "name": "Living Room"},
    )
    assert r.status_code == 403


def test_operator_can_send_command_but_not_add_device(client, auth_headers, mock_device_response):
    r = client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "op", "password": "operatorpass", "role": "operator"},
    )
    assert r.status_code == 200
    r = client.post("/auth/login", json={"username": "op", "password": "operatorpass"})
    op_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    # operator cannot add a device (admin-only)
    r = client.post(
        "/devices", headers=op_headers,
        json={"host": "ac1.local", "name": "Living Room"},
    )
    assert r.status_code == 403

    # but admin adds it, then operator CAN send a command to it
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Living Room"})
    r = client.post(
        "/devices/ac1.local/cmd", headers=op_headers,
        json={"params": {"mode": "COOL"}},
    )
    assert r.status_code == 200


# ── Device CRUD ──────────────────────────────────────────────


def test_add_and_list_device(client, auth_headers):
    r = client.post(
        "/devices", headers=auth_headers,
        json={"host": "ac1.local", "name": "Living Room", "btu": 18000},
    )
    assert r.status_code == 200
    assert r.json()["new"] is True

    r = client.get("/devices", headers=auth_headers)
    devices = r.json()["devices"]
    assert len(devices) == 1
    assert devices[0]["name"] == "Living Room"


def test_add_device_has_ir_emitter_defaults_false(client, auth_headers):
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Living Room"})
    r = client.get("/devices", headers=auth_headers)
    assert r.json()["devices"][0]["has_ir_emitter"] is False


def test_add_device_has_ir_emitter_can_be_set_true(client, auth_headers):
    client.post(
        "/devices", headers=auth_headers,
        json={"host": "ac1.local", "name": "Living Room", "has_ir_emitter": True},
    )
    r = client.get("/devices", headers=auth_headers)
    assert r.json()["devices"][0]["has_ir_emitter"] is True


def test_adding_same_host_twice_updates_not_duplicates(client, auth_headers):
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "First Name"})
    r = client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Renamed"})
    assert r.json()["new"] is False

    r = client.get("/devices", headers=auth_headers)
    devices = r.json()["devices"]
    assert len(devices) == 1
    assert devices[0]["name"] == "Renamed"


def test_delete_device(client, auth_headers):
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Living Room"})
    r = client.delete("/devices/ac1.local", headers=auth_headers)
    assert r.status_code == 200
    r = client.get("/devices", headers=auth_headers)
    assert r.json()["devices"] == []


# ── Schedules ─────────────────────────────────────────────────


def test_create_schedule(client, auth_headers):
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Living Room"})
    r = client.post(
        "/schedules", headers=auth_headers,
        json={
            "device_host": "ac1.local", "device_name": "Living Room",
            "time": "07:00", "days": [0, 1, 2, 3, 4],
            "power": "on", "mode": "COOL", "temp": 24,
        },
    )
    assert r.status_code == 200
    assert r.json()["warnings"] == []


def test_create_conflicting_schedule_returns_warning(client, auth_headers):
    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Living Room"})
    client.post(
        "/schedules", headers=auth_headers,
        json={
            "device_host": "ac1.local", "device_name": "Living Room",
            "time": "07:00", "days": [1], "power": "on", "mode": "COOL", "temp": 24,
        },
    )
    r = client.post(
        "/schedules", headers=auth_headers,
        json={
            "device_host": "ac1.local", "device_name": "Living Room",
            "time": "07:00", "days": [1], "power": "on", "mode": "HEAT", "temp": 20,
        },
    )
    assert len(r.json()["warnings"]) == 1
