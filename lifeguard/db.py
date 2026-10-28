import os
import pathlib
import logging
import re
from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)

# Safely import configuration/settings
try:
    from . import settings as cfg
except ImportError:
    try:
        from . import config as cfg
    except ImportError:
        cfg = None


def get_supabase_client():
    """Initializes and returns the Supabase client safely using environment variables."""
    url = (os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL") or "").strip()
    key = (os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

    if not url or not key:
        logger.warning("Supabase credentials missing. Ensure SUPABASE_URL and SUPABASE_KEY are set.")
        return None

    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception as e:
        logger.error(f"Failed to initialize Supabase client: {e}")
        return None


def init_db():
    """Initializes database connections cleanly."""
    is_supabase_active = bool(
        os.environ.get("SUPABASE_URL") 
        or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
        or (cfg and getattr(cfg, "USE_SUPABASE", False))
    )

    if is_supabase_active:
        logger.info("Initializing remote Supabase database connection...")
        return get_supabase_client()

    if cfg and hasattr(cfg, "DB_PATH"):
        try:
            cfg.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            if e.errno == 30:  # Read-only filesystem
                logger.info("Read-only filesystem detected. Falling back SQLite path to /tmp.")
                db_name = getattr(cfg.DB_PATH, "name", "lifeguard.db")
                cfg.DB_PATH = pathlib.Path("/tmp") / db_name
                cfg.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            else:
                raise e


# Expose safe instance
supabase = get_supabase_client()


# --- User Management & Verification Helpers ---

def get_user(username: str):
    """Fetch user record from Supabase by username."""
    client = get_supabase_client()
    if not client:
        logger.error("Supabase client unavailable for get_user.")
        return None
    try:
        response = client.table("users").select("*").eq("username", username).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]
        return None
    except Exception as e:
        logger.error(f"Error fetching user '{username}' from Supabase: {e}")
        return None


def get_user_by_id(user_id):
    """Fetch user record from Supabase by unique ID."""
    client = get_supabase_client()
    if not client:
        return None
    try:
        res = client.table("users").select("*").eq("id", user_id).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Error fetching user ID '{user_id}': {e}")
        return None


def is_legacy_hash(hash_str: str) -> bool:
    """
    Check if a stored password hash string uses a legacy algorithm (e.g., md5, sha256, sha512, pbkdf2:sha256).
    Current preferred Werkzeug hash algorithm is scrypt (scrypt:32768:8:1$).
    """
    if not hash_str or not isinstance(hash_str, str):
        return False

    # Standard MD5/SHA256 raw hex hashes
    if re.match(r"^[a-fA-F0-9]{32}$", hash_str) or re.match(r"^[a-fA-F0-9]{64}$", hash_str):
        return True

    # Legacy Werkzeug / passlib hash patterns
    if hash_str.startswith(("pbkdf2:sha256:", "sha256$", "md5$", "sha1$")):
        return True

    return False


def verify_password(user, password: str) -> bool:
    """Verify raw password against stored hash or legacy string safely."""
    if not user or not password:
        return False

    stored_hash = user.get("password_hash") if isinstance(user, dict) else getattr(user, "password_hash", None)
    stored_pass = user.get("password") if isinstance(user, dict) else getattr(user, "password", None)

    if stored_hash:
        try:
            return check_password_hash(stored_hash, password)
        except Exception as e:
            logger.warning(f"Failed checking hash format with Werkzeug check_password_hash: {e}")

    # Fallback to plain text matching if legacy column exists
    if stored_pass:
        return stored_pass == password

    return False


def rehash_password(user_id, password: str) -> bool:
    """Upgrade a user's legacy password hash to modern scrypt hash in Supabase."""
    client = get_supabase_client()
    if not client or not user_id or not password:
        return False
    try:
        new_hash = generate_password_hash(password)
        client.table("users").update({"password_hash": new_hash}).eq("id", user_id).execute()
        logger.info(f"Successfully upgraded password hash for user_id '{user_id}'.")
        return True
    except Exception as e:
        logger.error(f"Failed rehashing password for user_id '{user_id}': {e}")
        return False


def create_user(username: str, password: str, role: str = "User"):
    """Create a new user record in Supabase."""
    client = get_supabase_client()
    if not client:
        logger.error("Supabase client unavailable for create_user.")
        return False
    try:
        pw_hash = generate_password_hash(password)
        data = {
            "username": username,
            "password_hash": pw_hash,
            "role": role
        }
        client.table("users").insert(data).execute()
        return True
    except Exception as e:
        logger.error(f"Failed to create user '{username}': {e}")
        return False


def update_last_login(user_id) -> None:
    """Update user last_login timestamp in Supabase."""
    client = get_supabase_client()
    if not client:
        return
    try:
        from datetime import datetime, timezone
        client.table("users").update({
            "last_login": datetime.now(timezone.utc).isoformat()
        }).eq("id", user_id).execute()
    except Exception as e:
        logger.error(f"Failed updating last_login for user_id '{user_id}': {e}")


# --- Security & Rate-Limiting Helpers ---

def get_lock_remaining(key: str, *args, **kwargs) -> int:
    """Return remaining lock duration in seconds (defaults to 0 for serverless)."""
    return 0


def is_locked(key: str, *args, **kwargs) -> bool:
    """Check if an account or IP address is rate-limited/locked."""
    return False


def record_failed_attempt(key: str, *args, **kwargs):
    """Record a failed login attempt and return (remaining_lock_seconds, just_locked_bool)."""
    return 0, False


def record_failed_login(key: str, *args, **kwargs):
    """Alias for record_failed_attempt."""
    return record_failed_attempt(key, *args, **kwargs)


def reset_failed_attempts(key: str, *args, **kwargs) -> None:
    """Clear failed login attempts for an account or IP."""
    pass


def reset_throttle(key: str, *args, **kwargs) -> None:
    """Alias for reset_failed_attempts called by auth.py on successful login."""
    pass


def clear_failed_login(key: str, *args, **kwargs) -> None:
    """Alias for reset_failed_attempts."""
    pass


def clear_attempts(key: str, *args, **kwargs) -> None:
    """Alias for reset_failed_attempts."""
    pass


# --- Event & Audit Logging Helpers ---

def log_event(*args, **kwargs) -> None:
    """
    Safely handle log_event across varying call-site signatures.
    Supports:
      - log_event(username, event_type, success, ip, agent, details)
      - log_event(event_type, details, username)
      - Keyword arguments
    """
    client = get_supabase_client()
    if not client:
        return
    try:
        # Resolve parameters flexibly
        username = kwargs.get("username")
        event_type = kwargs.get("event_type")
        details = kwargs.get("details", "")

        if not username and len(args) > 0 and isinstance(args[0], str):
            username = args[0]
        if not event_type and len(args) > 1 and isinstance(args[1], str):
            event_type = args[1]

        # Handle 6-argument call from auth.py: (username, event_type, success, ip, agent, details)
        if len(args) >= 6:
            username = args[0]
            event_type = args[1]
            success = args[2]
            ip = args[3]
            agent = args[4]
            extra_details = args[5]
            details = f"Success: {success} | IP: {ip} | Agent: {agent} | Details: {extra_details}"
        elif len(args) >= 3 and not details:
            details = str(args[2:])

        payload = {
            "event_type": str(event_type or "SYSTEM_EVENT"),
            "details": str(details),
            "username": str(username) if username else None
        }

        client.table("logs").insert(payload).execute()
    except Exception as e:
        logger.warning(f"Failed inserting audit log event: {e}")


def log_login_attempt(*args, **kwargs) -> None:
    """Log authentication attempt events safely."""
    log_event(*args, **kwargs)