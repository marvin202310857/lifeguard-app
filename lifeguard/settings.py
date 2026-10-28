import json
import logging
import os
import secrets
import threading
from pathlib import Path

log = logging.getLogger("lifeguard.settings")

# --- Base Paths & File Configurations ---
BASE_DIR = Path(__file__).parent.parent.absolute()

# Safe directory handling for Vercel read-only filesystem
if os.environ.get("VERCEL"):
    DATA_DIR = Path("/tmp")
else:
    DATA_DIR = BASE_DIR / "data"

# Create local directory if not running in read-only environment
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    DATA_DIR = Path("/tmp")

DB_PATH = DATA_DIR / "lifeguard.db"
MEDIA_DIR = DATA_DIR / "media"
CONFIG_FILE = DATA_DIR / "config.json"

# --- Footage Settings ---
FOOTAGE_DIR = "footage"
FOOTAGE_FOLDER_PREFIX = "AlertSaved-"
FOOTAGE_FOLDER_DATE_FORMAT = "%m-%d-%y"
DEFAULT_FOOTAGE_RETENTION_DAYS = 14

# --- Default System Settings ---
DEFAULT_WARN_SEC = 30
DEFAULT_DANGER_SEC = 60
DEFAULT_OCCUPANCY_INTERVAL_SEC = 5
OCCUPANCY_RETENTION_DAYS = 30
LOGIN_LOG_RETENTION_DAYS = 30

# --- Camera Configuration ---
CAMERAS = {
    "cam1": "Main Pool Camera",
    "cam2": "Secondary Pool Camera",
}

# --- Security Settings ---
USERNAME_PATTERN = r"^[a-zA-Z0-9.\-_]{3,30}$"
MIN_PASSWORD_LENGTH = 8
VALID_ROLES = ["User", "Administrator"]
MAX_FAILED_ATTEMPTS = 5
LOCK_SECONDS = 900
FAILURE_WINDOW_SECONDS = 3600
SESSION_LIFETIME_SEC = 86400  # 24 hours session lifetime
REMOTE_URL = None


def load_secret_key():
    """Retrieve secret key from environment or generate a fallback for session security."""
    key = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY")
    if key:
        return key
    log.warning("No FLASK_SECRET_KEY set in environment; generating temporary secret key.")
    return secrets.token_hex(32)


# Expose top-level SECRET_KEY for Flask initialization
SECRET_KEY = load_secret_key()

# --- Cloud Database & Storage Credentials ---
SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_KEY = (
    os.environ.get("SUPABASE_KEY")
    or os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY")
    or SUPABASE_SERVICE_ROLE_KEY
)
SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL")


# --- Config File Management ---
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
        try:
            tmp.write_text(json.dumps(data, indent=2), "utf-8")
            os.replace(tmp, CONFIG_FILE)
        except OSError as exc:
            log.warning("Could not persist setting %s to disk: %s", key, exc)

        _cache["mtime"] = None


# --- Cloud Uplink Settings ---
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
    return (
        bool(os.environ.get("VERCEL"))
        or bool(os.environ.get("RENDER"))
        or os.environ.get("LIFEGUARD_CLOUD") == "1"
        or os.environ.get("IS_CLOUD", "").lower() in ("true", "1")
    )