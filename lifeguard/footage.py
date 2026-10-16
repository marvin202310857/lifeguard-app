"""On-disk footage store.

    footage/
      AlertSaved-10-10-26/
        incident_12_cam1_153012.jpg      snapshot taken when the alert fired
        incident_12_cam1_153012.mp4      clip: 10 s before + 10 s after the alert
      AlertSaved-10-11-26/ ...

* A folder is created automatically for every day (even an empty one).
* A day's folder is deleted, together with its incident records, once it is
  `footage_retention_days` old (default 14; stored in the system_settings table).
* Files are written to disk instead of into SQLite, which keeps the database small and fast.
"""
import logging
import os
import re
import shutil
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db
from . import settings as cfg

log = logging.getLogger("lifeguard.footage")

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAINTENANCE_INTERVAL_SEC = 300
DEFAULT_MIME = {"snapshot": "image/jpeg", "video": "video/mp4"}


# ------------------------------------------------------------------ helpers
def root():
    return Path(cfg.FOOTAGE_DIR)


def today():
    return date.today()


def is_valid_day(value):
    if not DAY_RE.match(value or ""):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def retention_days():
    return max(1, db.get_setting_int("footage_retention_days", cfg.DEFAULT_FOOTAGE_RETENTION_DAYS))


def _as_date(day):
    return day if isinstance(day, date) else date.fromisoformat(day)


def folder_name(day):
    """Folder on disk for a day, e.g. 'AlertSaved-10-10-26' (Windows can't use '/' in names)."""
    return f"{cfg.FOOTAGE_FOLDER_PREFIX}{_as_date(day).strftime(cfg.FOOTAGE_FOLDER_DATE_FORMAT)}"


def folder_label(day):
    """The same folder as shown in the dashboard: 'AlertSaved-10/10/26'."""
    stamp = _as_date(day).strftime(cfg.FOOTAGE_FOLDER_DATE_FORMAT).replace("-", "/")
    return f"{cfg.FOOTAGE_FOLDER_PREFIX}{stamp}"


def folder_date(name):
    """The day a folder belongs to, or None for anything this app didn't create.
    Understands the current name and the plain 2026-10-10 name of the first version."""
    if is_valid_day(name):
        return date.fromisoformat(name)
    prefix = cfg.FOOTAGE_FOLDER_PREFIX
    if name.startswith(prefix):
        try:
            parsed = datetime.strptime(name[len(prefix):], cfg.FOOTAGE_FOLDER_DATE_FORMAT).date()
        except ValueError:
            return None
        return parsed if folder_name(parsed) == name else None
    return None


def ensure_day(day):
    folder = root() / folder_name(day)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def resolve(relative_path):
    """Absolute path of a stored file, or None if it would escape the footage folder."""
    base = root().resolve()
    candidate = (base / relative_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


def _save(day, filename, data):
    """Write atomically (temp file, then rename) and return the path relative to the footage root."""
    target = ensure_day(day) / filename
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return f"{folder_name(day)}/{filename}"


def _stamp(created_at):
    """'2026-10-10 15:30:12' -> ('2026-10-10', '153012')"""
    return created_at[:10], created_at[11:19].replace(":", "")


def _unlink(relative_path):
    path = resolve(relative_path)
    if path:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not delete %s: %s", relative_path, exc)


# ---------------------------------------------------------------- incidents
def record_incident(cam_id, track_id, elapsed_sec, snapshot_jpg, event="Drowning Risk Detected",
                    created_at=None, client_ref=None):
    """Create the incident row and save its snapshot into that day's folder. -> incident id

    The cloud server passes the pool's `created_at` and a unique `client_ref`; repeating the same
    `client_ref` returns the incident that already exists instead of creating a second one."""
    if client_ref:
        existing = db.find_incident_by_ref(client_ref)
        if existing:
            return existing["id"]
    try:
        incident_id, created_at = db.create_incident(cam_id, track_id, elapsed_sec, event,
                                                     created_at=created_at, client_ref=client_ref)
    except sqlite3.IntegrityError:                       # two identical uploads arrived together
        return db.find_incident_by_ref(client_ref)["id"]
    if snapshot_jpg:
        day, clock = _stamp(created_at)
        try:
            path = _save(day, f"incident_{incident_id}_{cam_id}_{clock}.jpg", snapshot_jpg)
            db.set_incident_media(incident_id, snapshot_path=path)
        except OSError:
            log.exception("could not save snapshot for incident %s", incident_id)
    return incident_id


def store_video(incident_ids, cam_id, video, mime="video/mp4"):
    """Save one clip and link it to every incident it covers (several alerts can share one clip)."""
    existing = [row for row in map(db.get_incident, incident_ids) if row]
    if not video or not existing:
        db.attach_video([row["id"] for row in existing], None, None)
        return None
    first = existing[0]
    day, clock = _stamp(first["created_at"])
    try:
        path = _save(day, f"incident_{first['id']}_{cam_id}_{clock}.mp4", video)
    except OSError:
        log.exception("could not save video for incident %s", first["id"])
        db.attach_video([row["id"] for row in existing], None, None)
        return None
    db.attach_video([row["id"] for row in existing], path, mime)
    return path


def open_media(incident_id, kind):
    """-> ("file", Path, mime) | ("blob", bytes, mime) | None   (kind: 'snapshot' or 'video')"""
    row = db.get_incident(incident_id)
    if not row:
        return None
    mime = (row["video_mime"] if kind == "video" else None) or DEFAULT_MIME[kind]
    relative = row.get(f"{kind}_path")
    if relative:
        path = resolve(relative)
        if path and path.is_file():
            return "file", path, mime
    blob, blob_mime = db.get_incident_blob(incident_id, kind)      # rows not migrated yet
    if blob:
        return "blob", blob, blob_mime or mime
    return None


def delete_incident(incident_id):
    """Delete the incident and its files (a shared clip is kept until its last incident is gone)."""
    deleted, paths = db.delete_incident(incident_id)
    for relative in paths:
        if not db.media_path_in_use(relative):
            _unlink(relative)
    return deleted


# ------------------------------------------------------------- folder views
def folder_stats():
    """Daily folders still inside the retention window, newest first, for the dashboard."""
    keep, now = retention_days(), today()
    counts = db.incident_counts_by_day()
    sizes = {}
    base = root()
    if base.is_dir():
        for entry in os.scandir(base):
            folder_day = entry.is_dir() and folder_date(entry.name)
            if folder_day:
                total = 0
                for item in os.scandir(entry.path):
                    try:
                        total += item.stat().st_size if item.is_file() else 0
                    except OSError:
                        pass
                key = folder_day.isoformat()
                sizes[key] = sizes.get(key, 0) + total

    days = set(sizes) | set(counts) | {now.isoformat()}
    folders = []
    for day in days:
        if not is_valid_day(day):
            continue
        days_left = keep - (now - date.fromisoformat(day)).days
        if days_left <= 0:                       # expired, waiting for the next cleanup
            continue
        folders.append({"date": day, "folder": folder_name(day), "label": folder_label(day),
                        "incidents": counts.get(day, 0), "bytes": sizes.get(day, 0),
                        "today": day == now.isoformat(), "days_left": min(days_left, keep)})
    folders.sort(key=lambda f: f["date"], reverse=True)

    try:
        free = shutil.disk_usage(base if base.exists() else base.parent).free
    except OSError:
        free = 0
    return {"retention_days": keep, "total_bytes": sum(f["bytes"] for f in folders),
            "disk_free_bytes": free, "folders": folders}


# -------------------------------------------------------------- maintenance
def purge_expired():
    """Delete day folders (and incident records) that reached the retention age. -> (folders, rows)"""
    cutoff = today() - timedelta(days=retention_days())          # this day and older are expired
    rows = db.delete_incidents_through(cutoff.isoformat())
    folders = 0
    base = root()
    if base.is_dir():
        for entry in os.scandir(base):
            folder_day = entry.is_dir(follow_symlinks=False) and folder_date(entry.name)
            if not folder_day:
                continue                                          # never touch anything else
            if folder_day <= cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)
                folders += 1
    if folders or rows:
        log.info("footage cleanup: removed %d folder(s) and %d incident record(s) older than %d days",
                 folders, rows, retention_days())
    return folders, rows


