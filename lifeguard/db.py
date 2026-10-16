"""SQLite access layer: users, login security, alerts, incidents, settings.

One short-lived connection per call keeps things thread-safe; WAL mode (set
once in init_db) lets the camera threads write while the web UI reads.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

from . import settings as cfg

log = logging.getLogger("lifeguard.db")

_USERNAME_RE = re.compile(cfg.USERNAME_PATTERN)
_LEGACY_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# login_throttle keys: "<account>|<address>" per account+address, "@ip|<address>" per address,
# or a plain "<account>" in rows written by older versions.
_THROTTLE_ACCOUNT_SQL = ("CASE WHEN instr(username, '|') > 0 "
                         "THEN substr(username, 1, instr(username, '|') - 1) ELSE username END")

# Constant-time dummy check so unknown usernames take as long as real ones.
_DUMMY_HASH = generate_password_hash("lifeguard-dummy-password")


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def connect():
    conn = sqlite3.connect(str(cfg.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_column_if_missing(conn, table, column, definition):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# ============================================================ schema
def init_db():
    """Create tables, migrate older databases, seed settings and the first admin."""
    cfg.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.execute("PRAGMA journal_mode=WAL")

        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT,
                role TEXT NOT NULL DEFAULT 'User',
                approved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT
            )""")
        # Older databases (Node / early Python versions) lack some columns and
        # stored the hash in `password`. That column is left untouched but is
        # no longer written; password_hash is the single source of truth.
        _add_column_if_missing(c, "users", "password_hash", "TEXT")
        _add_column_if_missing(c, "users", "created_at", "TEXT")
        legacy_cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "password" in legacy_cols:
            c.execute("""UPDATE users SET password_hash = password
                         WHERE (password_hash IS NULL OR password_hash = '')
                           AND password IS NOT NULL AND password <> ''""")
        c.execute("UPDATE users SET created_at = ? WHERE created_at IS NULL OR created_at = ''",
                  (now_str(),))

        c.execute("""
            CREATE TABLE IF NOT EXISTS login_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                event TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 0,
                ip_address TEXT,
                user_agent TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS login_throttle (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until REAL NOT NULL DEFAULT 0,
                last_attempt REAL NOT NULL DEFAULT 0
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS admin_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                username TEXT,
                message TEXT NOT NULL,
                ip_address TEXT,
                created_at TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS occupancy_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id TEXT NOT NULL,
                people_in_pool INTEGER NOT NULL DEFAULT 0,
                people_poolside INTEGER NOT NULL DEFAULT 0,
                submerged_count INTEGER NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_occupancy_timestamp ON occupancy_logs(timestamp)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
        for key, value in {
            "submersion_warn_sec": cfg.DEFAULT_WARN_SEC,
            "submersion_danger_sec": cfg.DEFAULT_DANGER_SEC,
            "occupancy_log_interval_sec": cfg.DEFAULT_OCCUPANCY_INTERVAL_SEC,
            "footage_retention_days": cfg.DEFAULT_FOOTAGE_RETENTION_DAYS,
        }.items():
            c.execute("INSERT OR IGNORE INTO system_settings (key, value, updated_at) VALUES (?, ?, ?)",
                      (key, str(value), now_str()))

        c.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cam_id TEXT NOT NULL,
                track_id INTEGER,
                event TEXT NOT NULL,
                alert_level TEXT DEFAULT 'WARNING',
                elapsed_sec INTEGER DEFAULT 0,
                started_at TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                snapshot BLOB,
                video BLOB,
                video_mime TEXT,
                video_state TEXT NOT NULL DEFAULT 'RECORDING'
            )""")

        # Footage now lives on disk (see footage.py). The old BLOB columns stay only so
        # existing rows can be migrated; they are read as a fallback until then.
        _add_column_if_missing(c, "incidents", "snapshot_path", "TEXT")
        _add_column_if_missing(c, "incidents", "video_path", "TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at)")
        # Split deployment: an incident sent by the pool computer carries a unique reference, so a
        # retried upload can never create it twice.
        _add_column_if_missing(c, "incidents", "client_ref", "TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_incidents_ref ON incidents(client_ref) "
                  "WHERE client_ref IS NOT NULL")
        # Pool computer: items waiting to be sent to the cloud, and which cloud incident each became.
        c.execute("""
            CREATE TABLE IF NOT EXISTS uplink_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                incident_id INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )""")
        c.execute("CREATE TABLE IF NOT EXISTS uplink_map (local_id INTEGER PRIMARY KEY, cloud_id INTEGER NOT NULL)")

        # Incidents left in RECORDING by a crash/restart will never get footage.
        c.execute("UPDATE incidents SET video_state='FAILED' "
                  "WHERE video_state='RECORDING' AND video IS NULL")
        c.execute("DELETE FROM occupancy_logs WHERE timestamp < datetime('now', 'localtime', ?)",
                  (f"-{cfg.OCCUPANCY_RETENTION_DAYS} days",))

    _ensure_admin_exists()


