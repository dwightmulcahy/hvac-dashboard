"""Tests filling the remaining gaps in auth.py not already covered by
test_auth.py (unit tests of the core functions) and test_endpoints.py
(the main login/user-management flows).
"""

import datetime

# ── _check_login_lockout: expiry ─────────────────────────────


def test_login_lockout_expires_after_lockout_period(auth_module):
    for _ in range(auth_module.LOGIN_MAX_ATTEMPTS):
        auth_module._record_login_failure("admin", "1.2.3.4")
    assert auth_module._check_login_lockout("admin", "1.2.3.4") is not None

    # simulate the lockout window having passed
    key = auth_module._login_key("admin", "1.2.3.4")
    past = auth_module._utcnow() - datetime.timedelta(minutes=1)
    auth_module._login_attempts[key]["locked_until"] = past.isoformat()

    assert auth_module._check_login_lockout("admin", "1.2.3.4") is None
    # expired lockout entry should also be cleaned up
    assert key not in auth_module._login_attempts


def test_login_failure_window_resets_after_attempt_window_expires(auth_module):
    key = auth_module._login_key("admin", "1.2.3.4")
    old_time = auth_module._utcnow() - datetime.timedelta(minutes=auth_module.LOGIN_ATTEMPT_WINDOW_MINUTES + 1)
    auth_module._login_attempts[key] = {"failures": 4, "first_attempt": old_time.isoformat()}

    auth_module._record_login_failure("admin", "1.2.3.4")
    # window had expired, so this should be treated as failure #1, not #5
    assert auth_module._login_attempts[key]["failures"] == 1


# ── _create_token / _get_token_info: expiry ──────────────────


def test_create_token_cleans_up_expired_tokens(auth_module):
    expired_token = "expired-token-123"
    past = (auth_module._utcnow() - datetime.timedelta(hours=1)).isoformat()
    auth_module._tokens[expired_token] = {"username": "x", "role": "viewer", "expires": past}

    auth_module._create_token("admin", "admin")
    assert expired_token not in auth_module._tokens


def test_get_token_info_deletes_and_returns_none_for_expired_token(auth_module):
    token = "some-token"
    past = (auth_module._utcnow() - datetime.timedelta(hours=1)).isoformat()
    auth_module._tokens[token] = {"username": "x", "role": "viewer", "expires": past}

    info = auth_module._get_token_info(f"Bearer {token}")
    assert info is None
    assert token not in auth_module._tokens


# ── auth_middleware branches ──────────────────────────────────


def test_middleware_allows_everything_when_no_users_configured(client, api_module):
    # _ensure_default_admin() always runs during lifespan startup, so
    # the app is never really in a "no users" state via the normal
    # client fixture — simulate it directly to test the branch itself.
    api_module._state["users"] = {}
    r = client.post("/devices", json={"host": "ac1.local", "name": "Test"})
    assert r.status_code == 200


def test_middleware_rejects_missing_auth_once_users_exist(client, auth_headers):
    # auth_headers fixture triggers admin creation as a side effect of logging in
    r = client.post("/devices", json={"host": "ac1.local", "name": "Test"})  # no headers
    assert r.status_code == 401


def test_middleware_viewer_cannot_use_operator_action(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "watcher", "password": "watcherpass", "role": "viewer"},
    )
    r = client.post("/auth/login", json={"username": "watcher", "password": "watcherpass"})
    viewer_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    client.post("/devices", headers=auth_headers, json={"host": "ac1.local", "name": "Test"})
    r = client.post("/devices/ac1.local/cmd", headers=viewer_headers, json={"params": {"mode": "COOL"}})
    assert r.status_code == 403


def test_middleware_operator_can_use_operator_action_but_not_admin_action(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "op", "password": "operatorpass", "role": "operator"},
    )
    r = client.post("/auth/login", json={"username": "op", "password": "operatorpass"})
    op_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    # operator-level action on vacation mode should succeed
    r2 = client.post("/vacation/on", headers=op_headers)
    assert r2.status_code == 200

    # admin-only action should still be denied
    r3 = client.put("/settings", headers=op_headers, json={"poll_interval": 60})
    assert r3.status_code == 403


# ── /auth/recover: password too short ────────────────────────


def test_recover_rejects_short_new_password(client, api_module):
    key = api_module._state["_recovery_key"]
    r = client.post("/auth/recover", json={"recovery_key": key, "new_password": "short"})
    assert r.status_code == 400


