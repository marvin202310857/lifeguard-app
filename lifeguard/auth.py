"""Authentication routes and session helpers for LifeGuard."""
import ipaddress
import os
from functools import wraps

from flask import Blueprint, g, jsonify, redirect, request, session

from . import db
from . import settings as cfg
from .notify import notify_admins_email

bp = Blueprint("auth", __name__)


def json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


LOOPBACK = ("127.0.0.1", "::1")


def proxy_enabled():
    try:
        configured = bool(cfg.load_config().get("behind_proxy"))
    except Exception:
        configured = False
    return configured or os.environ.get("LIFEGUARD_PROXY") == "1"


def _from_trusted_proxy(direct):
    if direct in LOOPBACK:
        return True
    try:
        trust = (os.environ.get("LIFEGUARD_PROXY_TRUST") or str(cfg.load_config().get("proxy_trust") or "")).lower()
    except Exception:
        trust = os.environ.get("LIFEGUARD_PROXY_TRUST", "").lower()
    if trust == "private":
        try:
            return ipaddress.ip_address(direct).is_private
        except ValueError:
            return False
    return False


def client_ip():
    direct = request.remote_addr or "unknown"
    if proxy_enabled() and _from_trusted_proxy(direct):
        forwarded = (request.headers.get("CF-Connecting-IP") or request.headers.get("True-Client-IP")
                     or request.headers.get("X-Forwarded-For", "").split(",")[0]).strip()
        if forwarded:
            return forwarded[:45]
    return direct[:45]


def account_key(username, ip):
    return f"{username.lower()}|{ip}"


def address_key(ip):
    return f"@ip|{ip}"


def fail(message, status, **extra):
    return jsonify({"success": False, "message": message, **extra}), status


def fmt_duration(seconds):
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    return f"{hours}h {minutes}m" if hours else f"{max(minutes, 1)}m"


def current_user():
    """Re-check account approval in Supabase on each request."""
    if "current_user" not in g:
        user = None
        stored = session.get("user")
        if isinstance(stored, dict) and stored.get("username"):
            row = db.get_user(stored["username"])
            if row and int(row.get("approved") or 0) == 1:
                user = {
                    "username": row.get("username", ""),
                    "role": row.get("role", "User"),
                    "id": row.get("id"),
                }
            else:
                session.pop("user", None)
        g.current_user = user
    return g.current_user


def is_admin(user):
    return bool(user) and user.get("role") == "Administrator"


def _wants_json():
    return request.path.startswith("/api/") or request.path.endswith("_feed")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            if _wants_json():
                return fail("Not logged in.", 401)
            return redirect("/")
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not is_admin(current_user()):
            return fail("Administrator only.", 403)
        return view(*args, **kwargs)
    return wrapped


@bp.post("/register")
@bp.post("/api/register")
def register():
    data = json_body()
    username = str(data.get("username", "")).strip()[:80]
    password = str(data.get("password", ""))
    ok, error = db.register_user(username, password, "User")
    db.log_event(username or None, "REGISTER", ok, client_ip(), request.user_agent.string,
                 "Account requested" if ok else error)
    if not ok:
        return jsonify({"success": False, "error": error}), 400
    return jsonify({"success": True, "message": "Account requested! Waiting for Admin approval."})


@bp.post("/login")
@bp.post("/api/login")
def login():
    data = json_body()
    username = str(data.get("username", "")).strip()[:80]
    password = str(data.get("password", ""))
    ip, agent = client_ip(), request.user_agent.string
    if not username or not password:
        return fail("Username and password required.", 400)

    acct_key, addr_key = account_key(username, ip), address_key(ip)
    left = max(db.get_lock_remaining(acct_key), db.get_lock_remaining(addr_key))
    if left:
        db.log_event(username, "LOGIN_BLOCKED", False, ip, agent, "Too many failed attempts from this address")
        return fail("Too many failed attempts from this device. "
                    f"Try again in {fmt_duration(left)}. The administrator has been notified.",
                    423, locked=True, seconds_left=left)

    user = db.get_user(username)
    if db.verify_password(user, password):
        db.reset_throttle(acct_key)
        stored_hash = user.get("password_hash") if isinstance(user, dict) else None
        if db.is_legacy_hash(stored_hash):
            db.set_password(user.get("id"), password)
        if int(user.get("approved") or 0) != 1:
            db.log_event(user.get("username"), "LOGIN_PENDING", False, ip, agent, "Account awaiting approval")
            return fail("Your account is pending administrator approval.", 403)
        session.clear()
        session.permanent = True
        session["user"] = {"username": user["username"], "role": user.get("role", "User")}
        db.log_event(user["username"], "LOGIN_SUCCESS", True, ip, agent, "Login successful")
        return jsonify({"success": True, "redirect": "/dashboard"})

    remaining, just_locked = db.record_failed_attempt(acct_key)
    ip_max = getattr(cfg, "MAX_FAILED_ATTEMPTS", 5)
    ip_lock = getattr(cfg, "IP_LOCK_SECONDS", 900)
    db.record_failed_attempt(addr_key, ip_max, ip_lock)
    attempt = ip_max - remaining
    db.log_event(username, "LOGIN_FAILED", False, ip, agent,
                 f"Failed attempt {attempt} of {ip_max}")
    if just_locked:
        known = "" if user else " (no such account)"
        lock_seconds = getattr(cfg, "LOCK_SECONDS", 900)
        message = (f"Account '{username}'{known} was locked for {fmt_duration(lock_seconds)} "
                   f"from IP {ip} after {ip_max} failed login attempts.")
        db.log_event(username, "ACCOUNT_LOCKED", False, ip, agent, message)
        db.add_admin_alert("ACCOUNT_LOCKED", message, username, ip)
        try:
            notify_admins_email("LifeGuard security alert: account locked", message)
        except Exception:
            # Database alert remains available even if email is not configured.
            pass
        return fail("Too many failed attempts. Login from this device is locked for "
                    f"{fmt_duration(lock_seconds)}. The administrator has been notified.",
                    423, locked=True, seconds_left=lock_seconds)
    return fail(f"Invalid username or password. {remaining} attempt(s) left before the account is locked.",
                401, attempts_remaining=remaining)


@bp.post("/logout")
def logout():
    user = session.get("user")
    if user:
        db.log_event(user.get("username"), "LOGOUT", True, client_ip(), request.user_agent.string,
                     "User logged out")
    session.clear()
    return redirect("/")


@bp.get("/api/me")
@login_required
def me():
    user = current_user()
    return jsonify({"username": user["username"], "role": user["role"]})
