import os
import pathlib
import logging

# Configure logger
logger = logging.getLogger(__name__)

# Safely import configuration/settings module regardless of naming conventions
try:
    from . import settings as cfg
except ImportError:
    try:
        from . import config as cfg
    except ImportError:
        cfg = None


def get_supabase_client():
    """Initializes and returns the Supabase client using environment variables."""
    url = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        logger.warning(
            "Supabase credentials missing. Ensure SUPABASE_URL and SUPABASE_KEY are set."
        )
        return None

    try:
        from supabase import create_client
        return create_client(url, key)
    except ImportError:
        logger.error("The 'supabase' Python package is not installed.")
        return None


def init_db():
    """Initializes database connections.
    
    Bypasses local file system operations when running with Supabase or on Vercel.
    """
    # 1. Supabase Initialization Path
    is_supabase_active = bool(
        os.environ.get("SUPABASE_URL") 
        or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
        or (cfg and getattr(cfg, "USE_SUPABASE", False))
    )

    if is_supabase_active:
        logger.info("Initializing remote Supabase database connection...")
        client = get_supabase_client()
        if client:
            logger.info("Supabase client initialized successfully.")
        return client

    # 2. Local SQLite Fallback Path (Safe for Vercel's read-only filesystem)
    if cfg and hasattr(cfg, "DB_PATH"):
        try:
            cfg.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            if e.errno == 30:  # Read-only filesystem (/var/task on Vercel)
                logger.info("Read-only filesystem detected. Falling back SQLite path to /tmp.")
                db_name = getattr(cfg.DB_PATH, "name", "lifeguard.db")
                cfg.DB_PATH = pathlib.Path("/tmp") / db_name
                cfg.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            else:
                raise e


# Helper instance for direct importing across blueprints
supabase = get_supabase_client()