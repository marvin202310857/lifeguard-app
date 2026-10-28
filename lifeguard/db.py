import os
import pathlib
import logging

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

    # Validate presence and format of URL
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


# --- Security & Rate-Limiting Fallback Helpers ---

def get_lock_remaining(key: str) -> int:
    """Return remaining lock duration in seconds (defaults to 0 for serverless)."""
    return 0


def is_locked(key: str) -> bool:
    """Check if an account or IP address is rate-limited/locked."""
    return False


def record_failed_login(key: str) -> int:
    """Record a failed login attempt."""
    return 0


def clear_failed_login(key: str) -> None:
    """Clear failed login attempts for an account or IP."""
    pass