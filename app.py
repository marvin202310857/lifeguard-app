"""Application factory."""
import logging

from pathlib import Path

from flask import Flask, jsonify, request, url_for
from werkzeug.exceptions import HTTPException

from . import __version__, admin, auth, db, footage, live, pages
from . import settings as cfg

CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
       "style-src 'self'; script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def configure_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)   # dashboards poll constantly


def create_app(start_cameras=True, start_maintenance=True):
    configure_logging()
    app = Flask(__name__)
    app.secret_key = cfg.load_secret_key()
    app.config.update(
        SESSION_COOKIE_NAME="lifeguard_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=cfg.SESSION_LIFETIME_SEC,
        MAX_CONTENT_LENGTH=1024 * 1024,
    )

    db.init_db()
    for module in (pages, auth, admin, live):
        app.register_blueprint(module.bp)

    static_dir = Path(app.static_folder)

    @app.context_processor
    def template_helpers():
        """Cache-busted asset URLs; the user's own logo.png/logo.ico win over the bundled SVG."""
        def asset(filename):
            return url_for("static", filename=filename, v=__version__)

        def first_existing(*names):
            return next((n for n in names if (static_dir / n).exists()), names[-1])

        return {"asset": asset,
                "logo_url": asset(first_existing("logo.png", "logo.svg")),
                "favicon_url": asset(first_existing("logo.ico", "logo.svg"))}

    @app.after_request
    def harden(response):
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "message": error.description}), error.code
        return error

    if start_maintenance:
        footage.start_maintenance()       # daily folders + 14-day cleanup
    if start_cameras:
        from .vision.worker import start_workers
        start_workers()
    return app