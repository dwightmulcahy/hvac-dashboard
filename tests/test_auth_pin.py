"""Tests for kiosk PIN auth: PUT /auth/users/{username}/pin (admin sets
or clears a PIN) and POST /auth/login-pin (PIN-only login, no
username). PIN login reuses the exact same lockout machinery as
password login (LOGIN_MAX_ATTEMPTS/LOGIN_LOCKOUT_MINUTES) — those
thresholds are already covered by test_auth.py and test_auth_gaps.py
for the password path; these tests confirm the PIN path trips the
same mechanism rather than re-testing the mechanism itself.
"""

import auth as auth_module


def _add_user(client, auth_headers, username="someone", role="operator"):
    client.post(
        "/auth/users", headers=auth_headers,
        json={"username": username, "password": "longenough123", "role": role},
    )


# ── PUT /auth/users/{username}/pin ────────────────────────────


def test_set_pin_success(client, auth_headers):
    _add_user(client, auth_headers)
    r = client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": "4821"})
    assert r.status_code == 200
    assert r.json()["has_pin"] is True

    r2 = client.get("/auth/users", headers=auth_headers)
    someone = next(u for u in r2.json()["users"] if u["username"] == "someone")
    assert someone["has_pin"] is True


def test_set_pin_not_found(client, auth_headers):
    r = client.put("/auth/users/nonexistent/pin", headers=auth_headers, json={"pin": "4821"})
    assert r.status_code == 404


def test_set_pin_rejects_non_numeric(client, auth_headers):
    _add_user(client, auth_headers)
    r = client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": "48ab"})
    assert r.status_code == 400


def test_set_pin_rejects_too_short(client, auth_headers):
    _add_user(client, auth_headers)
    r = client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": "123"})
    assert r.status_code == 400


def test_set_pin_rejects_too_long(client, auth_headers):
    _add_user(client, auth_headers)
    r = client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": "1234567"})
    assert r.status_code == 400


def test_set_pin_accepts_boundary_lengths(client, auth_headers):
    _add_user(client, auth_headers, username="four")
    r4 = client.put("/auth/users/four/pin", headers=auth_headers, json={"pin": "1234"})
    assert r4.status_code == 200

    _add_user(client, auth_headers, username="six")
    r6 = client.put("/auth/users/six/pin", headers=auth_headers, json={"pin": "123456"})
    assert r6.status_code == 200


def test_set_pin_rejects_duplicate_across_users(client, auth_headers):
    _add_user(client, auth_headers, username="alice")
    _add_user(client, auth_headers, username="bob")
    client.put("/auth/users/alice/pin", headers=auth_headers, json={"pin": "4821"})

    r = client.put("/auth/users/bob/pin", headers=auth_headers, json={"pin": "4821"})
    assert r.status_code == 409


def test_set_pin_same_user_can_reuse_their_own_pin(client, auth_headers):
    _add_user(client, auth_headers)
    client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": "4821"})
    # re-setting the SAME pin for the SAME user should not trip the
    # "already assigned to another user" check against themselves
    r = client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": "4821"})
    assert r.status_code == 200


def test_clear_pin(client, auth_headers):
    _add_user(client, auth_headers)
    client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": "4821"})
    r = client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": ""})
    assert r.status_code == 200
    assert r.json()["has_pin"] is False

    r2 = client.get("/auth/users", headers=auth_headers)
    someone = next(u for u in r2.json()["users"] if u["username"] == "someone")
    assert someone["has_pin"] is False


def test_set_pin_requires_admin(client, auth_headers):
    _add_user(client, auth_headers, username="viewer1", role="viewer")
    login_r = client.post("/auth/login", json={"username": "viewer1", "password": "longenough123"})
    viewer_headers = {"Authorization": f"Bearer {login_r.json()['token']}"}

    r = client.put("/auth/users/viewer1/pin", headers=viewer_headers, json={"pin": "4821"})
    assert r.status_code == 403


# ── POST /auth/login-pin ──────────────────────────────────────


def test_login_pin_success(client, auth_headers):
    _add_user(client, auth_headers, role="operator")
    client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": "4821"})

    r = client.post("/auth/login-pin", json={"pin": "4821"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["username"] == "someone"
    assert body["role"] == "operator"
    assert "token" in body


def test_login_pin_token_works_for_authenticated_requests(client, auth_headers):
    _add_user(client, auth_headers, role="operator")
    client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": "4821"})

    r = client.post("/auth/login-pin", json={"pin": "4821"})
    pin_headers = {"Authorization": f"Bearer {r.json()['token']}"}
    r2 = client.get("/auth/me", headers=pin_headers)
    assert r2.status_code == 200
    assert r2.json()["username"] == "someone"


def test_login_pin_wrong_pin_fails(client, auth_headers):
    _add_user(client, auth_headers)
    client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": "4821"})

    r = client.post("/auth/login-pin", json={"pin": "9999"})
    assert r.status_code == 401


def test_login_pin_no_users_have_pins_fails(client, auth_headers):
    r = client.post("/auth/login-pin", json={"pin": "4821"})
    assert r.status_code == 401


def test_login_pin_records_last_login(client, auth_headers):
    _add_user(client, auth_headers)
    client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": "4821"})
    client.post("/auth/login-pin", json={"pin": "4821"})

    r = client.get("/auth/users", headers=auth_headers)
    someone = next(u for u in r.json()["users"] if u["username"] == "someone")
    assert someone["last_login"] is not None


def test_login_pin_locks_out_after_max_attempts(client, auth_headers):
    _add_user(client, auth_headers)
    client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": "4821"})

    for _ in range(auth_module.LOGIN_MAX_ATTEMPTS):
        r = client.post("/auth/login-pin", json={"pin": "0000"})
        assert r.status_code == 401

    r = client.post("/auth/login-pin", json={"pin": "4821"})  # correct PIN, but locked out
    assert r.status_code == 429


def test_login_pin_lockout_is_separate_from_password_lockout(client, auth_headers):
    """PIN failures use a distinct lockout key (PIN_LOCKOUT_KEY) from
    password failures, which are keyed by username — confirms one
    doesn't trip the other."""
    _add_user(client, auth_headers)
    client.put("/auth/users/someone/pin", headers=auth_headers, json={"pin": "4821"})

    for _ in range(auth_module.LOGIN_MAX_ATTEMPTS):
        client.post("/auth/login-pin", json={"pin": "0000"})

    # password login for the same user, from the same client, should
    # be unaffected by the PIN lockout
    r = client.post("/auth/login", json={"username": "someone", "password": "longenough123"})
    assert r.status_code == 200


def test_login_pin_ignores_users_without_a_pin_configured(client, auth_headers):
    _add_user(client, auth_headers, username="haspin")
    _add_user(client, auth_headers, username="nopin")
    client.put("/auth/users/haspin/pin", headers=auth_headers, json={"pin": "4821"})
    # "nopin" never gets a PIN set

    r = client.post("/auth/login-pin", json={"pin": "1111"})
    assert r.status_code == 401  # doesn't crash trying to check nopin's absent hash
