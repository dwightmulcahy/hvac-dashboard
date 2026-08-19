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

from fastapi import APIRouter, HTTPException, Header, Request, Body
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

# ── Kiosk PIN login ───────────────────────────────────────────
# PINs are stored/verified with the exact same hash+salt machinery as
# passwords (_hash_password/_verify_password below) — a PIN is just a
# short password. PIN login has no separate lockout system: it reuses
# _check_login_lockout/_record_login_failure/_clear_login_failures
# keyed by this sentinel "username" + the client IP, so repeated wrong
# PINs from the same kiosk trip the same LOGIN_MAX_ATTEMPTS/
# LOGIN_LOCKOUT_MINUTES threshold a password brute-force would.
PIN_LOCKOUT_KEY = "__pin__"
PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 6


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


# ── "Remember me" ─────────────────────────────────────────────
# A deliberately separate mechanism from the session token above, not
# just a longer TTL on it — session tokens live in memory only
# (_tokens dict), so a longer TTL wouldn't survive this container's
# routine restarts (Watchtower auto-update, the optional nightly
# reboot). A remember-token is instead persisted to disk per-user
# (_state["users"][username]["remember_tokens"]) specifically so it
# survives those restarts, and is exchanged for a fresh, normal
# session token on the next visit rather than acting as a session
# itself — /auth/login-remember below is really just a second front
# door into the exact same _create_token() flow as a password login.
#
# Hashed with plain SHA-256, not _hash_password's slow PBKDF2 — PBKDF2's
# expense defends against brute-forcing a low-entropy human-chosen
# secret, which doesn't apply here: this token is 32 bytes of
# secrets.token_urlsafe randomness, already computationally infeasible
# to guess regardless of hash speed. All a fast hash needs to do is
# keep the raw token unrecoverable if state.json's contents ever leak.
#
# Rotates on every use (the matched record is deleted and a fresh one
# issued in the same call) rather than staying valid for its full TTL
# across repeated redemptions — standard refresh-token-rotation
# practice, and the only meaningful mitigation available for a secret
# that necessarily lives in localStorage (survives a tab close, unlike
# the sessionStorage-based session token) and is therefore more
# exposed to theft via XSS than the session token is. Rotation doesn't
# eliminate that exposure, just bounds how long a stolen copy stays
# useful before the legitimate device's next visit invalidates it.
#
# No login-lockout system here unlike password/PIN login — this token
# has ~256 bits of entropy, so rate-limiting guesses is not the
# relevant defense the way it is for a short PIN or a chosen password;
# the defense is the secret's own size.
REMEMBER_TOKEN_TTL_DAYS = 30


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _create_remember_token(username: str) -> str:
    user = _state["users"][username]
    records = user.setdefault("remember_tokens", [])
    now = _utcnow()
    # prune expired records for this user while we're already here,
    # mirroring _create_token's own expired-token cleanup
    records[:] = [r for r in records if r["expires"] > now.isoformat()]
    raw = secrets.token_urlsafe(32)
    records.append({
        "token_hash": _hash_token(raw),
        "created_at": now.isoformat(),
        "expires": (now + datetime.timedelta(days=REMEMBER_TOKEN_TTL_DAYS)).isoformat(),
    })
    return raw


def _find_remember_token(raw_token: str):
    """Linear scan across all users for a matching, unexpired
    remember-token — same shape and same small-household-count
    justification as login_pin's scan below, since a bare token (like
    a bare PIN) carries no username to key the lookup on directly."""
    token_hash = _hash_token(raw_token)
    now_iso = _utcnow().isoformat()
    for username, user in _state["users"].items():
        for record in user.get("remember_tokens", []):
            if record["token_hash"] == token_hash and record["expires"] > now_iso:
                return username, user, record
    return None, None, None


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

    # operator: can send commands, toggle beeper, poll, mark maintenance done
    # ("/complete" only matches POST /maintenance/{id}/complete — plain
    # POST/PUT/DELETE /maintenance/{id} (create/edit/delete reminders)
    # falls through to the admin-only check below, unaffected)
    operator_patterns = ["/cmd", "/beeper/", "/poll", "/display-toggle", "/vacation/", "/complete"]
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