def migrate_legacy_blobs():
    """One-time move of snapshots/videos stored inside SQLite into the daily folders."""
    moved = 0
    for incident_id in db.legacy_blob_incident_ids():
        row = db.get_incident(incident_id)
        if not row:
            continue
        day, clock = _stamp(row["created_at"])
        snapshot = None if row["snapshot_path"] else db.get_incident_blob(incident_id, "snapshot")[0]
        video, mime = (None, None) if row["video_path"] else db.get_incident_blob(incident_id, "video")
        try:
            snapshot_path = _save(day, f"incident_{incident_id}_{row['cam_id']}_{clock}.jpg", snapshot) if snapshot else None
            video_path = _save(day, f"incident_{incident_id}_{row['cam_id']}_{clock}.mp4", video) if video else None
        except OSError:
            log.exception("could not migrate incident %s", incident_id)
            continue
        db.set_incident_media(incident_id, snapshot_path, video_path, mime, clear_blobs=True)
        moved += 1
    if moved:
        log.info("moved footage of %d incident(s) out of the database into %s", moved, root())
        try:
            db.compact()
        except Exception as exc:                                  # cosmetic: file just stays larger
            log.warning("could not compact the database: %s", exc)
    return moved


def migrate_day_folders():
    """Rename first-version folders (2026-10-10) to the AlertSaved-10-10-26 naming."""
    base = root()
    if not base.is_dir():
        return 0
    legacy = [e.name for e in os.scandir(base) if e.is_dir(follow_symlinks=False) and is_valid_day(e.name)]
    renamed = 0
    for old in legacy:
        new = folder_name(old)
        source, target = base / old, base / new
        try:
            if target.exists():                                   # merge into an existing folder
                for item in list(os.scandir(source)):
                    shutil.move(item.path, target / item.name)
                os.rmdir(source)
            else:
                os.rename(source, target)
        except OSError as exc:                                    # e.g. a clip is playing: retry later
            log.warning("could not rename %s: %s", old, exc)
            continue
        db.rename_media_folder(old, new)
        renamed += 1
    if renamed:
        log.info("renamed %d footage folder(s) to the %s naming", renamed, cfg.FOOTAGE_FOLDER_PREFIX)
    return renamed


def maintain():
    """Folders use the current naming; today's folder exists; expired folders and old records are gone.
    Each step is isolated so one failure is logged without blocking the others."""
    for step in (migrate_day_folders, purge_expired, db.prune_old_records,
                 lambda: ensure_day(today().isoformat())):
        try:
            step()
        except Exception:
            log.exception("footage maintenance step %s failed", getattr(step, "__name__", "step"))


_stop = threading.Event()
_thread = None


def _run():
    first = True
    while not _stop.is_set():
        try:
            maintain()
            if first:
                migrate_legacy_blobs()
                first = False
        except Exception:
            log.exception("footage maintenance failed")
        _stop.wait(MAINTENANCE_INTERVAL_SEC)


def start_maintenance():
    """Background thread: creates each new day's folder and deletes expired ones."""
    global _thread
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_run, daemon=True, name="footage-maintenance")
        _thread.start()