# ── /auth/logout ──────────────────────────────────────────────


def test_logout_invalidates_token(client, auth_headers):
    r = client.post("/auth/logout", headers=auth_headers)
    assert r.status_code == 200
    # token should no longer work
    r2 = client.get("/auth/me", headers=auth_headers)
    assert r2.status_code == 401


def test_logout_without_token_is_a_noop(client):
    r = client.post("/auth/logout")
    assert r.status_code == 200


# ── /auth/change-password: failure paths ─────────────────────


def test_change_password_rejects_short_new_password(client, auth_headers):
    r = client.post(
        "/auth/change-password", headers=auth_headers,
        json={"old_password": "admin", "new_password": "short"},
    )
    assert r.status_code == 400


def test_change_password_rejects_wrong_old_password(client, auth_headers):
    r = client.post(
        "/auth/change-password", headers=auth_headers,
        json={"old_password": "wrongpassword", "new_password": "newpassword123"},
    )
    assert r.status_code == 401


# ── /auth/users: validation paths ────────────────────────────


def test_add_user_rejects_short_password(client, auth_headers):
    r = client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "short", "role": "viewer"},
    )
    assert r.status_code == 400


def test_add_user_rejects_invalid_role(client, auth_headers):
    r = client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "longenough123", "role": "superadmin"},
    )
    assert r.status_code == 400


def test_add_user_rejects_duplicate_username(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "longenough123", "role": "viewer"},
    )
    r = client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "differentpass1", "role": "viewer"},
    )
    assert r.status_code == 409


def test_list_users_returns_all(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "longenough123", "role": "viewer"},
    )
    r = client.get("/auth/users", headers=auth_headers)
    usernames = [u["username"] for u in r.json()["users"]]
    assert "admin" in usernames
    assert "someone" in usernames


def test_get_recovery_key_admin_can_view(client, auth_headers, api_module):
    r = client.get("/auth/recovery-key", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["recovery_key"] == api_module._state["_recovery_key"]


def test_get_recovery_key_operator_forbidden(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "op", "password": "operatorpass", "role": "operator"},
    )
    r = client.post("/auth/login", json={"username": "op", "password": "operatorpass"})
    op_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    r = client.get("/auth/recovery-key", headers=op_headers)
    assert r.status_code == 403


def test_get_recovery_key_viewer_forbidden(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "view", "password": "viewerpass", "role": "viewer"},
    )
    r = client.post("/auth/login", json={"username": "view", "password": "viewerpass"})
    viewer_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    r = client.get("/auth/recovery-key", headers=viewer_headers)
    assert r.status_code == 403


def test_get_recovery_key_unauthenticated_forbidden(client, auth_headers):
    # once real users exist, an unauthenticated request must be rejected
    r = client.get("/auth/recovery-key")
    assert r.status_code == 401


def test_get_recovery_key_reflects_regeneration_after_recover(client, auth_headers, api_module):
    """A key retrieved before a successful /auth/recover use must not
    still be valid afterward — recover() calls generate_recovery_key()
    again, and this endpoint should always reflect the current one."""
    old_key = client.get("/auth/recovery-key", headers=auth_headers).json()["recovery_key"]

    client.post("/auth/recover", json={"recovery_key": old_key, "new_password": "recovered123"})

    new_key = api_module._state["_recovery_key"]
    assert new_key != old_key


# ── "Remember me" ─────────────────────────────────────────────


def test_remember_mints_a_token_for_the_current_user(client, auth_headers, api_module):
    r = client.post("/auth/remember", headers=auth_headers)
    assert r.status_code == 200
    raw = r.json()["remember_token"]
    assert raw

    records = api_module._state["users"]["admin"].get("remember_tokens", [])
    assert len(records) == 1
    # the raw token is never stored — only its hash
    assert records[0]["token_hash"] != raw


def test_remember_requires_auth(client):
    r = client.post("/auth/remember")
    assert r.status_code == 401


def test_login_remember_exchanges_token_for_a_real_session(client, auth_headers):
    raw = client.post("/auth/remember", headers=auth_headers).json()["remember_token"]

    r = client.post("/auth/login-remember", json={"remember_token": raw})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "admin"
    assert body["role"] == "admin"
    assert body["token"]
    assert body["remember_token"]

    # the returned session token is a genuinely working session
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    assert me.json()["username"] == "admin"


