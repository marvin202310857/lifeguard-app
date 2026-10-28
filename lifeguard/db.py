import os
import pathlib
from lifeguard import config as cfg

def init_db():
    """Initializes database connections, bypassing local SQLite directory creation when using Supabase."""
    # If using Supabase, skip local filesystem setup completely
    if os.environ.get("SUPABASE_URL") or getattr(cfg, "USE_SUPABASE", False):
        print("Initializing Supabase database connection...")
        # Add any remote Supabase initialization logic here if needed
        return

    # Fallback for local development using SQLite
    try:
        cfg.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        if e.errno == 30:  # Read-only filesystem on Vercel
            cfg.DB_PATH = pathlib.Path("/tmp") / getattr(cfg.DB_PATH, "name", "lifeguard.db")
            cfg.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        else:
            raise 