def _ensure_admin_exists():
    """A fresh install has nobody who can approve accounts - create the first admin."""
    with connect() as c:
        has_admin = c.execute(
            "SELECT 1 FROM users WHERE role='Administrator' AND approved=1 LIMIT 1").fetchone()
    if has_admin:
        return
    password = os.environ.get("LIFEGUARD_ADMIN_PASSWORD", "")
    if len(password) < cfg.MIN_PASSWORD_LENGTH:
        password = secrets.token_urlsafe(12)
    ok, err = register_user("admin", password, "Administrator")
    if not ok:  # username 'admin' exists but isn't an approved admin
        with connect() as c:
            c.execute("UPDATE users SET role='Administrator', approved=1, password_hash=? "
                      "WHERE username='admin' COLLATE NOCASE", (generate_password_hash(password),))
    else:
        with connect() as c:
            c.execute("UPDATE users SET approved=1 WHERE username='admin' COLLATE NOCASE")
    banner = "=" * 62
    log.warning("\n%s\n First-run administrator created\n   username: admin\n   password: %s\n"
                " Change it, or set LIFEGUARD_ADMIN_PASSWORD before first run.\n%s",
                banner, password, banner)


# ============================================================ users
def register_user(username, password, role):
    username = (username or "").strip()
    password = password or ""
    if not _USERNAME_RE.match(username):
        return False, "Username must be 3-30 characters: letters, numbers, dot, dash or underscore."
    if len(password) < cfg.MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {cfg.MIN_PASSWORD_LENGTH} characters."
    if role not in cfg.VALID_ROLES:
        role = "User"

    with connect() as c:
        if c.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone():
            return False, "Username already taken."
        c.execute(
            "INSERT INTO users (id, username, password_hash, role, approved, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (str(uuid.uuid4()), username, generate_password_hash(password), role, now_str()))
    return True, None


def get_user(username):
    with connect() as c:
        row = c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                        ((username or "").strip(),)).fetchone()
    if not row:
        return None
    user = dict(row)
    user["approved"] = int(user.get("approved") or 0)
    return user


def list_users():
    with connect() as c:
        rows = c.execute(f"""
            SELECT u.id, u.username, u.role, u.approved, u.created_at,
                   COALESCE(t.failed_attempts, 0) AS failed_attempts,
                   COALESCE(t.locked_until, 0)    AS locked_until
            FROM users u
            LEFT JOIN (
                SELECT {_THROTTLE_ACCOUNT_SQL} AS account,
                       MAX(failed_attempts) AS failed_attempts, MAX(locked_until) AS locked_until
                FROM login_throttle WHERE username NOT LIKE '@ip|%' GROUP BY account
            ) t ON t.account = u.username COLLATE NOCASE
            ORDER BY u.approved ASC, u.created_at DESC""").fetchall()
    now = time.time()
    users = []
    for row in rows:
        d = dict(row)
        left = float(d.pop("locked_until") or 0) - now
        d["approved"] = bool(d["approved"])
        d["locked"] = left > 0
        d["locked_seconds_left"] = max(0, int(left))
        users.append(d)
    return users