def test_login_remember_rotates_the_token_single_use(client, auth_headers):
    raw = client.post("/auth/remember", headers=auth_headers).json()["remember_token"]

    first = client.post("/auth/login-remember", json={"remember_token": raw})
    assert first.status_code == 200
    new_raw = first.json()["remember_token"]
    assert new_raw != raw

    # the original (now-spent) token no longer works
    replay = client.post("/auth/login-remember", json={"remember_token": raw})
    assert replay.status_code == 401

    # but the newly-issued one does
    second = client.post("/auth/login-remember", json={"remember_token": new_raw})
    assert second.status_code == 200


def test_login_remember_rejects_unknown_token(client):
    r = client.post("/auth/login-remember", json={"remember_token": "totally-made-up"})
    assert r.status_code == 401


def test_login_remember_rejects_missing_token(client):
    r = client.post("/auth/login-remember", json={})
    assert r.status_code == 401


def test_login_remember_rejects_expired_token(client, auth_headers, api_module, monkeypatch):
    raw = client.post("/auth/remember", headers=auth_headers).json()["remember_token"]
    # force the stored record into the past
    record = api_module._state["users"]["admin"]["remember_tokens"][0]
    record["expires"] = "2000-01-01T00:00:00"

    r = client.post("/auth/login-remember", json={"remember_token": raw})
    assert r.status_code == 401


def test_change_password_revokes_all_remember_tokens(client, auth_headers, api_module):
    client.post("/auth/remember", headers=auth_headers)
    client.post("/auth/remember", headers=auth_headers)
    assert len(api_module._state["users"]["admin"]["remember_tokens"]) == 2

    client.post("/auth/change-password", headers=auth_headers,
                json={"old_password": "admin", "new_password": "newpassword123"})

    assert api_module._state["users"]["admin"]["remember_tokens"] == []


def test_force_reset_revokes_all_remember_tokens(client, auth_headers, api_module):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "longenough123", "role": "viewer"},
    )
    r = client.post("/auth/login", json={"username": "someone", "password": "longenough123"})
    someone_headers = {"Authorization": f"Bearer {r.json()['token']}"}
    client.post("/auth/remember", headers=someone_headers)
    assert len(api_module._state["users"]["someone"]["remember_tokens"]) == 1

    client.post("/auth/users/someone/force-reset", headers=auth_headers)

    assert api_module._state["users"]["someone"]["remember_tokens"] == []


def test_logout_with_remember_token_revokes_it(client, auth_headers, api_module):
    raw = client.post("/auth/remember", headers=auth_headers).json()["remember_token"]

    client.post("/auth/logout", headers=auth_headers, json={"remember_token": raw})

    assert api_module._state["users"]["admin"]["remember_tokens"] == []
    replay = client.post("/auth/login-remember", json={"remember_token": raw})
    assert replay.status_code == 401


def test_logout_without_remember_token_leaves_other_devices_remembered(client, auth_headers, api_module):
    raw = client.post("/auth/remember", headers=auth_headers).json()["remember_token"]

    # a plain logout (no remember_token in the body) shouldn't touch
    # any remembered devices at all — only an explicit one does
    client.post("/auth/logout", headers=auth_headers)

    assert len(api_module._state["users"]["admin"]["remember_tokens"]) == 1
    r = client.post("/auth/login-remember", json={"remember_token": raw})
    assert r.status_code == 200


def test_list_users_reports_remembered_device_count(client, auth_headers):
    client.post("/auth/remember", headers=auth_headers)
    client.post("/auth/remember", headers=auth_headers)

    r = client.get("/auth/users", headers=auth_headers)
    admin = next(u for u in r.json()["users"] if u["username"] == "admin")
    assert admin["remembered_devices"] == 2


def test_delete_user_implicitly_removes_their_remember_tokens(client, auth_headers, api_module):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "longenough123", "role": "viewer"},
    )
    r = client.post("/auth/login", json={"username": "someone", "password": "longenough123"})
    someone_headers = {"Authorization": f"Bearer {r.json()['token']}"}
    raw = client.post("/auth/remember", headers=someone_headers).json()["remember_token"]

    client.delete("/auth/users/someone", headers=auth_headers)

    assert "someone" not in api_module._state["users"]
    r = client.post("/auth/login-remember", json={"remember_token": raw})
    assert r.status_code == 401


