"""Paths, constants and access to config.json.

Everything tunable lives here so the rest of the code never hard-codes it.
User data (database, config, model, secret key) stays in the project root,
next to run.py, so existing installs keep working unchanged.
"""
import json
import logging
import os
import secrets
import threading
from pathlib import Path

log = logging.getLogger("lifeguard.settings")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"
DB_PATH = Path(os.environ.get("LIFEGUARD_DB", BASE_DIR / "lifeguard.db"))
SECRET_KEY_FILE = BASE_DIR / "secret.key"
EVENT_LOG_FILE = BASE_DIR / "pool_events.log"
FOOTAGE_DIR = Path(os.environ.get("LIFEGUARD_FOOTAGE_DIR", BASE_DIR / "footage"))

# --- cloud sync / remote uplink -------------------------------------------
REMOTE_URL = os.environ.get("LIFEGUARD_REMOTE_URL", "https://lifeguard-8t4w.onrender.com")

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
LOST_TRACK_GRACE_SEC = 4

DEFAULT_WARN_SEC = 30
DEFAULT_DANGER_SEC = 60
DEFAULT_OCCUPANCY_INTERVAL_SEC = 5
OCCUPANCY_RETENTION_DAYS = 30

# --- incident footage -----------------------------------------------------
FOOTAGE_FOLDER_PREFIX = "AlertSaved-"
FOOTAGE_FOLDER_DATE_FORMAT = "%m-%d-%y"
DEFAULT_FOOTAGE_RETENTION_DAYS = 14
LOGIN_LOG_RETENTION_DAYS = 30
PRE_ROLL_SEC = 10
POST_ROLL_SEC = 10

# --- accounts / sessions --------------------------------------------------
MAX_FAILED_ATTEMPTS = 5
FAILURE_WINDOW_SECONDS = 60
LOCK_SECONDS = 3 * 60 * 60

IP_MAX_FAILED_ATTEMPTS = MAX_FAILED_ATTEMPTS
IP_LOCK_SECONDS = LOCK_SECONDS

SESSION_LIFETIME_SEC = 12 * 60 * 60
USERNAME_PATTERN = r"^[A-Za-z0-9._-]{3,30}$"
MIN_PASSWORD_LENGTH = 8
VALID_ROLES = ("User", "Administrator")


# --- secret key management ------------------------------------------------
def load_secret_key():
    """Load or generate persistent secret key for Flask session signing."""
    secret_key = os.environ.get("FLASK_SECRET_KEY")
    if secret_key:
        return secret_key

    if SECRET_KEY_FILE.exists():
        try:
            return SECRET_KEY_FILE.read_text(encoding="utf-8", errors="ignore").strip()
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("Could not read secret key file %s: %s", SECRET_KEY_FILE.name, exc)

    new_key = secrets.token_hex(32)
    try:
        SECRET_KEY_FILE.write_text(new_key, encoding="utf-8")
    except OSError as exc:
        log.warning("Could not persist secret key to %s: %s", SECRET_KEY_FILE.name, exc)

    return new_key


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


# --- cloud uplink settings --------------------------------------------------
def cloud_settings():
    """Return normalized cloud uplink settings, or None if credentials are absent."""
    config = load_config()
    if not isinstance(config, dict):
        config = {}

    cloud = config.get("cloud", {})
    if not isinstance(cloud, dict):
        cloud = {}

    def first(*values):
        for value in values:
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    url = first(
        os.environ.get("LIFEGUARD_CLOUD_URL"),
        os.environ.get("LIFEGUARD_REMOTE_URL"),
        cloud.get("url"),
        config.get("remote_url"),
        REMOTE_URL,
    )
    key = first(
        os.environ.get("LIFEGUARD_CLOUD_KEY"),
        os.environ.get("LIFEGUARD_INGEST_KEY"),
        cloud.get("key"),
        cloud.get("ingest_key"),
        config.get("ingest_key"),
        config.get("remote_key"),
    )
    site = first(
        os.environ.get("LIFEGUARD_SITE"),
        cloud.get("site"),
        cloud.get("site_id"),
        config.get("site"),
        config.get("site_name"),
        config.get("site_id"),
    )

    if not url or not key or not site:
        return None

    url = url.rstrip("/")
    if not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        log.warning("Cloud uplink URL must use HTTPS except for localhost; uplink disabled")
        return None

    try:
        frame_width = int(cloud.get("frame_width", 640))
        frame_fps = float(cloud.get("frame_fps", 2))
    except (TypeError, ValueError):
        log.warning("Invalid cloud frame_width/frame_fps; using safe defaults")
        frame_width, frame_fps = 640, 2.0

    return {
        "url": url,
        "key": key,
        "site": site,
        "frame_width": max(160, min(frame_width, 1920)),
        "frame_fps": max(0.5, min(frame_fps, 5.0)),
    }


def is_cloud():
    """Whether this process is running in a cloud deployment."""
    return bool(os.environ.get("RENDER")) or os.environ.get("LIFEGUARD_CLOUD") == "1"