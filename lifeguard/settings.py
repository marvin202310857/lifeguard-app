"""Paths, constants and access to config.json.

Everything tunable lives here so the rest of the code never hard-codes it.
User data (database, config, model, secret key) stays in the project root,
next to run.py, so existing installs keep working unchanged.
"""
import json
import logging
import os
import socket
import threading
from pathlib import Path

log = logging.getLogger("lifeguard.settings")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"
DB_PATH = Path(os.environ.get("LIFEGUARD_DB", BASE_DIR / "lifeguard.db"))
SECRET_KEY_FILE = BASE_DIR / "secret.key"
EVENT_LOG_FILE = BASE_DIR / "pool_events.log"
FOOTAGE_DIR = Path(os.environ.get("LIFEGUARD_FOOTAGE_DIR", BASE_DIR / "footage"))

# Use the local model when present; otherwise ultralytics downloads it by name.
_local_model = BASE_DIR / "yolov8n.pt"
MODEL_PATH = str(_local_model if _local_model.exists() else "yolov8n.pt")

# --- cameras --------------------------------------------------------------
CAMERAS = {"cam1": "Pool Cam 1", "cam2": "Pool Cam 2"}
DEFAULT_STREAMS = {
    "cam1": "http://192.168.1.62:81/stream",
    "cam2": "http://192.168.1.72:81/stream",
}

# --- detection ------------------------------------------------------------
DETECTION_CONF = 0.30
# A swimmer who vanishes for less than this (e.g. under the surface, behind a
# splash) keeps their timer instead of being treated as having left the pool.
LOST_TRACK_GRACE_SEC = 4

# Default escalation: GREEN -> ORANGE (warn) -> RED (danger). The live values
# come from the `system_settings` table, these are only the fallbacks.
DEFAULT_WARN_SEC = 30
DEFAULT_DANGER_SEC = 60
DEFAULT_OCCUPANCY_INTERVAL_SEC = 5
OCCUPANCY_RETENTION_DAYS = 30
LOGIN_LOG_RETENTION_DAYS = 90            # login activity, resolved security alerts

# --- incident footage -----------------------------------------------------
FOOTAGE_FOLDER_PREFIX = "AlertSaved-"
FOOTAGE_FOLDER_DATE_FORMAT = "%m-%d-%y"        # AlertSaved-10-10-26 (month-day-year); "%d-%m-%y" = day first
DEFAULT_FOOTAGE_RETENTION_DAYS = 14     # daily folders older than this are deleted automatically
PRE_ROLL_SEC = 10
POST_ROLL_SEC = 10

# --- accounts / sessions --------------------------------------------------
MAX_FAILED_ATTEMPTS = 5                    # per account AND per visitor address
LOCK_SECONDS = 3 * 60 * 60
FAILURE_WINDOW_SECONDS = 30 * 60           # failed attempts older than this are forgotten
IP_MAX_FAILED_ATTEMPTS = 20                # failed logins from one address (any usernames) ...
IP_LOCK_SECONDS = 15 * 60                  # ... block that address for 15 minutes
SESSION_LIFETIME_SEC = 12 * 60 * 60
USERNAME_PATTERN = r"^[A-Za-z0-9._-]{3,30}$"
MIN_PASSWORD_LENGTH = 8
VALID_ROLES = ("User", "Administrator")


# --- config.json ----------------------------------------------------------
_lock = threading.Lock()
_cache = {"mtime": None, "data": {}}


def load_config():
    """config.json as a dict, re-read only when the file changes."""
    try:
        mtime = CONFIG_FILE.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        if mtime != _cache["mtime"]:
            try:
                _cache["data"] = json.loads(CONFIG_FILE.read_text("utf-8"))
                _cache["mtime"] = mtime
            except (OSError, ValueError) as exc:
                log.warning("Could not read %s: %s", CONFIG_FILE.name, exc)
        return _cache["data"]


def set_config_value(key, value):
    """Update one key atomically (write temp file, then rename)."""
    with _lock:
        try:
            data = json.loads(CONFIG_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        data[key] = value
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        os.replace(tmp, CONFIG_FILE)
        _cache["mtime"] = None


def is_cloud():
    """True on the cloud server (Render): no cameras, fed by the pool computer instead."""
    return os.environ.get("LIFEGUARD_MODE", "").lower() == "cloud"


def cloud_settings():
    """Where the pool computer sends its data, from the environment or config.json's "cloud" block:
    {"url": "https://...onrender.com", "key": "...", "site": "pool-1"}. None when not configured."""
    block = load_config().get("cloud") or {}
    url = os.environ.get("LIFEGUARD_CLOUD_URL") or block.get("url") or ""
    key = os.environ.get("LIFEGUARD_CLOUD_KEY") or block.get("key") or ""
    if not url or not key:
        return None
    return {"url": url.rstrip("/"), "key": key, "site": str(block.get("site") or socket.gethostname()),
            "frame_fps": float(block.get("frame_fps", 2)), "frame_width": int(block.get("frame_width", 400))}


def load_secret_key():
    """Key for signing session cookies: LIFEGUARD_SECRET_KEY if set (cloud), else a saved random file."""
    from_env = os.environ.get("LIFEGUARD_SECRET_KEY")
    if from_env:
        return from_env.encode()
    if not SECRET_KEY_FILE.exists():
        SECRET_KEY_FILE.write_bytes(os.urandom(32))
    return SECRET_KEY_FILE.read_bytes()
