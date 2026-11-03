"""Authenticated edge-to-cloud ingestion endpoints for LifeGuard.

The local pool computer runs the camera readers and YOLO workers. It sends
small heartbeats and processed JPEG frames to this API. Shared live-camera
state and frames are stored in Supabase Storage so Vercel requests do not rely
on process-local Python memory.

Incident snapshots and videos are uploaded directly from the local computer
to the private Supabase Storage bucket. This API receives only their storage
paths and incident metadata (apart from a small legacy multipart snapshot).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import time
from typing import Any

from flask import Blueprint, jsonify, request

from . import db, settings as cfg, storage

log = logging.getLogger("lifeguard.ingest")
bp = Blueprint("ingest", __name__)

# app.py limits requests to 1 MB. Keep JPEGs below that limit to leave room
# for HTTP headers and avoid Vercel's request-body size restriction.
MAX_LIVE_FRAME_BYTES = 900_000
MAX_LEGACY_SNAPSHOT_BYTES = 850_000
_ALLOWED_STATUSES = {"SAFE", "WARNING", "DANGER", "OFFLINE"}


def _first(*values: Any) -> str | None:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _credentials() -> tuple[str | None, str | None]:
    """Resolve the server-side bearer key and site from env/config."""
    config = cfg.load_config()
    if not isinstance(config, dict):
        config = {}
    cloud = config.get("cloud", {})
    if not isinstance(cloud, dict):
        cloud = {}

    key = _first(
        os.environ.get("LIFEGUARD_INGEST_KEY"),
        os.environ.get("LIFEGUARD_CLOUD_KEY"),
        cloud.get("key"),
        cloud.get("ingest_key"),
        config.get("ingest_key"),
        config.get("remote_key"),
    )
    site = _first(
        os.environ.get("LIFEGUARD_SITE"),
        cloud.get("site"),
        cloud.get("site_id"),
        config.get("site"),
        config.get("site_name"),
        config.get("site_id"),
    )
    return key, site


def _authorize(site_value: Any = None):
    """Validate the Bearer token and, when supplied, the site identifier."""
    expected_key, expected_site = _credentials()
    if not expected_key:
        return jsonify({
            "status": "error",
            "message": "Server ingest authentication is not configured.",
        }), 503

    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not token.strip()
        or not hmac.compare_digest(token.strip(), expected_key)
    ):
        return jsonify({
            "status": "error",
            "message": "Invalid ingest credentials.",
        }), 401

    if site_value is not None:
        supplied_site = str(site_value).strip()
        if not supplied_site:
            return jsonify({
                "status": "error",
                "message": "Missing site identifier.",
            }), 400
        if expected_site and not hmac.compare_digest(supplied_site, expected_site):
            return jsonify({
                "status": "error",
                "message": "Site identifier does not match.",
            }), 403

    return None


def _configured_site() -> str | None:
    """Return the validated site's configured name for Storage operations."""
    _key, site = _credentials()
    return site


def _valid_camera(cam_id: str) -> bool:
    return cam_id in (getattr(cfg, "CAMERAS", {}) or {})


def _as_nonnegative_int(value: Any, field: str) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be an integer.") from exc
    if result < 0:
        raise ValueError(f"{field} cannot be negative.")
    return result


def _valid_media_path(path: Any, cam_id: str, extension: str | None = None) -> bool:
    """Accept only incident paths under the specified camera's Storage prefix."""
    if not isinstance(path, str) or not path or len(path) > 512:
        return False
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    if (
        normalized.startswith("/")
        or any(part in ("", ".", "..") for part in parts)
        or not normalized.startswith(f"incidents/{cam_id}/")
    ):
        return False
    if extension and not normalized.lower().endswith(extension.lower()):
        return False
    return True


def _parse_incident_payload():
    """Accept JSON metadata and the prior multipart snapshot contract."""
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        meta = payload.get("meta", payload)
        if isinstance(meta, dict):
            return payload, dict(meta), None
        return payload, None, None

    raw_meta = request.form.get("meta")
    if raw_meta:
        try:
            meta = json.loads(raw_meta)
        except (TypeError, ValueError):
            return {}, None, None
        if isinstance(meta, dict):
            return (
                {"site": request.form.get("site")},
                meta,
                request.files.get("snapshot"),
            )

    return {}, None, None


# ---------------------------------------------------------------------------
# Local pool computer -> cloud: heartbeat
# ---------------------------------------------------------------------------