def approve_user(user_id):
    with connect() as c:
        return c.execute("UPDATE users SET approved=1 WHERE id=?", (str(user_id),)).rowcount


def delete_user(user_id):
    """-> (ok, username_or_error). Refuses to remove the last administrator."""
    user_id = str(user_id)
    with connect() as c:
        row = c.execute("SELECT username, role, approved FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return False, "User not found."
        if row["role"] == "Administrator" and row["approved"]:
            others = c.execute("SELECT COUNT(*) FROM users WHERE role='Administrator' "
                               "AND approved=1 AND id<>?", (user_id,)).fetchone()[0]
            if others == 0:
                return False, "You can't remove the last administrator."
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
        c.execute(f"DELETE FROM login_throttle WHERE ({_THROTTLE_ACCOUNT_SQL}) = ? COLLATE NOCASE", (row["username"],))
    return True, row["username"]


def set_password(user_id, password):
    with connect() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (generate_password_hash(password), str(user_id)))


# ============================================================ passwords
def is_legacy_hash(stored):
    """Unsalted SHA-256 hex digests written by the old Node server."""
    return bool(_LEGACY_SHA256_RE.match(stored or ""))


def _legacy_matches(stored, password):
    # The Node server hashed once for registration, but upsert() hashed that
    # digest a second time, so both single and double digests exist in the wild.
    single = hashlib.sha256(password.encode()).hexdigest()
    double = hashlib.sha256(single.encode()).hexdigest()
    return hmac.compare_digest(stored, single) or hmac.compare_digest(stored, double)


def verify_password(user, password):
    password = password or ""
    if not user:
        check_password_hash(_DUMMY_HASH, password)
        return False
    stored = user.get("password_hash") or ""
    if not stored:
        return False
    if is_legacy_hash(stored):
        return _legacy_matches(stored, password)
    try:
        return check_password_hash(stored, password)
    except ValueError:
        return False


# ============================================================ login throttle
def get_lock_remaining(key):
    """Seconds left on a lock. `key` is an account+address, address or (old) account key."""
    with connect() as c:
        row = c.execute("SELECT locked_until FROM login_throttle WHERE username=?", (key,)).fetchone()
    return max(0, int(float(row["locked_until"]) - time.time())) if row else 0


def record_failed_attempt(key, max_attempts=cfg.MAX_FAILED_ATTEMPTS, lock_seconds=cfg.LOCK_SECONDS,
                          window_seconds=cfg.FAILURE_WINDOW_SECONDS):
    """Count a failed login for `key`. -> (attempts_remaining, just_locked)"""
    now = time.time()
    with connect() as c:
        row = c.execute("SELECT failed_attempts, locked_until, last_attempt FROM login_throttle WHERE username=?",
                        (key,)).fetchone()
        attempts = 0
        if row:
            finished_lock = bool(row["locked_until"]) and row["locked_until"] <= now
            stale = now - float(row["last_attempt"] or 0) > window_seconds
            if not finished_lock and not stale:
                attempts = int(row["failed_attempts"] or 0)
        attempts += 1
        just_locked = attempts >= max_attempts
        c.execute("""
            INSERT INTO login_throttle (username, failed_attempts, locked_until, last_attempt)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                failed_attempts=excluded.failed_attempts,
                locked_until=excluded.locked_until,
                last_attempt=excluded.last_attempt""",
                  (key, attempts, now + lock_seconds if just_locked else 0, now))
    return max(0, max_attempts - attempts), just_locked


def reset_throttle(key):
    with connect() as c:
        c.execute("DELETE FROM login_throttle WHERE username=?", (key,))


