"""Background server used by LifeGuard.pyw (auto-start) and start.bat.

Runs the production server (waitress), logs to logs/lifeguard.log, frees port 5000 from an old
version of the system if needed, and restarts itself if the server ever crashes.
"""
import atexit
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from . import portcheck
from . import settings as cfg
from .events import write_event

log = logging.getLogger("lifeguard.serve")


def setup_logging():
    """Log to a rotating file; pythonw has no console, so send print()/errors there too."""
    log_dir = cfg.BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_dir / "lifeguard.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = open(log_dir / "console.log", "a", buffering=1, encoding="utf-8")
    return handler


def main(host=None, port=None, attempts=10, retry_sec=15, restart_sec=5):
    setup_logging()
    host = host or os.environ.get("LIFEGUARD_HOST", "0.0.0.0")
    port = int(port or os.environ.get("LIFEGUARD_PORT", 5000))

    for attempt in range(1, attempts + 1):                 # the old version may still be starting up
        state = portcheck.stop_old_versions(port, log.warning)
        if state == "ours":
            log.info("LifeGuard is already running on port %s - nothing to do.", port)
            return 0
        if state == "free":
            break
        log.error("Port %s is used by another program (try %d/%d); retrying in %ds",
                  port, attempt, attempts, retry_sec)
        time.sleep(retry_sec)
    else:
        log.error("Giving up: port %s never became free.", port)
        return 1

    from waitress import serve
    from .app import create_app

    app = create_app()
    write_event("System Started")
    atexit.register(write_event, "System Stopped")
    log.info("LifeGuard is running on http://localhost:%s", port)
    while True:
        try:
            serve(app, host=host, port=port, threads=24)
            return 0
        except Exception:
            log.exception("Server crashed - restarting in %ds", restart_sec)
            time.sleep(restart_sec)