@bp.post("/api/ingest/heartbeat")
def ingest_heartbeat():
    """Persist each camera's metrics and return camera IDs with active viewers."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "Invalid heartbeat payload."}), 400

    auth_error = _authorize(payload.get("site"))
    if auth_error:
        return auth_error

    reports = payload.get("cameras")
    if not isinstance(reports, dict):
        return jsonify({"status": "error", "message": "Invalid cameras payload."}), 400

    site = _configured_site()
    for cam_id, value in reports.items():
        if not _valid_camera(cam_id):
            log.warning("Ignoring heartbeat for unconfigured camera %s", cam_id)
            continue
        if not isinstance(value, dict):
            log.warning("Ignoring malformed heartbeat for %s", cam_id)
            continue

        try:
            report = {
                "people_in_pool": _as_nonnegative_int(
                    value.get("people_in_pool"), "people_in_pool"
                ),
                "people_outside_pool": _as_nonnegative_int(
                    value.get("people_outside_pool"), "people_outside_pool"
                ),
                "submerged_count": _as_nonnegative_int(
                    value.get("submerged_count"), "submerged_count"
                ),
                "status": str(value.get("status") or "OFFLINE").upper(),
                "last_updated": value.get("last_updated"),
            }
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400

        if report["status"] not in _ALLOWED_STATUSES:
            report["status"] = "OFFLINE"

        alerts = value.get("alerts", [])
        if not isinstance(alerts, list):
            alerts = []

        try:
            storage.write_live_state(
                cam_id,
                report,
                alerts[:20],
                site=site,
            )
        except Exception:
            log.exception("Could not persist heartbeat for camera %s", cam_id)
            return jsonify({
                "status": "error",
                "message": f"Could not persist camera state for {cam_id}.",
            }), 503

    wanted_frames = []
    for cam_id in (getattr(cfg, "CAMERAS", {}) or {}):
        try:
            if storage.viewer_is_active(cam_id, site=site):
                wanted_frames.append(cam_id)
        except Exception:
            # A viewer-lease read failure should not invalidate the heartbeat.
            log.exception("Could not read viewer lease for camera %s", cam_id)

    return jsonify({
        "status": "ok",
        "frames": wanted_frames,
        "timestamp": time.time(),
    }), 200


# ---------------------------------------------------------------------------
# Local pool computer -> cloud: processed live JPEG
# ---------------------------------------------------------------------------

@bp.post("/api/ingest/frame/<cam_id>")
def ingest_frame(cam_id: str):
    """Store the newest processed JPEG for one configured camera."""
    if not _valid_camera(cam_id):
        return jsonify({"status": "error", "message": "Unknown camera identifier."}), 404

    auth_error = _authorize()
    if auth_error:
        return auth_error

    frame = request.get_data(cache=False)
    if not frame:
        return jsonify({"status": "error", "message": "No frame data received."}), 400
    if len(frame) > MAX_LIVE_FRAME_BYTES:
        return jsonify({"status": "error", "message": "Frame exceeds the 900 KB limit."}), 413
    if not (frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9")):
        return jsonify({"status": "error", "message": "Uploaded frame is not a JPEG."}), 415

    try:
        path = storage.upload_live_frame(
            cam_id,
            frame,
            site=_configured_site(),
        )
    except Exception:
        log.exception("Could not persist live frame for camera %s", cam_id)
        return jsonify({"status": "error", "message": "Could not store camera frame."}), 503

    return jsonify({
        "status": "ok",
        "cam_id": cam_id,
        "bytes": len(frame),
        "path": path,
        "timestamp": time.time(),
    }), 200


# ---------------------------------------------------------------------------
# Local pool computer -> cloud: incident and snapshot metadata
# ---------------------------------------------------------------------------

@bp.post("/api/ingest/incident")
def ingest_incident():
    """Create/find an incident and attach its private Storage snapshot path."""
    payload, meta, snapshot_file = _parse_incident_payload()
    if meta is None:
        return jsonify({"status": "error", "message": "Invalid incident metadata."}), 400

    auth_error = _authorize(payload.get("site"))
    if auth_error:
        return auth_error

    cam_id = str(meta.get("cam_id") or "").strip()
    if not _valid_camera(cam_id):
        return jsonify({"status": "error", "message": "Unknown incident camera."}), 400

    client_ref = str(meta.get("client_ref") or "").strip()
    if not client_ref or len(client_ref) > 240:
        return jsonify({"status": "error", "message": "Missing or invalid incident client_ref."}), 400

    snapshot_path = meta.get("snapshot_path")
    if snapshot_path and not _valid_media_path(snapshot_path, cam_id, ".jpg"):
        return jsonify({"status": "error", "message": "Invalid snapshot storage path."}), 400

    try:
        # The client_ref makes retries idempotent if the edge and cloud share
        # the same incidents table or if Vercel receives a repeated request.
        existing = db.find_incident_by_ref(client_ref)
        if existing:
            incident_id = int(existing["id"])
            created_at = existing.get("created_at") or meta.get("created_at")
        else:
            incident_id, created_at = db.create_incident(
                cam_id=cam_id,
                track_id=meta.get("track_id"),
                elapsed_sec=meta.get("elapsed_sec", 0),
                event=meta.get("event") or "Drowning Risk Detected",
                alert_level=meta.get("alert_level") or "DANGER",
                created_at=meta.get("created_at"),
                client_ref=client_ref,
            )

        # Legacy clients may still send a small multipart JPEG snapshot.
        if snapshot_file is not None and not snapshot_path:
            snapshot = snapshot_file.read(MAX_LEGACY_SNAPSHOT_BYTES + 1)
            if len(snapshot) > MAX_LEGACY_SNAPSHOT_BYTES:
                return jsonify({"status": "error", "message": "Legacy snapshot exceeds the allowed size."}), 413
            if snapshot:
                safe_ref = re.sub(r"[^A-Za-z0-9._-]+", "-", client_ref)[:100]
                snapshot_path = f"incidents/{cam_id}/legacy/{safe_ref}/snapshot.jpg"
                storage.upload_bytes(
                    snapshot_path,
                    snapshot,
                    "image/jpeg",
                    upsert=True,
                )

        if snapshot_path:
            db.set_incident_media(
                incident_id,
                snapshot_path=snapshot_path,
            )

        return jsonify({
            "status": "ok",
            "id": incident_id,
            "created_at": created_at,
        }), 200

    except Exception:
        log.exception("Could not ingest incident %s", client_ref)
        return jsonify({"status": "error", "message": "Could not save incident."}), 503


# ---------------------------------------------------------------------------
# Local pool computer -> cloud: video metadata (video bytes bypass Vercel)
# ---------------------------------------------------------------------------

@bp.post("/api/ingest/incident/<int:cloud_id>/video")
def ingest_incident_video(cloud_id: int):
    """Attach a video already uploaded directly to private Supabase Storage."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}

    auth_error = _authorize(payload.get("site"))
    if auth_error:
        return auth_error

    storage_path = payload.get("storage_path")
    mime = str(payload.get("mime") or "video/mp4").strip().lower()
    if mime != "video/mp4":
        return jsonify({"status": "error", "message": "Only video/mp4 is supported."}), 415

    try:
        incident = db.get_incident(cloud_id)
        if not incident:
            return jsonify({"status": "error", "message": "Incident not found."}), 404

        cam_id = str(incident.get("cam_id") or "")
        if not _valid_camera(cam_id):
            return jsonify({"status": "error", "message": "Incident has an invalid camera ID."}), 400

        if not _valid_media_path(storage_path, cam_id, ".mp4"):
            return jsonify({"status": "error", "message": "Invalid video storage path."}), 400

        db.attach_video([cloud_id], storage_path, mime)
        return jsonify({
            "status": "ok",
            "id": cloud_id,
            "storage_path": storage_path,
        }), 200

    except Exception:
        log.exception("Could not attach video to incident %s", cloud_id)
        return jsonify({"status": "error", "message": "Could not attach incident video."}), 503


# ---------------------------------------------------------------------------
# Local pool computer -> cloud: resolve event
# ---------------------------------------------------------------------------

@bp.post("/api/ingest/incident/<int:cloud_id>/resolve")
def ingest_incident_resolve(cloud_id: int):
    """Mark a cloud incident as resolved after the local worker resolves it."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}

    auth_error = _authorize(payload.get("site"))
    if auth_error:
        return auth_error

    try:
        resolved = db.resolve_incident(
            cloud_id,
            resolved_at=payload.get("resolved_at"),
        )
        if not resolved:
            return jsonify({"status": "error", "message": "Incident not found."}), 404
        return jsonify({"status": "ok", "id": cloud_id}), 200

    except Exception:
        log.exception("Could not resolve cloud incident %s", cloud_id)
        return jsonify({"status": "error", "message": "Could not resolve incident."}), 503