def clear_account_throttle(username):
    """Unlock an account for every address (administrator action)."""
    with connect() as c:
        c.execute(f"DELETE FROM login_throttle WHERE username NOT LIKE '@ip|%' "
                  f"AND ({_THROTTLE_ACCOUNT_SQL}) = ? COLLATE NOCASE", ((username or "").strip(),))


# ============================================================ logs & alerts
def log_event(username, event, success, ip=None, user_agent=None, detail=None):
    with connect() as c:
        c.execute("""INSERT INTO login_logs
                     (username, event, success, ip_address, user_agent, detail, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                  (username, event, 1 if success else 0, ip, (user_agent or "")[:250], detail, now_str()))


def get_login_logs(limit=100):
    with connect() as c:
        rows = c.execute("""SELECT id, username, event, success, ip_address, detail, created_at
                            FROM login_logs ORDER BY id DESC LIMIT ?""", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def add_admin_alert(kind, message, username=None, ip=None):
    with connect() as c:
        return c.execute("""INSERT INTO admin_alerts (kind, username, message, ip_address, created_at)
                            VALUES (?, ?, ?, ?, ?)""",
                         (kind, username, message, ip, now_str())).lastrowid


def get_admin_alerts(only_open=True, limit=50):
    sql = "SELECT * FROM admin_alerts" + (" WHERE acknowledged=0" if only_open else "")
    with connect() as c:
        rows = c.execute(sql + " ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def ack_admin_alert(alert_id=None):
    """Acknowledge one alert, or every open alert when alert_id is None."""
    with connect() as c:
        if alert_id is None:
            c.execute("UPDATE admin_alerts SET acknowledged=1")
        else:
            c.execute("UPDATE admin_alerts SET acknowledged=1 WHERE id=?", (int(alert_id),))


# ============================================================ settings & occupancy
def get_setting_int(key, default):
    with connect() as c:
        row = c.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
    try:
        return int(row["value"]) if row else default
    except (TypeError, ValueError):
        return default


def set_setting(key, value):
    with connect() as c:
        c.execute("""INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, ?)
                     ON CONFLICT(key) DO UPDATE SET
                         value=excluded.value, updated_at=excluded.updated_at""",
                  (key, str(value), now_str()))


def log_occupancy(camera_id, people_in_pool, people_poolside, submerged_count):
    with connect() as c:
        c.execute("""INSERT INTO occupancy_logs
                     (camera_id, people_in_pool, people_poolside, submerged_count, timestamp)
                     VALUES (?, ?, ?, ?, ?)""",
                  (camera_id, int(people_in_pool), int(people_poolside), int(submerged_count), now_str()))


# ============================================================ incidents
_INCIDENT_COLUMNS = """
    id, cam_id, track_id, event, alert_level, elapsed_sec, status, created_at, started_at,
    resolved_at, video_state,
    (snapshot_path IS NOT NULL OR snapshot IS NOT NULL) AS has_snapshot,
    (video_path    IS NOT NULL OR video    IS NOT NULL) AS has_video"""


def create_incident(cam_id, track_id, elapsed_sec, event="Drowning Risk Detected", alert_level="DANGER",
                    created_at=None, client_ref=None):
    """-> (incident_id, created_at). Media files are attached afterwards by footage.py.
    `created_at`/`client_ref` are given when the pool computer reports an incident to the cloud."""
    now = created_at or now_str()
    with connect() as c:
        incident_id = c.execute("""
            INSERT INTO incidents (cam_id, track_id, event, alert_level, elapsed_sec,
                                   started_at, created_at, status, video_state, client_ref)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'RECORDING', ?)""",
                                (cam_id, int(track_id), event, alert_level, int(elapsed_sec), now, now,
                                 client_ref)).lastrowid
    return incident_id, now


def find_incident_by_ref(client_ref):
    with connect() as c:
        row = c.execute("SELECT id, created_at FROM incidents WHERE client_ref=?", (client_ref,)).fetchone()
    return dict(row) if row else None


def get_incident_for_upload(incident_id):
    with connect() as c:
        row = c.execute("""SELECT id, cam_id, track_id, event, elapsed_sec, created_at
                           FROM incidents WHERE id=?""", (int(incident_id),)).fetchone()
    return dict(row) if row else None


def get_incident(incident_id):
    with connect() as c:
        row = c.execute("""SELECT id, cam_id, status, created_at, snapshot_path, video_path,
                                  video_mime, video_state FROM incidents WHERE id=?""",
                        (int(incident_id),)).fetchone()
    return dict(row) if row else None


def resolve_incident(incident_id, resolved_at=None):
    with connect() as c:
        c.execute("UPDATE incidents SET status='RESOLVED', resolved_at=? WHERE id=?",
                  (resolved_at or now_str(), int(incident_id)))


def set_incident_media(incident_id, snapshot_path=None, video_path=None, video_mime=None, clear_blobs=False):
    sets, args = [], []
    if snapshot_path:
        sets.append("snapshot_path=?")
        args.append(snapshot_path)
    if video_path:
        sets += ["video_path=?", "video_mime=?", "video_state='READY'"]
        args += [video_path, video_mime or "video/mp4"]
    if clear_blobs:
        sets += ["snapshot=NULL", "video=NULL"]
    if sets:
        with connect() as c:
            c.execute(f"UPDATE incidents SET {', '.join(sets)} WHERE id=?", (*args, int(incident_id)))


def attach_video(incident_ids, video_path, mime):
    """Mark the clip ready (video_path given) or failed (None) for every incident it covers."""
    with connect() as c:
        for incident_id in incident_ids:
            if video_path:
                c.execute("UPDATE incidents SET video_path=?, video_mime=?, video_state='READY' WHERE id=?",
                          (video_path, mime or "video/mp4", int(incident_id)))
            else:
                c.execute("UPDATE incidents SET video_state='FAILED' WHERE id=?", (int(incident_id),))


def list_incidents(limit=60):
    with connect() as c:
        rows = c.execute(f"SELECT {_INCIDENT_COLUMNS} FROM incidents ORDER BY id DESC LIMIT ?",
                         (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def list_incidents_for_day(day):
    """Incidents created on `day` (YYYY-MM-DD), newest first."""
    with connect() as c:
        rows = c.execute(f"SELECT {_INCIDENT_COLUMNS} FROM incidents "
                         "WHERE substr(created_at, 1, 10) = ? ORDER BY id DESC", (day,)).fetchall()
    return [dict(r) for r in rows]


def incident_counts_by_day():
    with connect() as c:
        rows = c.execute("SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n "
                         "FROM incidents GROUP BY day").fetchall()
    return {r["day"]: r["n"] for r in rows}


def get_incident_blob(incident_id, column):
    """Legacy fallback: -> (bytes, mime) from the old BLOB columns, else (None, None)."""
    if column not in ("snapshot", "video"):
        return None, None
    with connect() as c:
        row = c.execute(f"SELECT {column} AS blob, video_mime FROM incidents WHERE id=?",
                        (int(incident_id),)).fetchone()
    if not row or row["blob"] is None:
        return None, None
    return bytes(row["blob"]), row["video_mime"]


def legacy_blob_incident_ids():
    with connect() as c:
        rows = c.execute("""SELECT id FROM incidents
                            WHERE (snapshot IS NOT NULL AND snapshot_path IS NULL)
                               OR (video    IS NOT NULL AND video_path    IS NULL)""").fetchall()
    return [r["id"] for r in rows]


def delete_incident(incident_id):
    """-> (deleted, [media paths that belonged to it]); the caller removes unused files."""
    with connect() as c:
        row = c.execute("SELECT snapshot_path, video_path FROM incidents WHERE id=?",
                        (int(incident_id),)).fetchone()
        if not row:
            return False, []
        c.execute("DELETE FROM incidents WHERE id=?", (int(incident_id),))
    return True, [p for p in (row["snapshot_path"], row["video_path"]) if p]


def delete_incidents_through(day):
    """Delete every incident created on or before `day` (YYYY-MM-DD). -> rows deleted"""
    with connect() as c:
        return c.execute("DELETE FROM incidents WHERE substr(created_at, 1, 10) <= ?", (day,)).rowcount


def rename_media_folder(old_folder, new_folder):
    """Point stored media paths at a renamed day folder ('2026-10-10' -> 'AlertSaved-10-10-26')."""
    with connect() as c:
        for column in ("snapshot_path", "video_path"):
            c.execute(f"UPDATE incidents SET {column} = ? || substr({column}, ?) WHERE {column} LIKE ?",
                      (new_folder, len(old_folder) + 1, old_folder + "/%"))


def media_path_in_use(relative_path):
    with connect() as c:
        return c.execute("SELECT 1 FROM incidents WHERE snapshot_path=? OR video_path=? LIMIT 1",
                         (relative_path, relative_path)).fetchone() is not None


# ============================================================ uplink (pool computer -> cloud)
def outbox_add(kind, incident_id):
    with connect() as c:
        c.execute("INSERT INTO uplink_outbox (kind, incident_id, created_at) VALUES (?, ?, ?)",
                  (kind, int(incident_id), now_str()))


def outbox_next():
    with connect() as c:
        row = c.execute("SELECT id, kind, incident_id, attempts FROM uplink_outbox ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None


def outbox_done(item_id):
    with connect() as c:
        c.execute("DELETE FROM uplink_outbox WHERE id=?", (int(item_id),))


def outbox_failed(item_id):
    with connect() as c:
        c.execute("UPDATE uplink_outbox SET attempts = attempts + 1 WHERE id=?", (int(item_id),))


def outbox_has(kind, incident_id):
    with connect() as c:
        return c.execute("SELECT 1 FROM uplink_outbox WHERE kind=? AND incident_id=? LIMIT 1",
                         (kind, int(incident_id))).fetchone() is not None


def outbox_size():
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM uplink_outbox").fetchone()[0]


def map_set(local_id, cloud_id):
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO uplink_map (local_id, cloud_id) VALUES (?, ?)",
                  (int(local_id), int(cloud_id)))


def map_get(local_id):
    with connect() as c:
        row = c.execute("SELECT cloud_id FROM uplink_map WHERE local_id=?", (int(local_id),)).fetchone()
    return row["cloud_id"] if row else None


def prune_old_records():
    """Delete records past their retention so a system that runs for months doesn't grow forever.
    -> {'occupancy': n, 'login_logs': n, 'alerts': n, 'throttle': n}"""
    now = time.time()
    occupancy_cutoff = f"-{cfg.OCCUPANCY_RETENTION_DAYS} days"
    log_cutoff = f"-{cfg.LOGIN_LOG_RETENTION_DAYS} days"
    with connect() as c:
        removed = {
            "occupancy": c.execute("DELETE FROM occupancy_logs WHERE timestamp < datetime('now','localtime',?)",
                                   (occupancy_cutoff,)).rowcount,
            "login_logs": c.execute("DELETE FROM login_logs WHERE created_at < datetime('now','localtime',?)",
                                    (log_cutoff,)).rowcount,
            "alerts": c.execute("DELETE FROM admin_alerts WHERE acknowledged=1 "
                                "AND created_at < datetime('now','localtime',?)", (log_cutoff,)).rowcount,
            # failed-login counters whose lock ended and whose window has passed
            "throttle": c.execute("DELETE FROM login_throttle WHERE locked_until < ? AND last_attempt < ?",
                                  (now, now - cfg.FAILURE_WINDOW_SECONDS)).rowcount,
        }
        c.execute("DELETE FROM uplink_map WHERE local_id NOT IN (SELECT id FROM incidents)")
    return removed


def compact():
    """Shrink the database file after media moved out of it."""
    with connect() as c:
        c.execute("VACUUM")
