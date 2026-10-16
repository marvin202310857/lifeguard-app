"""Authentication: session helpers, decorators and the register / login / logout routes."""
import ipaddress
import os
from functools import wraps

from flask import Blueprint, g, jsonify, redirect, request, session

from . import db
from . import settings as cfg
from .notify import notify_admins_email

bp = Blueprint("auth", __name__)


# ------------------------------------------------------------------ helpers
def json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


LOOPBACK = ("127.0.0.1", "::1")


def proxy_enabled():
    """True when LifeGuard sits behind a reverse proxy / tunnel on this computer (cloudflared, nginx...)."""
    return bool(cfg.load_config().get("behind_proxy")) or os.environ.get("LIFEGUARD_PROXY") == "1"


def _from_trusted_proxy(direct):
    """The proxy must be on this computer (tunnel), or - only if you say so - on a private network
    address, which is how Render's load balancer reaches the app."""
    if direct in LOOPBACK:
        return True
    trust = (os.environ.get("LIFEGUARD_PROXY_TRUST") or str(cfg.load_config().get("proxy_trust") or "")).lower()
    if trust == "private":
        try:
            return ipaddress.ip_address(direct).is_private
        except ValueError:
            return False
    return False


def client_ip():
    """The visitor's address. Behind a trusted proxy this is the address the proxy forwards; a
    connection from anywhere else can't fake it because the forwarded headers are ignored."""
    direct = request.remote_addr
    if proxy_enabled() and _from_trusted_proxy(direct):
        forwarded = (request.headers.get("CF-Connecting-IP") or request.headers.get("True-Client-IP")
                     or request.headers.get("X-Forwarded-For", "").split(",")[0]).strip()
        if forwarded:
            return forwarded[:45]
    return direct


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
    """The signed-in user, re-checked against the DB once per request, so deleted or
    unapproved accounts lose access immediately."""
    if "current_user" not in g:
        user = None
        stored = session.get("user")
        if stored:
            row = db.get_user(stored.get("username"))
            if row and row["approved"]:
                user = {"username": row["username"], "role": row["role"], "id": row["id"]}
            else:
                session.pop("user", None)
        g.current_user = user
    return g.current_user


def is_admin(user):
    return bool(user) and user["role"] == "Administrator"


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


# ------------------------------------------------------------------- routes
@bp.post("/register")
@bp.post("/api/register")
def register():
    data = json_body()
    username = str(data.get("username", "")).strip()
    ok, error = db.register_user(username, str(data.get("password", "")), data.get("role", "User"))
    db.log_event(username[:80] or None, "REGISTER", ok, client_ip(), request.user_agent.string,
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

    # 1) blocked? A lock applies to one account from one address, or to one address overall,
    #    so a stranger can't lock the real administrator out from somewhere else.
    left = max(db.get_lock_remaining(acct_key), db.get_lock_remaining(addr_key))
    if left:
        db.log_event(username, "LOGIN_BLOCKED", False, ip, agent, "Too many failed attempts from this address")
        return fail("Too many failed attempts from this device. "
                    f"Try again in {fmt_duration(left)}. The administrator has been notified.",
                    423, locked=True, seconds_left=left)

    # 2) credentials
    user = db.get_user(username)
    if db.verify_password(user, password):
        db.reset_throttle(acct_key)
        if db.is_legacy_hash(user["password_hash"]):
            db.set_password(user["id"], password)      # upgrade old SHA-256 hash to a salted one
        if not user["approved"]:
            db.log_event(user["username"], "LOGIN_PENDING", False, ip, agent, "Account awaiting approval")
            return fail("Your account is pending administrator approval.", 403)
        session.clear()
        session.permanent = True
        session["user"] = {"username": user["username"], "role": user["role"]}
        db.log_event(user["username"], "LOGIN_SUCCESS", True, ip, agent)
        return jsonify({"success": True, "redirect": "/dashboard"})

    # 3) wrong credentials: count it against the account+address and the address
    remaining, just_locked = db.record_failed_attempt(acct_key)
    db.record_failed_attempt(addr_key, cfg.IP_MAX_FAILED_ATTEMPTS, cfg.IP_LOCK_SECONDS)
    attempt = cfg.MAX_FAILED_ATTEMPTS - remaining
    db.log_event(username, "LOGIN_FAILED", False, ip, agent,
                 f"Failed attempt {attempt} of {cfg.MAX_FAILED_ATTEMPTS}")
    if just_locked:
        known = "" if user else " (no such account)"
        message = (f"Account '{username}'{known} was LOCKED for {fmt_duration(cfg.LOCK_SECONDS)} from "
                   f"IP {ip} after {cfg.MAX_FAILED_ATTEMPTS} failed login attempts.")
        db.log_event(username, "ACCOUNT_LOCKED", False, ip, agent, message)
        db.add_admin_alert("ACCOUNT_LOCKED", message, username, ip)
        notify_admins_email("LifeGuard security alert: account locked", message)
        return fail("Too many failed attempts. Login from this device is locked for "
                    f"{fmt_duration(cfg.LOCK_SECONDS)}. The administrator has been notified.",
                    423, locked=True, seconds_left=cfg.LOCK_SECONDS)
    return fail(f"Invalid username or password. {remaining} attempt(s) left before the account is locked.",
                401, attempts_remaining=remaining)


@bp.post("/logout")
def logout():
    user = session.get("user")
    if user:
        db.log_event(user.get("username"), "LOGOUT", True, client_ip(), request.user_agent.string)
    session.clear()
    return redirect("/")


@bp.get("/api/me")
@login_required
def me():
    user = current_user()
    return jsonify({"username": user["username"], "role": user["role"]})
