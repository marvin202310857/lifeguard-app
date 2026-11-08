from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from functools import lru_cache
from typing import Any

from supabase import Client, create_client

from . import settings as cfg

log = logging.getLogger("lifeguard.storage")

BUCKET_NAME = "incident-footage"
LIVE_FRAME_MAX_AGE_SEC = 10
VIEWER_LEASE_SEC = 10

_client_lock = threading.Lock()


def _clean_segment(value: str, fallback: str = "default") -> str:
    """Return a safe single path segment for Storage object names."""
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    value = value.strip(".-_")
    return value[:100] or fallback


def _clean_object_path(path: str) -> str:
    """Normalize an object path and reject traversal or empty segments."""
    raw_parts = str(path or "").replace("\\", "/").split("/")
    parts = [part for part in raw_parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Invalid Supabase Storage object path.")
    cleaned = [_clean_segment(part) for part in parts]
    return "/".join(cleaned)


def site_id() -> str:
    """Resolve the shared site key used by the local uploader and Vercel API."""
    config = cfg.load_config()
    cloud = config.get("cloud", {}) if isinstance(config, dict) else {}
    if not isinstance(cloud, dict):
        cloud = {}

    value = (
        os.environ.get("LIFEGUARD_SITE")
        or cloud.get("site")
        or cloud.get("site_id")
        or (config.get("site") if isinstance(config, dict) else None)
        or (config.get("site_name") if isinstance(config, dict) else None)
        or (config.get("site_id") if isinstance(config, dict) else None)
    )
    return _clean_segment(value or "default")


@lru_cache(maxsize=1)
def get_supabase_client() -> Client:
    """Create and cache a server-side Supabase client."""
    url = (cfg.SUPABASE_URL or "").strip()
    key = (cfg.SUPABASE_SERVICE_ROLE_KEY or "").strip()

    if not url or not key:
        raise ValueError(
            "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY."
        )

    if not url.startswith(("https://", "http://")):
        url = "https://" + url

    with _client_lock:
        return create_client(url, key)


# Backwards-compatible alias used by older footage code.
def _get_client() -> Client:
    return get_supabase_client()


def _bucket():
    return get_supabase_client().storage.from_(BUCKET_NAME)


def upload_bytes(
    destination_name: str,
    data: bytes,
    mime_type: str,
    *,
    upsert: bool = True,
) -> str:
    """Upload bytes to the private incident-footage bucket and return the path."""
    if not isinstance(data, (bytes, bytearray, memoryview)) or not data:
        raise ValueError("No media data supplied for upload.")

    path = _clean_object_path(destination_name)
    options = {
        "content-type": mime_type or "application/octet-stream",
        "cache-control": "0",
        "upsert": "true" if upsert else "false",
    }

    # storage3 2.33.0 accepts bytes directly.
    # BytesIO is not recognized by its synchronous upload implementation.
    _bucket().upload(
        file=bytes(data),
        path=path,
        file_options=options,
    )
    return path


def upload_file(
    file_path: str | os.PathLike,
    destination_name: str,
    mime_type: str = "application/octet-stream",
    *,
    upsert: bool = True,
) -> str:
    """Upload a local file directly to Supabase, without proxying through Vercel."""
    path = _clean_object_path(destination_name)
    options = {
        "content-type": mime_type or "application/octet-stream",
        "cache-control": "0",
        "upsert": "true" if upsert else "false",
    }
    with open(file_path, "rb") as handle:
        _bucket().upload(
            file=handle,
            path=path,
            file_options=options,
        )
    return path


def upload_incident_video(file_path: str, destination_name: str) -> str | None:
    """Compatibility wrapper for older callers that expect None on failure."""
    try:
        return upload_file(file_path, destination_name, "video/mp4")
    except Exception:
        log.exception("Failed to upload incident video %s", destination_name)
        return None


def download_bytes(storage_path: str) -> bytes:
    """Download one private Storage object."""
    path = _clean_object_path(storage_path)
    data = _bucket().download(path)
    if not data:
        raise FileNotFoundError(f"Storage object not found: {path}")
    return bytes(data)


def delete_objects(paths: list[str] | tuple[str, ...]) -> bool:
    """Delete a list of object paths from the private bucket."""
    clean_paths = sorted({_clean_object_path(p) for p in paths if p})
    if not clean_paths:
        return True
    _bucket().remove(clean_paths)
    return True


def get_secure_video_url(
    storage_path: str,
    expiration_sec: int = 3600,
) -> str | None:
    """Create a short-lived signed URL for a private snapshot or video."""
    if not storage_path:
        return None
    try:
        response = _bucket().create_signed_url(
            _clean_object_path(storage_path),
            max(60, min(int(expiration_sec), 86400)),
        )
        if isinstance(response, dict):
            return response.get("signedURL") or response.get("signedUrl")
        return getattr(response, "signed_url", None)
    except Exception:
        log.exception("Failed to generate signed URL for %s", storage_path)
        return None


# Generic alias: the same signed URL mechanism serves both snapshots and videos.
get_secure_media_url = get_secure_video_url


# ==========================================================
# SHARED LIVE CAMERA STATE FOR VERCEL INSTANCES
# ==========================================================
# Vercel Functions do not share process memory. These objects live in the
# private Supabase Storage bucket so separate function invocations see the
# same heartbeat, viewer lease, and latest JPEG.


def _live_base(site: str | None = None) -> str:
    return f"_live/{_clean_segment(site or site_id())}"


def _json_upload(path: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return upload_bytes(path, encoded, "application/json", upsert=True)


def _json_download(path: str) -> dict[str, Any] | None:
    try:
        raw = download_bytes(path)
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except FileNotFoundError:
        return None
    except Exception as exc:
        # Missing Storage objects can surface as SDK-specific API errors.
        text = str(exc).lower()
        if any(
            token in text
            for token in ("not found", "404", "object does not exist")
        ):
            return None
        log.warning("Could not read shared live state %s: %s", path, exc)
        return None


def write_live_state(
    cam_id: str,
    report: dict[str, Any],
    alerts: list[dict[str, Any]] | None = None,
    *,
    site: str | None = None,
) -> str:
    """Persist one camera heartbeat report for all Vercel instances."""
    cam = _clean_segment(cam_id)
    payload = {
        "cam_id": cam,
        "report": report if isinstance(report, dict) else {},
        "alerts": (alerts or [])[:20],
        "received_at": time.time(),
    }
    return _json_upload(f"{_live_base(site)}/state/{cam}.json", payload)


def read_live_state(
    cam_id: str,
    *,
    site: str | None = None,
) -> dict[str, Any] | None:
    cam = _clean_segment(cam_id)
    return _json_download(f"{_live_base(site)}/state/{cam}.json")


def write_viewer_lease(
    cam_id: str,
    *,
    site: str | None = None,
    lease_seconds: int = VIEWER_LEASE_SEC,
) -> str:
    """Record that a dashboard has recently requested this camera's feed."""
    cam = _clean_segment(cam_id)
    payload = {
        "cam_id": cam,
        "expires_at": time.time() + max(3, min(int(lease_seconds), 60)),
    }
    return _json_upload(f"{_live_base(site)}/viewers/{cam}.json", payload)


def viewer_is_active(
    cam_id: str,
    *,
    site: str | None = None,
) -> bool:
    cam = _clean_segment(cam_id)
    lease = _json_download(f"{_live_base(site)}/viewers/{cam}.json")
    try:
        return bool(lease and float(lease.get("expires_at", 0)) > time.time())
    except (TypeError, ValueError):
        return False


def upload_live_frame(
    cam_id: str,
    jpeg: bytes,
    *,
    site: str | None = None,
) -> str:
    """Update one camera's latest JPEG and its timestamp metadata."""
    cam = _clean_segment(cam_id)
    base = _live_base(site)
    frame_path = f"{base}/frames/{cam}.jpg"
    uploaded = upload_bytes(frame_path, jpeg, "image/jpeg", upsert=True)
    _json_upload(
        f"{base}/frame-state/{cam}.json",
        {"cam_id": cam, "frame_updated_at": time.time()},
    )
    return uploaded


def read_live_frame(
    cam_id: str,
    *,
    site: str | None = None,
    max_age_sec: int = LIVE_FRAME_MAX_AGE_SEC,
) -> bytes | None:
    """Read a fresh latest JPEG; return None for stale or unavailable frames."""
    cam = _clean_segment(cam_id)
    base = _live_base(site)
    meta = _json_download(f"{base}/frame-state/{cam}.json")
    if not meta:
        return None

    try:
        age = time.time() - float(meta.get("frame_updated_at", 0))
    except (TypeError, ValueError):
        return None

    if age < 0 or age > max_age_sec:
        return None

    try:
        return download_bytes(f"{base}/frames/{cam}.jpg")
    except Exception as exc:
        log.warning("Could not download latest frame for %s: %s", cam, exc)
        return None
