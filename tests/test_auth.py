"""Tests for auth.py: password hashing, token creation/validation,
role checks, and login rate limiting.
"""


def test_hash_password_produces_different_hash_each_time(auth_module):
    h1, salt1 = auth_module._hash_password("mypassword")
    h2, salt2 = auth_module._hash_password("mypassword")
    # different random salt each call -> different hash even for same password
    assert salt1 != salt2
    assert h1 != h2


def test_verify_password_correct(auth_module):
    h, salt = auth_module._hash_password("correcthorse")
    assert auth_module._verify_password("correcthorse", h, salt) is True


def test_verify_password_incorrect(auth_module):
    h, salt = auth_module._hash_password("correcthorse")
    assert auth_module._verify_password("wrongpassword", h, salt) is False


def test_verify_password_with_fixed_salt_is_deterministic(auth_module):
    h1, salt = auth_module._hash_password("samepassword", salt="fixedsalt123")
    h2, _ = auth_module._hash_password("samepassword", salt="fixedsalt123")
    assert h1 == h2


def test_create_token_and_get_token_info(auth_module):
    token = auth_module._create_token("alice", "operator")
    info = auth_module._get_token_info(f"Bearer {token}")
    assert info is not None
    assert info["username"] == "alice"
    assert info["role"] == "operator"


def test_get_token_info_rejects_missing_bearer_prefix(auth_module):
    token = auth_module._create_token("alice", "operator")
    assert auth_module._get_token_info(token) is None  # no "Bearer " prefix


def test_get_token_info_rejects_unknown_token(auth_module):
    assert auth_module._get_token_info("Bearer not-a-real-token") is None


def test_get_token_info_rejects_no_header(auth_module):
    assert auth_module._get_token_info(None) is None


def test_require_role_allows_when_no_users_configured(auth_module):
    # first-run state: no users yet -> everything is open
    assert auth_module._state["users"] == {}
    info = auth_module._require_role("admin", None)
    assert info["role"] == "admin"


def test_require_role_rejects_missing_token_once_users_exist(auth_module):
    from fastapi import HTTPException
    auth_module._state["users"]["admin"] = {
        "hash": "x", "salt": "y", "role": "admin", "must_change_password": False,
    }
    try:
        auth_module._require_role("admin", None)
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 401


def test_require_role_rejects_insufficient_role(auth_module):
    from fastapi import HTTPException
    auth_module._state["users"]["viewer1"] = {
        "hash": "x", "salt": "y", "role": "viewer", "must_change_password": False,
    }
    token = auth_module._create_token("viewer1", "viewer")
    try:
        auth_module._require_role("admin", f"Bearer {token}")
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 403


def test_require_role_allows_sufficient_role(auth_module):
    auth_module._state["users"]["op1"] = {
        "hash": "x", "salt": "y", "role": "operator", "must_change_password": False,
    }
    token = auth_module._create_token("op1", "operator")
    info = auth_module._require_role("operator", f"Bearer {token}")
    assert info["username"] == "op1"


# ── Login rate limiting ─────────────────────────────────────


def test_login_lockout_not_triggered_before_max_attempts(auth_module):
    for _ in range(auth_module.LOGIN_MAX_ATTEMPTS - 1):
        auth_module._record_login_failure("admin", "1.2.3.4")
    assert auth_module._check_login_lockout("admin", "1.2.3.4") is None


def test_login_lockout_triggers_at_max_attempts(auth_module):
    for _ in range(auth_module.LOGIN_MAX_ATTEMPTS):
        auth_module._record_login_failure("admin", "1.2.3.4")
    remaining = auth_module._check_login_lockout("admin", "1.2.3.4")
    assert remaining is not None
    assert remaining > 0


def test_login_lockout_is_per_username_and_ip(auth_module):
    for _ in range(auth_module.LOGIN_MAX_ATTEMPTS):
        auth_module._record_login_failure("admin", "1.2.3.4")
    # different IP, same username -> not locked
    assert auth_module._check_login_lockout("admin", "5.6.7.8") is None
    # different username, same IP -> not locked
    assert auth_module._check_login_lockout("other_user", "1.2.3.4") is None


def test_clear_login_failures_resets_lockout(auth_module):
    for _ in range(auth_module.LOGIN_MAX_ATTEMPTS):
        auth_module._record_login_failure("admin", "1.2.3.4")
    assert auth_module._check_login_lockout("admin", "1.2.3.4") is not None
    auth_module._clear_login_failures("admin", "1.2.3.4")
    assert auth_module._check_login_lockout("admin", "1.2.3.4") is None
