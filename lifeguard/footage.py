"""On-disk and cloud footage store.

    footage/
      AlertSaved-10-10-26/
        incident_12_cam1_153012.jpg      snapshot taken when the alert fired
        incident_12_cam1_153012.mp4      clip: 10 s before + 10 s after the alert
      AlertSaved-10-11-26/ ...

* Locally, a folder is created automatically for every day (even an empty one).
* Locally, a day's folder is deleted, together with its incident records, once it is
  `footage_retention_days` old.
* In cloud mode, media is routed directly to Supabase Storage and ephemeral disk 
  maintenance is bypassed.
"""
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db, storage
from . import settings as cfg

log = logging.getLogger("lifeguard.footage")

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAINTENANCE_INTERVAL_SEC = 300
DEFAULT_MIME = {"snapshot": "image/jpeg", "video": "video/mp4"}


# ------------------------------------------------------------------ maintenance
def start_maintenance():
    """Start background disk retention cleanup thread if not running in cloud mode."""
    if cfg.is_cloud():
        log.info("Cloud mode active: bypassing local disk maintenance.")
        return
    log.info("Starting background footage retention maintenance thread.")
    t = threading.Thread(target=_maintenance_loop, daemon=True)
    t.start()


def _maintenance_loop():
    while True:
        try:
            now = today()
            keep = retention_days()
            base = root()
            if base.is_dir():
                for entry in os.scandir(base):
                    folder_day = entry.is_dir() and folder_date(entry.name)
                    if folder_day and (now - folder_day).days >= keep:
                        log.info("Cleaning up expired local footage folder: %s", entry.name)
                        shutil.rmtree(entry.path, ignore_errors=True)
        except Exception as exc:
            log.warning("Footage maintenance error: %s", exc)
        time.sleep(MAINTENANCE_INTERVAL_SEC)


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
    """The day a folder belongs to, or None for anything this app didn't create."""
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
    """Create the incident row and save its snapshot into that day's folder. -> incident id"""
    if client_ref:
        existing = db.find_incident_by_ref(client_ref)
        if existing:
            return existing["id"]
    try:
        incident_id, created_at = db.create_incident(cam_id, track_id, elapsed_sec, event,
                                                     created_at=created_at, client_ref=client_ref)
    except sqlite3.IntegrityError:                       
        return db.find_incident_by_ref(client_ref)["id"]
        
    if snapshot_jpg:
        day, clock = _stamp(created_at)
        try:
            if cfg.is_cloud():
                with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                    tmp.write(snapshot_jpg)
                    tmp_path = tmp.name
                
                dest_name = f"incidents/{cam_id}/incident_{incident_id}_{clock}.jpg"
                supabase = storage.get_supabase_client()
                with open(tmp_path, "rb") as f:
                    supabase.storage.from_("incident-footage").upload(
                        file=f, path=dest_name, file_options={"content-type": "image/jpeg"}
                    )
                path = dest_name
                os.remove(tmp_path)
            else:
                path = _save(day, f"incident_{incident_id}_{cam_id}_{clock}.jpg", snapshot_jpg)
                
            db.set_incident_media(incident_id, snapshot_path=path)
        except Exception:
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
        if cfg.is_cloud():
            path = f"incidents/{cam_id}/incident_{first['id']}_{clock}.mp4"
        else:
            path = _save(day, f"incident_{first['id']}_{cam_id}_{clock}.mp4", video)
    except OSError:
        log.exception("could not save video for incident %s", first["id"])
        db.attach_video([row["id"] for row in existing], None, None)
        return None
        
    db.attach_video([row["id"] for row in existing], path, mime)
    return path


def open_media(incident_id, kind):
    """-> ("file", Path, mime) | ("cloud", path, mime) | ("blob", bytes, mime) | None"""
    row = db.get_incident(incident_id)
    if not row:
        return None
    mime = (row["video_mime"] if kind == "video" else None) or DEFAULT_MIME[kind]
    relative = row.get(f"{kind}_path")
    
    if relative:
        if cfg.is_cloud():
            return "cloud", relative, mime
        path = resolve(relative)
        if path and path.is_file():
            return "file", path, mime
            
    blob, blob_mime = db.get_incident_blob(incident_id, kind)      
    if blob:
        return "blob", blob, blob_mime or mime
    return None


def delete_incident(incident_id):
    """Delete the incident and its files."""
    row = db.get_incident(incident_id)
    deleted, paths = db.delete_incident(incident_id)
    
    if not cfg.is_cloud():
        for relative in paths:
            if not db.media_path_in_use(relative):
                _unlink(relative)
    else:
        if row and deleted:
            try:
                paths_to_delete = [p for p in paths if not db.media_path_in_use(p)]
                if paths_to_delete:
                    supabase = storage._get_client()
                    supabase.storage.from_(storage.BUCKET_NAME).remove(paths_to_delete)
            except Exception as e:
                log.error(f"Failed to delete cloud files for incident {incident_id}: {e}")
                
    return deleted


# ------------------------------------------------------------- folder views
def folder_stats():
    """Daily folders still inside the retention window, newest first, for the dashboard."""
    keep, now = retention_days(), today()
    counts = db.incident_counts_by_day()
    
    if cfg.is_cloud():
        days = set(counts) | {now.isoformat()}
        folders = []
        for day in days:
            if not is_valid_day(day):
                continue
            days_left = keep - (now - date.fromisoformat(day)).days
            if days_left <= 0:
                continue
            folders.append({"date": day, "folder": folder_name(day), "label": folder_label(day),
                            "incidents": counts.get(day, 0), "bytes": 0,
                            "today": day == now.isoformat(), "days_left": min(days_left, keep)})
        folders.sort(key=lambda f: f["date"], reverse=True)
        return {"retention_days": keep, "total_bytes": 0, "disk_free_bytes": 0, "folders": folders}

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
        if days_left <= 0:                       
            continue
        folders.append({"date": day, "folder": folder_name(day), "label": folder_label(day),
                        "incidents": counts.get(day, 0), "bytes": sizes.get(day, 0),
                        "today": day == now.isoformat(), "days_left": min(days_left, keep)})
    folders.sort(key=lambda f: f["date"], reverse=True)

    try:
        free = shutil.disk_usage(base if base.exists() else base.parent).free
    except OSError:
        free = 0
    return {"retention_days": keep, "total_bytes": sum(f["bytes"] for f in folders)}