"""
HVAC Dashboard — authentication.

Owns:
- password hashing / verification
- bearer tokens (create, validate, expire)
- role-based access control (_require_role, ROLE_WEIGHTS)
- login rate limiting / lockout
- the default admin/admin bootstrap user
- the one-time recovery key (printed to logs on startup)
- auth_middleware — enforces roles on every request based on method/path
- the /auth/* router (login, logout, change-password, users CRUD, recover, me)

api.py wires this up with:

    from auth import router as auth_router, auth_middleware, generate_recovery_key
    app.include_router(auth_router)
    app.middleware("http")(auth_middleware)
"""

import datetime
import hashlib
import secrets
from typing import Optional

from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import JSONResponse

from state import _state, _lock, _save_raw, _add_log, _utcnow, _now_iso

# ── Roles ─────────────────────────────────────────────────

ROLES = ["admin", "operator", "viewer"]
ROLE_WEIGHTS = {"viewer": 0, "operator": 1, "admin": 2}
USER_NOT_FOUND = "User not found"

# ── Login rate limiting ──────────────────────────────────────
# {key: {"failures": int, "locked_until": iso_str_or_None}}
_login_attempts: dict = {}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15
LOGIN_ATTEMPT_WINDOW_MINUTES = 15


def _login_key(username: str, ip: str) -> str:
    return f"{username}:{ip}"


def _check_login_lockout(username: str, ip: str) -> Optional[int]:
    """Return remaining lockout seconds if locked, else None."""
    key = _login_key(username, ip)
    entry = _login_attempts.get(key)
    if not entry or not entry.get("locked_until"):
        return None
    locked_until = datetime.datetime.fromisoformat(entry["locked_until"])
    now = _utcnow()
    if now < locked_until:
        return int((locked_until - now).total_seconds())
    # lockout expired — reset
    _login_attempts.pop(key, None)
    return None


def _record_login_failure(username: str, ip: str):
    key = _login_key(username, ip)
    now = _utcnow()
    entry = _login_attempts.get(key, {"failures": 0, "first_attempt": now.isoformat()})
    first_attempt = datetime.datetime.fromisoformat(entry.get("first_attempt", now.isoformat()))
    # reset window if too old
    if (now - first_attempt).total_seconds() > LOGIN_ATTEMPT_WINDOW_MINUTES * 60:
        entry = {"failures": 0, "first_attempt": now.isoformat()}
    entry["failures"] = entry.get("failures", 0) + 1
    if entry["failures"] >= LOGIN_MAX_ATTEMPTS:
        locked_until = now + datetime.timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
        entry["locked_until"] = locked_until.isoformat()
        _add_log(f"🔒 Login locked for '{username}' from {ip} — {LOGIN_MAX_ATTEMPTS} failed attempts", "err")
    _login_attempts[key] = entry


def _clear_login_failures(username: str, ip: str):
    _login_attempts.pop(_login_key(username, ip), None)


# ── Tokens ────────────────────────────────────────────────

# in-memory token store: {token: {username, role, expires}}
_tokens: dict = {}
TOKEN_TTL_HOURS = 24


def _hash_password(password: str, salt: str = None) -> tuple:
    """Return (hash, salt) for a password."""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return h.hex(), salt


def _verify_password(password: str, stored_hash: str, salt: str) -> bool:
    h, _ = _hash_password(password, salt)
    return secrets.compare_digest(h, stored_hash)