def test_new_user_has_no_last_login_until_they_log_in(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "longenough123", "role": "viewer"},
    )
    r = client.get("/auth/users", headers=auth_headers)
    someone = next(u for u in r.json()["users"] if u["username"] == "someone")
    assert someone["last_login"] is None


def test_last_login_recorded_after_successful_login(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "longenough123", "role": "viewer"},
    )
    client.post("/auth/login", json={"username": "someone", "password": "longenough123"})

    r = client.get("/auth/users", headers=auth_headers)
    someone = next(u for u in r.json()["users"] if u["username"] == "someone")
    assert someone["last_login"] is not None


def test_last_login_not_recorded_on_failed_login(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "longenough123", "role": "viewer"},
    )
    client.post("/auth/login", json={"username": "someone", "password": "wrongpassword"})

    r = client.get("/auth/users", headers=auth_headers)
    someone = next(u for u in r.json()["users"] if u["username"] == "someone")
    assert someone["last_login"] is None


def test_last_login_updates_on_each_subsequent_login(client, auth_headers, api_module):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "longenough123", "role": "viewer"},
    )
    client.post("/auth/login", json={"username": "someone", "password": "longenough123"})

    # force a stale timestamp, then confirm a fresh login overwrites it
    api_module._state["users"]["someone"]["last_login"] = "2020-01-01T00:00:00"
    client.post("/auth/login", json={"username": "someone", "password": "longenough123"})
    updated_login = api_module._state["users"]["someone"]["last_login"]

    assert updated_login != "2020-01-01T00:00:00"


def test_delete_user_cannot_delete_self(client, auth_headers):
    r = client.delete("/auth/users/admin", headers=auth_headers)
    assert r.status_code == 400


def test_delete_user_not_found(client, auth_headers):
    r = client.delete("/auth/users/nonexistent", headers=auth_headers)
    assert r.status_code == 404


def test_delete_user_revokes_their_tokens(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "longenough123", "role": "viewer"},
    )
    login_r = client.post("/auth/login", json={"username": "someone", "password": "longenough123"})
    someone_headers = {"Authorization": f"Bearer {login_r.json()['token']}"}

    client.delete("/auth/users/someone", headers=auth_headers)

    r = client.get("/auth/me", headers=someone_headers)
    assert r.status_code == 401  # token revoked along with the user


def test_force_reset_sets_must_change_password(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "longenough123", "role": "viewer"},
    )
    r = client.post("/auth/users/someone/force-reset", headers=auth_headers)
    assert r.status_code == 200

    login_r = client.post("/auth/login", json={"username": "someone", "password": "longenough123"})
    assert login_r.json()["must_change_password"] is True


def test_force_reset_not_found(client, auth_headers):
    r = client.post("/auth/users/nonexistent/force-reset", headers=auth_headers)
    assert r.status_code == 404


def test_set_role_cannot_change_own_role(client, auth_headers):
    r = client.put("/auth/users/admin/role", headers=auth_headers, json={"role": "viewer"})
    assert r.status_code == 400


def test_set_role_not_found(client, auth_headers):
    r = client.put("/auth/users/nonexistent/role", headers=auth_headers, json={"role": "viewer"})
    assert r.status_code == 404


def test_set_role_invalid_role(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "longenough123", "role": "viewer"},
    )
    r = client.put("/auth/users/someone/role", headers=auth_headers, json={"role": "superadmin"})
    assert r.status_code == 400


def test_set_role_success(client, auth_headers):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": "someone", "password": "longenough123", "role": "viewer"},
    )
    r = client.put("/auth/users/someone/role", headers=auth_headers, json={"role": "operator"})
    assert r.status_code == 200

    r2 = client.get("/auth/users", headers=auth_headers)
    someone = next(u for u in r2.json()["users"] if u["username"] == "someone")
    assert someone["role"] == "operator"


# ── /auth/me ──────────────────────────────────────────────────


def test_me_returns_current_user_info(client, auth_headers):
    r = client.get("/auth/me", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["username"] == "admin"
    assert r.json()["role"] == "admin"


def test_me_without_token_when_users_exist_returns_401(client, auth_headers):
    # auth_headers side effect creates the admin user; call /me with no token
    r = client.get("/auth/me")
    assert r.status_code == 401
