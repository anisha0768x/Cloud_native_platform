import base64
import hashlib
import hmac
import os
import secrets
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Request, Response
import pyotp

from helio.common import PERMISSIONS, actor, fail, service_app, stamp, uid
from helio.contracts import Credentials, NewUser, PasswordChange, UserUpdate


def password_hash(password):
    salt = secrets.token_hex(16)
    return (
        salt + "$" + hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex()
    )


def password_ok(password, stored):
    salt, digest = stored.split("$")
    return hmac.compare_digest(
        hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex(), digest
    )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def public(user):
    return {k: user[k] for k in ("id", "name", "email", "role", "active", "created_at")} | {
        "mfa_enabled": bool(user.get("mfa_secret")),
        "permissions": sorted(PERMISSIONS[user["role"]]),
    }


def cipher():
    return AESGCM(base64.urlsafe_b64decode(os.environ["AUTH_ENCRYPTION_KEY"]))


def encrypt(value):
    nonce = secrets.token_bytes(12)
    return base64.b64encode(nonce + cipher().encrypt(nonce, value.encode(), b"auth-mfa")).decode()


def decrypt(value):
    data = base64.b64decode(value)
    return cipher().decrypt(data[:12], data[12:], b"auth-mfa").decode()


def totp_valid(user, code, pending=False):
    encrypted = user.get("mfa_pending" if pending else "mfa_secret")
    if not encrypted or not isinstance(code, str) or len(code) != 6 or not code.isdigit():
        return False
    otp = pyotp.TOTP(decrypt(encrypted))
    now = int(time.time() // 30)
    for step in (now - 1, now, now + 1):
        if step > user.get("mfa_step", -1) and hmac.compare_digest(otp.at(step * 30), code):
            user["mfa_step"] = step
            return True
    return False


def session(db, token, permission="read", method="GET", csrf="", bearer=False):
    key = digest(token)
    row = db.get("sessions", key)
    if not row or row["expires"] <= time.time() or row["last_seen"] + 1800 <= time.time():
        fail(401, "Your session expired. Sign in again.")
    user = db.get("users", row["user_id"])
    if not user or not user["active"]:
        fail(401, "Account is unavailable.")
    if permission not in PERMISSIONS[user["role"]]:
        fail(403, "Your role cannot perform this action.")
    if (
        method not in {"GET", "HEAD", "OPTIONS"}
        and not bearer
        and not hmac.compare_digest(csrf, row["csrf"])
    ):
        fail(403, "Session verification failed. Refresh and try again.")
    policy = db.get("policy", "security", {"require_privileged_mfa": False})
    if (
        permission not in {"read", "audit"}
        and policy["require_privileged_mfa"]
        and not (user.get("mfa_secret") and row.get("mfa_verified"))
    ):
        fail(
            403,
            "A session verified with MFA is required. Enroll an authenticator and sign in with its code.",
        )
    row["last_seen"] = time.time()
    db.put("sessions", key, row)
    return user, row, key


def install(app):
    def db():
        return app.state.ctx.db

    def own(request):
        principal = actor(request)
        user = db().get("users", principal["id"])
        if not user:
            fail(401, "Account no longer exists.")
        return user, principal

    def revoke(user_id, except_key=None):
        for row in db().rows("sessions", 10000, {"user_id": user_id}):
            if row["id"] != except_key:
                db().delete("sessions", row["id"])

    def create(payload):
        key = digest(payload.email)
        if db().get("emails", key):
            fail(409, "Email address is already registered.")
        user = {
            "id": uid("user"),
            "name": payload.name,
            "email": payload.email,
            "password_hash": password_hash(payload.password),
            "role": payload.role,
            "active": True,
            "created_at": stamp(),
            "mfa_secret": None,
            "mfa_step": -1,
        }
        db().put("users", user["id"], user)
        db().put("emails", key, {"id": user["id"]})
        return user

    @app.get("/api/auth/setup-status")
    def setup_status():
        return {"required": not bool(db().rows("users", 1))}

    @app.post("/api/auth/setup", status_code=201)
    def setup(payload: NewUser, request: Request):
        with db().tx():
            if db().rows("users", 1):
                fail(409, "An administrator already exists.")
            # Only gateway-authenticated loopback requests may omit the setup secret.
            if request.headers.get("x-helio-loopback") != "true":
                if not payload.setup_token:
                    fail(
                        403,
                        "Enter the server setup token shown in this project's VS Code terminal.",
                    )
                if not hmac.compare_digest(
                    payload.setup_token.encode("utf-8"), os.environ["SETUP_TOKEN"].encode("utf-8")
                ):
                    fail(
                        403,
                        "The setup token does not match this running server. Copy the current token from this project's terminal and try again.",
                    )
            payload.role = "admin"
            user = create(payload)
            db().audit(user["email"], "workspace.initialized", user["id"])
        return {"user": public(user)}

    @app.post("/api/auth/login")
    def login(payload: Credentials, request: Request, response: Response):
        with db().tx():
            throttle = digest(
                payload.email.lower() + ":" + request.headers.get("x-helio-client", "unknown")
            )
            attempt = db().get("attempts", throttle, {"n": 0, "start": time.time()})
            if time.time() - attempt["start"] > 300:
                attempt = {"n": 0, "start": time.time()}
            if attempt["n"] >= 10:
                fail(429, "Too many sign-in attempts. Wait five minutes.")
            attempt["n"] += 1
            db().put("attempts", throttle, attempt)
        row = db().get("emails", digest(payload.email.lower().strip()))
        user = db().get("users", row["id"]) if row else None
        if (
            not user
            or not user["active"]
            or not password_ok(payload.password, user["password_hash"])
        ):
            fail(401, "Invalid credentials.")
        with db().tx():
            user = db().get("users", user["id"])
            if user.get("mfa_secret") and not totp_valid(user, payload.code):
                fail(401, "Enter a valid, unused authenticator code.")
            token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            key = digest(token)
            db().put("users", user["id"], user)
            db().put(
                "sessions",
                key,
                {
                    "id": key,
                    "user_id": user["id"],
                    "csrf": csrf,
                    "expires": time.time() + 28800,
                    "last_seen": time.time(),
                    "mfa_verified": bool(user.get("mfa_secret")),
                },
            )
            db().delete("attempts", throttle)
            db().audit(user["email"], "session.created", user["id"])
        response.set_cookie(
            "helio_session",
            token,
            httponly=True,
            samesite="strict",
            secure=request.headers.get("x-helio-transport") == "https",
            max_age=28800,
        )
        return {"user": public(user), "csrf": csrf, "access_token": token}

    @app.post("/internal/introspect")
    def introspect(payload: dict):
        with db().tx():
            user, row, key = session(
                db(),
                payload.get("token", ""),
                payload.get("permission", "read"),
                payload.get("method", "GET"),
                payload.get("csrf", ""),
                payload.get("bearer", False),
            )
        return public(user) | {
            "session_id": key,
            "csrf": row["csrf"],
            "mfa_verified": row["mfa_verified"],
        }

    @app.post("/internal/reauth")
    def reauth(payload: dict):
        user = db().get("users", payload.get("id", ""))
        if not user or not password_ok(payload.get("password", ""), user["password_hash"]):
            fail(403, "Current password is incorrect.")
        return {"verified": True}

    @app.get("/internal/users/{user_id}")
    def get_user(user_id: str):
        user = db().get("users", user_id)
        if not user:
            fail(404, "User not found")
        return public(user)

    @app.get("/internal/security")
    def get_policy():
        return db().get("policy", "security", {"require_privileged_mfa": False})

    @app.get("/api/auth/me")
    def me(request: Request):
        user, principal = own(request)
        return {"user": public(user), "csrf": principal["csrf"]}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        user, principal = own(request)
        db().delete("sessions", principal["session_id"])
        response.delete_cookie("helio_session")
        return {"ok": True}

    @app.post("/api/auth/password")
    def change_password(request: Request, payload: PasswordChange):
        user, principal = own(request)
        if not password_ok(payload.current_password, user["password_hash"]):
            fail(403, "Current password is incorrect.")
        with db().tx():
            user["password_hash"] = password_hash(payload.new_password)
            db().put("users", user["id"], user)
            revoke(user["id"])
            db().audit(user["email"], "password.changed", user["id"])
        return {"message": "Password changed. Sign in again."}

    @app.post("/api/auth/mfa/enroll")
    def enroll(request: Request):
        user, principal = own(request)
        if user.get("mfa_secret"):
            fail(409, "MFA is already enabled.")
        secret = pyotp.random_base32()
        user["mfa_pending"] = encrypt(secret)
        db().put("users", user["id"], user)
        return {
            "secret": secret,
            "uri": pyotp.TOTP(secret).provisioning_uri(
                user["email"], issuer_name="Helio Operations"
            ),
        }

    @app.post("/api/auth/mfa/confirm")
    def confirm(request: Request, payload: dict):
        with db().tx():
            user, principal = own(request)
            if not totp_valid(user, payload.get("code", ""), pending=True):
                fail(422, "Invalid or already-used authenticator code.")
            user["mfa_secret"] = user.pop("mfa_pending")
            db().put("users", user["id"], user)
            # Verification belongs to THIS session. Older sessions are revoked immediately.
            revoke(user["id"], principal["session_id"])
            row = db().get("sessions", principal["session_id"])
            row["mfa_verified"] = True
            db().put("sessions", row["id"], row)
            db().audit(user["email"], "mfa.enabled", user["id"])
        return {"enabled": True, "other_sessions_revoked": True}

    @app.get("/api/users")
    def users(request: Request):
        actor(request, "operate")
        return {"users": [public(u) for u in db().rows("users")]}

    @app.post("/api/users", status_code=201)
    def new_user(request: Request, payload: NewUser):
        principal = actor(request, "admin")
        with db().tx():
            user = create(payload)
            db().audit(principal["email"], "user.created", user["id"])
        return {"user": public(user)}

    @app.patch("/api/users/{user_id}")
    def update_user(user_id: str, payload: UserUpdate, request: Request):
        principal = actor(request, "admin")
        with db().tx():
            user = db().get("users", user_id)
            if not user:
                fail(404, "User not found")
            admins = [u for u in db().rows("users") if u["active"] and u["role"] == "admin"]
            if (
                user["active"]
                and user["role"] == "admin"
                and len(admins) == 1
                and (not payload.active or payload.role != "admin")
            ):
                fail(409, "Keep at least one active administrator.")
            user.update(payload.model_dump())
            db().put("users", user_id, user)
            revoke(user_id)
            db().audit(principal["email"], "user.updated", user_id, payload.model_dump())
        return {"ok": True}

    @app.put("/api/settings/security")
    def security_policy(payload: dict, request: Request):
        principal = actor(request, "admin")
        enabled = payload.get("require_privileged_mfa")
        if not isinstance(enabled, bool):
            fail(422, "MFA policy must be true or false.")
        if enabled and not principal.get("mfa_verified"):
            fail(409, "Verify MFA in your current session before requiring it.")
        with db().tx():
            db().put("policy", "security", {"require_privileged_mfa": enabled})
            db().audit(principal["email"], "security.policy_updated", "workspace", payload)
        return {"require_privileged_mfa": enabled}

    @app.get("/api/cloud/iam")
    def iam(request: Request):
        actor(request)
        users = db().rows("users")
        policy = get_policy()
        return {
            "roles": [
                {
                    "role": r,
                    "members": sum(u["active"] and u["role"] == r for u in users),
                    "mfa_members": sum(
                        u["active"] and u["role"] == r and bool(u.get("mfa_secret")) for u in users
                    ),
                }
                for r in PERMISSIONS
            ],
            "policy": policy,
            "controls": [
                {
                    "name": "Role enforcement",
                    "status": "Gateway and private services enforce roles behind an authenticated internal boundary",
                },
                {"name": "Sessions", "status": "30-minute idle / 8-hour maximum; revocable"},
                {
                    "name": "Privileged MFA",
                    "status": (
                        "required per session"
                        if policy["require_privileged_mfa"]
                        else "optional; enrollment revokes older sessions"
                    ),
                },
                {
                    "name": "Transport to gateway",
                    "status": (
                        "TLS active"
                        if request.headers.get("x-helio-transport") == "https"
                        else "HTTP on local development endpoint"
                    ),
                },
                {
                    "name": "Secrets and objects",
                    "status": "AES-256-GCM; separate auth and object keys",
                },
                {
                    "name": "Database metadata",
                    "status": "PostgreSQL; disk encryption depends on your host",
                },
            ],
        }


app = service_app("auth", install)