def _create_token(username: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = _utcnow() + datetime.timedelta(hours=TOKEN_TTL_HOURS)
    _tokens[token] = {"username": username, "role": role, "expires": expires.isoformat()}
    # clean expired tokens
    now = _utcnow().isoformat()
    expired = [t for t, v in _tokens.items() if v["expires"] < now]
    for t in expired:
        del _tokens[t]
    return token


def _get_token_info(authorization: str = None) -> Optional[dict]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    info = _tokens.get(token)
    if not info:
        return None
    if info["expires"] < _utcnow().isoformat():
        del _tokens[token]
        return None
    return info


def _require_role(role: str, authorization: str = None):
    """Raise 401/403 if token doesn't meet required role level."""
    # if no users configured yet, allow all (first-run)
    if not _state.get("users"):
        return {"username": "admin", "role": "admin"}
    info = _get_token_info(authorization)
    if not info:
        raise HTTPException(status_code=401, detail="Authentication required")
    if ROLE_WEIGHTS.get(info["role"], 0) < ROLE_WEIGHTS.get(role, 0):
        raise HTTPException(status_code=403, detail=f"Requires {role} role")
    return info


def _ensure_default_admin():
    """Create default admin/admin user if no users exist. Called once
    from api.py's lifespan startup."""
    if not _state["users"]:
        h, s = _hash_password("admin")
        _state["users"]["admin"] = {
            "hash": h, "salt": s,
            "role": "admin",
            "must_change_password": True,
        }


def generate_recovery_key() -> str:
    """Generate and store a fresh one-time recovery key. Called once
    from api.py's lifespan startup — the key is printed to Docker logs
    there, not here, so the caller controls exactly what gets logged."""
    _state["_recovery_key"] = secrets.token_urlsafe(24)
    return _state["_recovery_key"]


# ── Middleware ────────────────────────────────────────────


async def auth_middleware(request: Request, call_next):
    """Enforce auth based on method and path."""
    path = request.url.path
    method = request.method

    # always allow: auth endpoints, health, root status, GET reads
    open_paths = {"/api/", "/", "/health", "/health/push", "/exchange-rate"}
    if path in open_paths or path.startswith("/auth/"):
        return await call_next(request)
    # if no users configured yet, allow everything (first run)
    if not _state.get("users"):
        return await call_next(request)

    authorization = request.headers.get("Authorization")
    info = _get_token_info(authorization)

    if info is None:
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})

    role = info.get("role", "viewer")

    # viewer: GET only (except auth endpoints)
    if method == "GET":
        return await call_next(request)

    # operator: can send commands, toggle beeper, poll
    operator_patterns = ["/cmd", "/beeper/", "/poll", "/display-toggle", "/vacation/"]
    if method == "POST" and any(p in path for p in operator_patterns):
        if ROLE_WEIGHTS.get(role, 0) >= ROLE_WEIGHTS["operator"]:
            return await call_next(request)
        return JSONResponse(status_code=403, content={"detail": "Operator role required"})

    # everything else (settings, devices, schedules, backup, restore, reset) needs admin
    if ROLE_WEIGHTS.get(role, 0) >= ROLE_WEIGHTS["admin"]:
        return await call_next(request)

    return JSONResponse(status_code=403, content={"detail": "Admin role required"})


# ── Router ────────────────────────────────────────────────

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/recover")
async def recover_password(data: dict):
    """Reset admin password using the recovery key printed to Docker logs on startup."""
    key = data.get("recovery_key", "")
    new_password = data.get("new_password", "")
    stored_key = _state.get("_recovery_key", "")
    if not stored_key or not secrets.compare_digest(key, stored_key):
        raise HTTPException(status_code=403, detail="Invalid recovery key")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    h, s = _hash_password(new_password)
    _state["users"]["admin"] = {"hash": h, "salt": s, "role": "admin", "must_change_password": False}
    # invalidate recovery key after use
    generate_recovery_key()
    # revoke all existing tokens
    _tokens.clear()
    async with _lock:
        _save_raw(_state)
    _add_log("Admin password reset via recovery key", "warn")
    return {"ok": True, "message": "Admin password reset — please log in with new password"}


@router.post("/login")
async def login(data: dict, request: Request):
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    client_ip = request.client.host if request.client else "unknown"

    remaining = _check_login_lockout(username, client_ip)
    if remaining is not None:
        mins = remaining // 60 + 1
        raise HTTPException(status_code=429, detail=f"Too many failed attempts — try again in {mins} minute(s)")

    user = _state["users"].get(username)
    if not user or not _verify_password(password, user["hash"], user["salt"]):
        _record_login_failure(username, client_ip)
        attempts_left = max(0, LOGIN_MAX_ATTEMPTS - _login_attempts.get(_login_key(username, client_ip), {}).get("failures", 0))
        detail = "Invalid username or password"
        if 0 < attempts_left <= 2:
            detail += f" ({attempts_left} attempt(s) remaining)"
        raise HTTPException(status_code=401, detail=detail)

    _clear_login_failures(username, client_ip)
    user["last_login"] = _now_iso()
    async with _lock:
        _save_raw(_state)
    token = _create_token(username, user["role"])
    return {
        "ok": True,
        "token": token,
        "username": username,
        "role": user["role"],
        "must_change_password": user.get("must_change_password", False),
    }


