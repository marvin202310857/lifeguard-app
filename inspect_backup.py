
import sqlite3
from pathlib import Path

backup = Path(__file__).resolve().parent / "lifeguard-backup.db"

if not backup.is_file():
    raise FileNotFoundError(
        f"Backup not found: {backup}\n"
        "Keep the backup in the same folder as this script."
    )

connection = sqlite3.connect(
    f"file:{backup.as_posix()}?mode=ro",
    uri=True
)

try:
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    print("Backup integrity:", result)

    if result != "ok":
        raise RuntimeError("Backup integrity check failed; stop here.")

    tables = connection.execute("""
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """).fetchall()

    for table_name, create_sql in tables:
        quoted_name = '"' + table_name.replace('"', '""') + '"'
        count = connection.execute(
            f"SELECT COUNT(*) FROM {quoted_name}"
        ).fetchone()[0]

        print(f"\n{'=' * 60}")
        print(f"TABLE: {table_name}")
        print(f"RECORD COUNT: {count}")
        print("COLUMNS:")

        for column in connection.execute(
            f"PRAGMA table_info({quoted_name})"
        ).fetchall():
            cid, name, data_type, not_null, default, primary_key = column
            print(
                f"  {name}: {data_type or 'UNDECLARED'}"
                f" | not_null={bool(not_null)}"
                f" | primary_key={primary_key}"
                f" | default={default}"
            )

        print("CREATE SQL:")
        print(create_sql or "(No CREATE statement available)")

    print("\nINDEXES:")
    indexes = connection.execute("""
        SELECT name, tbl_name, sql
        FROM sqlite_master
        WHERE type = 'index'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY tbl_name, name
    """).fetchall()

    for name, table, sql_text in indexes:
        print(f"\n{table}.{name}: {sql_text}")

finally:
    connection.close()