@router.post("/remember")
async def remember_me(authorization: Optional[str] = Header(None)):
    """Mints a remember-token for the CURRENTLY authenticated user —
    called as a deliberate follow-up to a successful /auth/login when
    the person checked "remember me", not folded into /auth/login
    itself. This keeps PIN login (kiosk) and any other auth path from
    ever getting a remember-token by default; only a client that
    explicitly opts in and calls this gets one."""
    info = _require_role("viewer", authorization)
    raw = _create_remember_token(info["username"])
    async with _lock:
        _save_raw(_state)
    return {"ok": True, "remember_token": raw}


@router.post("/login-remember")
async def login_remember(data: dict):
    """Silently re-establishes a real session from a persisted
    remember-token — the client calls this on page load when it finds
    no valid session (fresh visit, or the in-memory session token got
    wiped by a container restart) but does have a stored
    remember-token, before falling back to showing the login form."""
    raw_token = data.get("remember_token", "")
    if not raw_token:
        raise HTTPException(status_code=401, detail="No remember token provided")

    username, user, record = _find_remember_token(raw_token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired remember token")

    # rotate: this exact record is single-use — delete it and issue a
    # fresh one in the same response, so a stolen-and-replayed copy of
    # this token stops working the moment either the legitimate device
    # or an attacker redeems it, whichever happens first
    user["remember_tokens"].remove(record)
    new_raw = _create_remember_token(username)
    user["last_login"] = _now_iso()
    async with _lock:
        _save_raw(_state)
    token = _create_token(username, user["role"])
    return {
        "ok": True,
        "token": token,
        "remember_token": new_raw,
        "username": username,
        "role": user["role"],
        "must_change_password": user.get("must_change_password", False),
    }


@router.put("/users/{username}/pin")
async def set_user_pin(username: str, data: dict, authorization: Optional[str] = Header(None)):
    """Set or clear a user's kiosk PIN. Admin-only, same as role/
    force-reset — a household member doesn't self-serve a PIN, since
    it's meant to be handed out deliberately alongside a chosen role."""
    _require_role("admin", authorization)
    user = _state["users"].get(username)
    if not user:
        raise HTTPException(status_code=404, detail=USER_NOT_FOUND)
    pin = data.get("pin")
    if pin in (None, ""):
        user.pop("pin_hash", None)
        user.pop("pin_salt", None)
        async with _lock:
            _save_raw(_state)
        _add_log(f"PIN cleared for user '{username}'", "info")
        return {"ok": True, "has_pin": False}
    if not isinstance(pin, str) or not pin.isdigit() or not (PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH):
        raise HTTPException(status_code=400, detail=f"PIN must be {PIN_MIN_LENGTH}-{PIN_MAX_LENGTH} digits")
    # uniqueness: a PIN alone identifies the user on the kiosk (no
    # username typed first), so no two users can share one — verify
    # the candidate against every OTHER user's stored hash rather than
    # comparing hashes directly, since each has its own random salt.
    for other_username, other_user in _state["users"].items():
        if other_username == username:
            continue
        other_hash = other_user.get("pin_hash")
        other_salt = other_user.get("pin_salt")
        if other_hash and other_salt and _verify_password(pin, other_hash, other_salt):
            raise HTTPException(status_code=409, detail="That PIN is already assigned to another user")
    h, s = _hash_password(pin)
    user["pin_hash"] = h
    user["pin_salt"] = s
    async with _lock:
        _save_raw(_state)
    _add_log(f"PIN set for user '{username}'", "info")
    return {"ok": True, "has_pin": True}


@router.post("/login-pin")
async def login_pin(data: dict, request: Request):
    """Kiosk PIN login. Unlike /auth/login, there's no username to key
    the lookup on — the PIN itself identifies the user, so this scans
    every user with a PIN configured and verifies against each. Small
    household user counts make that linear scan a non-issue."""
    pin = data.get("pin", "")
    client_ip = request.client.host if request.client else "unknown"

    remaining = _check_login_lockout(PIN_LOCKOUT_KEY, client_ip)
    if remaining is not None:
        mins = remaining // 60 + 1
        raise HTTPException(status_code=429, detail=f"Too many failed attempts — try again in {mins} minute(s)")

    matched_username = None
    matched_user = None
    for username, user in _state["users"].items():
        pin_hash = user.get("pin_hash")
        pin_salt = user.get("pin_salt")
        if pin_hash and pin_salt and _verify_password(pin, pin_hash, pin_salt):
            matched_username = username
            matched_user = user
            break

    if not matched_user:
        _record_login_failure(PIN_LOCKOUT_KEY, client_ip)
        attempts_left = max(0, LOGIN_MAX_ATTEMPTS - _login_attempts.get(_login_key(PIN_LOCKOUT_KEY, client_ip), {}).get("failures", 0))
        detail = "Incorrect PIN"
        if 0 < attempts_left <= 2:
            detail += f" ({attempts_left} attempt(s) remaining)"
        raise HTTPException(status_code=401, detail=detail)

    _clear_login_failures(PIN_LOCKOUT_KEY, client_ip)
    matched_user["last_login"] = _now_iso()
    async with _lock:
        _save_raw(_state)
    token = _create_token(matched_username, matched_user["role"])
    return {
        "ok": True,
        "token": token,
        "username": matched_username,
        "role": matched_user["role"],
        "must_change_password": matched_user.get("must_change_password", False),
    }


@router.post("/logout")
async def logout(authorization: Optional[str] = Header(None), data: dict = Body(default={})):
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        _tokens.pop(token, None)
    # an explicit logout also forgets this device, if it was
    # remembered — "remember me" is meant to survive a browser
    # restart silently, not to survive someone deliberately logging
    # out. Optional: existing callers (kiosk's relock(), any client
    # that never enabled remember-me) send no body at all and this is
    # simply a no-op for them.
    raw_remember_token = (data or {}).get("remember_token")
    if raw_remember_token:
        _, user, record = _find_remember_token(raw_remember_token)
        if user:
            user["remember_tokens"].remove(record)
            async with _lock:
                _save_raw(_state)
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
    # a password change revokes every remembered device for this
    # account too — if the change was prompted by a suspected leak,
    # leaving old remember-tokens valid would undermine the point of
    # changing the password at all
    user["remember_tokens"] = []
    async with _lock:
        _save_raw(_state)
    _add_log(f"Password changed for user '{username}'", "info")
    return {"ok": True}


@router.get("/users")
async def list_users(authorization: Optional[str] = Header(None)):
    _require_role("admin", authorization)
    now_iso = _utcnow().isoformat()
    return {"users": [
        {
            "username": u, "role": v["role"],
            "must_change_password": v.get("must_change_password", False),
            "last_login": v.get("last_login"),
            "has_pin": bool(v.get("pin_hash")),
            "remembered_devices": len([r for r in v.get("remember_tokens", []) if r["expires"] > now_iso]),
        }
        for u, v in _state["users"].items()
    ]}


@router.get("/recovery-key")
async def get_recovery_key(authorization: Optional[str] = Header(None)):
    """Admin-only. Surfaces the same key already printed to Docker logs
    on startup (see api.py's lifespan) so an admin with dashboard
    access but not container/log access can still retrieve it — no new
    exposure beyond what already exists for anyone who can run `docker
    logs`. Still single-use and still regenerated fresh on every
    container restart; this only changes *where* an admin can read the
    current one from, not its lifetime or invalidation rules."""
    _require_role("admin", authorization)
    return {"recovery_key": _state.get("_recovery_key", "")}


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
    # same reasoning as change_password: a forced reset is often a
    # response to a security concern, so leaving that account's
    # remembered devices valid would undercut it
    user["remember_tokens"] = []
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