@router.post("/logout")
async def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        _tokens.pop(token, None)
    return {"ok": True}


@router.post("/change-password")
async def change_password(data: dict, authorization: Optional[str] = Header(None)):
    info = _require_role("viewer", authorization)
    username = info["username"]
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "")
    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    user = _state["users"].get(username)
    if not user or not _verify_password(old_pw, user["hash"], user["salt"]):
        raise HTTPException(status_code=401, detail="Current password incorrect")
    h, s = _hash_password(new_pw)
    user["hash"] = h
    user["salt"] = s
    user["must_change_password"] = False
    async with _lock:
        _save_raw(_state)
    _add_log(f"Password changed for user '{username}'", "info")
    return {"ok": True}


@router.get("/users")
async def list_users(authorization: Optional[str] = Header(None)):
    _require_role("admin", authorization)
    return {"users": [
        {
            "username": u, "role": v["role"],
            "must_change_password": v.get("must_change_password", False),
            "last_login": v.get("last_login"),
        }
        for u, v in _state["users"].items()
    ]}


@router.post("/users")
async def add_user(data: dict, authorization: Optional[str] = Header(None)):
    _require_role("admin", authorization)
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    role = data.get("role", "viewer")
    if not username or len(password) < 6:
        raise HTTPException(status_code=400, detail="Username and password (6+ chars) required")
    if role not in ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of {ROLES}")
    if username in _state["users"]:
        raise HTTPException(status_code=409, detail="User already exists")
    h, s = _hash_password(password)
    _state["users"][username] = {"hash": h, "salt": s, "role": role, "must_change_password": True}
    async with _lock:
        _save_raw(_state)
    _add_log(f"User '{username}' added with role '{role}'", "info")
    return {"ok": True}


@router.delete("/users/{username}")
async def delete_user(username: str, authorization: Optional[str] = Header(None)):
    info = _require_role("admin", authorization)
    if username == info["username"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if username not in _state["users"]:
        raise HTTPException(status_code=404, detail=USER_NOT_FOUND)
    del _state["users"][username]
    # revoke any active tokens for this user
    to_revoke = [t for t, v in _tokens.items() if v["username"] == username]
    for t in to_revoke:
        del _tokens[t]
    async with _lock:
        _save_raw(_state)
    _add_log(f"User '{username}' deleted", "warn")
    return {"ok": True}


@router.post("/users/{username}/force-reset")
async def force_password_reset(username: str, authorization: Optional[str] = Header(None)):
    _require_role("admin", authorization)
    user = _state["users"].get(username)
    if not user:
        raise HTTPException(status_code=404, detail=USER_NOT_FOUND)
    user["must_change_password"] = True
    async with _lock:
        _save_raw(_state)
    _add_log(f"Password reset forced for user '{username}'", "warn")
    return {"ok": True}


@router.put("/users/{username}/role")
async def set_user_role(username: str, data: dict, authorization: Optional[str] = Header(None)):
    info = _require_role("admin", authorization)
    if username == info["username"]:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    user = _state["users"].get(username)
    if not user:
        raise HTTPException(status_code=404, detail=USER_NOT_FOUND)
    role = data.get("role")
    if role not in ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of {ROLES}")
    old_role = user.get("role", "viewer")
    user["role"] = role
    async with _lock:
        _save_raw(_state)
    _add_log(f"User '{username}' role changed: {old_role} → {role} (by {info['username']})", "info")
    return {"ok": True}


@router.get("/me")
async def get_me(authorization: Optional[str] = Header(None)):
    # if no users, return open access
    if not _state.get("users"):
        return {"username": "admin", "role": "admin", "must_change_password": False}
    info = _get_token_info(authorization)
    if not info:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = _state["users"].get(info["username"], {})
    return {
        "username": info["username"],
        "role": info["role"],
        "must_change_password": user.get("must_change_password", False),
    }
