
import sqlite3
from pathlib import Path

backup = Path("lifeguard-backup.db")

if not backup.exists():
    raise SystemExit("ERROR: Backup file not found.")

con = sqlite3.connect(str(backup))

try:
    check = con.execute("PRAGMA integrity_check").fetchone()[0]
    print("Integrity check:", check)

    tables = [
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]

    print("\nTable record counts:")
    for table in tables:
        count = con.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone()[0]
        print(f"{table}: {count}")
finally:
    con.close()
