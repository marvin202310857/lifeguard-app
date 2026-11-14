"""Supabase Storage helpers for LifeGuard live feeds, leases, and incident media."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from supabase import Client, create_client

from . import settings as cfg

log = logging.getLogger("lifeguard.storage")

BUCKET_NAME = os.environ.get("SUPABASE_STORAGE_BUCKET") or "incident-footage"
VIEWER_LEASE_TTL_SEC = 15.0

_client: Client | None = None
_client_creds: tuple[str, str] | None = None


def _get_client() -> Client:
    """Return a cached Supabase client using service-role or configured credentials."""
    global _client, _client_creds
    url = (cfg.SUPABASE_URL or "").strip()
    key = (cfg.SUPABASE_SERVICE_ROLE_KEY or cfg.SUPABASE_KEY or "").strip()
    if not url or not key:
        raise ValueError("Missing Supabase credentials (SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY).")

    creds = (url, key)
    if _client is None or _client_creds != creds:
        _client = create_client(url, key)
        _client_creds = creds
    return _client


def get_supabase_client() -> Client:
    """Initialize/return the Supabase client using environment variables."""
    return _get_client()


def site_id(site: str | None = None) -> str:
    """Resolve and sanitize the active site identifier for Storage paths."""
    raw = (site or "").strip()
    if not raw:
        config = cfg.load_config()
        if not isinstance(config, dict):
            config = {}
        cloud = config.get("cloud", {})
        if not isinstance(cloud, dict):
            cloud = {}
        for candidate in (
            os.environ.get("LIFEGUARD_SITE"),
            cloud.get("site"),
            cloud.get("site_id"),
            config.get("site"),
            config.get("site_name"),
            config.get("site_id"),
            "default",
        ):
            if candidate is not None and str(candidate).strip():
                raw = str(candidate).strip()
                break
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")
    return safe or "default"


def _live_base(site: str | None = None) -> str:
    return f"live/{site_id(site)}"


def _is_not_found(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(token in msg for token in ("404", "not found", "not_found", "object not found"))


def upload_bytes(
    destination: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    upsert: bool = True,
) -> str:
    """Upload raw bytes to the private Supabase Storage bucket."""
    client = _get_client()
    options = {
        "content-type": content_type,
        "upsert": "true" if upsert else "false",
    }
    client.storage.from_(BUCKET_NAME).upload(
        path=destination,
        file=data,
        file_options=options,
    )
    return destination


def upload_file(
    file_path: str | Path,
    destination: str,
    content_type: str = "video/mp4",
    upsert: bool = True,
) -> str:
    """Upload a local file path to the private Supabase Storage bucket."""
    client = _get_client()
    options = {
        "content-type": content_type,
        "upsert": "true" if upsert else "false",
    }
    with open(file_path, "rb") as handle:
        client.storage.from_(BUCKET_NAME).upload(
            path=destination,
            file=handle,
            file_options=options,
        )
    return destination


def download_bytes(storage_path: str) -> bytes | None:
    """Download an object's bytes from Supabase Storage, returning None if missing."""
    try:
        client = _get_client()
        data = client.storage.from_(BUCKET_NAME).download(storage_path)
        return bytes(data) if data else None
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise


def _json_upload(storage_path: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return upload_bytes(storage_path, raw, "application/json", upsert=True)


def _json_download(storage_path: str) -> dict[str, Any] | None:
    raw = download_bytes(storage_path)
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (UnicodeDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Live state, viewer leases, and live JPEG frames
# ---------------------------------------------------------------------------

def write_live_state(
    cam_id: str,
    report: dict[str, Any],
    alerts: list[Any] | None = None,
    site: str | None = None,
) -> str:
    """Persist camera heartbeat metrics and recent alerts in Storage."""
    base = _live_base(site)
    payload = {
        "cam_id": cam_id,
        "received_at": time.time(),
        "report": dict(report),
        "alerts": list(alerts or []),
    }
    return _json_upload(f"{base}/state/{cam_id}.json", payload)


def read_live_state(cam_id: str, site: str | None = None) -> dict[str, Any] | None:
    """Read the latest heartbeat state for a camera."""
    base = _live_base(site)
    return _json_download(f"{base}/state/{cam_id}.json")


def write_viewer_lease(
    cam_id: str,
    site: str | None = None,
    ttl_sec: float = VIEWER_LEASE_TTL_SEC,
) -> str:
    """Record that a dashboard viewer is watching cam_id."""
    now = time.time()
    base = _live_base(site)
    payload = {
        "cam_id": cam_id,
        "updated_at": now,
        "expires_at": now + float(ttl_sec),
    }
    return _json_upload(f"{base}/lease/{cam_id}.json", payload)


def viewer_is_active(cam_id: str, site: str | None = None) -> bool:
    """Return True when a viewer lease for cam_id has not expired."""
    base = _live_base(site)
    lease = _json_download(f"{base}/lease/{cam_id}.json")
    if not lease:
        return False
    try:
        expires_at = float(lease.get("expires_at", 0))
    except (TypeError, ValueError):
        return False
    return time.time() <= expires_at


def upload_live_frame(
    cam_id: str,
    frame_jpeg: bytes,
    site: str | None = None,
) -> str:
    """Store the latest processed JPEG frame and update its timestamp metadata."""
    base = _live_base(site)
    frame_path = f"{base}/frames/{cam_id}.jpg"
    upload_bytes(frame_path, frame_jpeg, "image/jpeg", upsert=True)
    _json_upload(
        f"{base}/frame-state/{cam_id}.json",
        {"cam_id": cam_id, "frame_updated_at": time.time(), "bytes": len(frame_jpeg)},
    )
    return frame_path


def read_live_frame(cam_id: str, site: str | None = None) -> bytes | None:
    """Download the latest processed JPEG frame for a camera."""
    base = _live_base(site)
    return download_bytes(f"{base}/frames/{cam_id}.jpg")


# ---------------------------------------------------------------------------
# Incident media helpers (used by admin.py & footage.py)
# ---------------------------------------------------------------------------

def upload_incident_video(file_path: str, destination_name: str) -> str | None:
    """Uploads a local video to the private Supabase Storage bucket."""
    try:
        upload_file(file_path, destination_name, "video/mp4", upsert=True)
        log.info("Successfully uploaded %s to cloud storage.", destination_name)
        return destination_name
    except Exception as exc:
        log.error("Failed to upload video to Supabase: %s", exc)
        return None


def get_secure_video_url(storage_path: str, expiration_sec: int = 3600) -> str | None:
    """Generates a temporary, signed URL for authorized dashboard playback."""
    if not storage_path:
        return None
    try:
        client = _get_client()
        response = client.storage.from_(BUCKET_NAME).create_signed_url(
            path=storage_path,
            expires_in=expiration_sec,
        )
        if isinstance(response, dict):
            return response.get("signedURL") or response.get("signedUrl")
        return None
    except Exception as exc:
        log.error("Failed to generate secure video URL: %s", exc)
        return None