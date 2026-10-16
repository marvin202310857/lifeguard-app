"""HTML pages and the favicon."""
from pathlib import Path

from flask import Blueprint, current_app, redirect, render_template, send_from_directory

from . import settings as cfg
from .auth import current_user, is_admin, login_required

bp = Blueprint("pages", __name__)


@bp.get("/")
def index():
    if current_user():
        return redirect("/dashboard")
    return render_template("login.html")


@bp.get("/login.html")
@bp.get("/index.html")
@bp.get("/admin")
def legacy_routes():
    return redirect("/")


@bp.get("/dashboard")
@login_required
def dashboard():
    user = current_user()
    cameras = [{"id": cam_id, "name": name} for cam_id, name in cfg.CAMERAS.items()]
    return render_template("dashboard.html", user=user, is_admin=is_admin(user), cameras=cameras)


@bp.get("/healthz")
def healthz():
    """Used by the hosting platform to see that the app is alive."""
    return "ok"


@bp.get("/favicon.ico")
def favicon():
    static = Path(current_app.static_folder)
    for name in ("logo.ico", "logo.svg"):
        if (static / name).exists():
            return send_from_directory(static, name)
    return "", 204
