import os
import pathlib
import sqlite3
from lifeguard import config as cfg

def init_db():
    """Initializes the database, falling back to /tmp on Vercel's read-only filesystem."""
    try:
        # Attempt to create parent directory in local environment
        cfg.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        if e.errno == 30:  # Read-only file system (/var/task on Vercel)
            # Override DB_PATH to point to /tmp
            cfg.DB_PATH = pathlib.Path("/tmp") / cfg.DB_PATH.name
            cfg.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            
            # If using SQLAlchemy, also update SQLALCHEMY_DATABASE_URI dynamically
            if hasattr(cfg, 'SQLALCHEMY_DATABASE_URI'):
                cfg.SQLALCHEMY_DATABASE_URI = f"sqlite:///{cfg.DB_PATH}"
        else:
            raise e

    # Create schema tables if needed
    conn = sqlite3.connect(cfg.DB_PATH)
    cursor = conn.cursor()
    # Add your CREATE TABLE IF NOT EXISTS statements here if applicable
    conn.commit()
    conn.close()