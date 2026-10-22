"""Endpoints for pool computer and ESP32 telemetry/frame uploads.

Every authenticated call to /api/ingest/... must carry the secret key 
(header "Authorization: Bearer <key>") that is set as the LIFEGUARD_INGEST_KEY 
environment variable on the cloud server.
"""
import hmac
import json
import logging
import os
import re
import tempfile

from flask import Blueprint, jsonify, request

from . import db, footage, storage
from . import settings as cfg
from .auth import client_ip, fail
from .cloud import RemoteCamera
from .registry import workers

log = logging.getLogger("lifeguard.ingest")
bp = Blueprint("ingest", __name__)

MAX_VIDEO_BYTES = 80 * 1024 * 1024
MAX_FRAME_BYTES = 400 * 1024
MAX_SNAPSHOT_BYTES = 3 * 1024 * 1024
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

# In-memory frame storage imported by live.py for dynamic feeds
LATEST_FRAMES = {}


@bp.post("/api/occupancy")
def receive_occupancy():
    """Unauthenticated endpoint to receive telemetry logs from ESP32 / test scripts."""
    data = request.get_json(silent=True) or {}
    cam_id = data.get("camera_id", "cam1")
    
    in_pool = data.get("people_in_pool", 0)
    poolside = data.get("people_poolside", 0)
    submerged = data.get("submerged_count", 0)

    try:
        with db.connect() as c:
            c.execute(
                "INSERT INTO occupancy_logs (camera_id, people_in_pool, people_poolside, submerged_count, timestamp) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (cam_id, in_pool, poolside, submerged)
            )
    except Exception as e:
        log.warning("Failed to store occupancy log: %s", e)

    return jsonify({"success": True})


@bp.post("/api/ingest/frame/<cam_id>")
def frame_upload(cam_id):
    """Receive raw binary JPEG frame buffers and cache them in LATEST_FRAMES."""
    data = request.get_data()
    if data.startswith(b"\xff\xd8") and len(data) <= MAX_FRAME_BYTES:
        LATEST_FRAMES[cam_id] = data
        return "", 204
    return fail("Not a valid JPEG frame.", 400)


@bp.before_request
def authenticate():
    """Enforce authentication key only for protected pool-computer endpoints."""
    # Allow unauthenticated telemetry and direct frame uploads to bypass key enforcement
    if request.path in ("/api/occupancy",) or request.path.startswith("/api/ingest/frame/"):
        return None

    key = os.environ.get("LIFEGUARD_INGEST_KEY", "")
    if len(key) < 16:
        return fail("Uploads from the pool computer are not enabled on this server.", 503)
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    if not hmac.compare_digest(token.encode(), key.encode()):
        log.warning("rejected an upload with a wrong key from %s", client_ip())
        return fail("Invalid key.", 401)
    try:
        request.max_content_length = MAX_VIDEO_BYTES
    except AttributeError:
        pass


def _remote(cam_id):
    camera = workers.get(cam_id)
    return camera if isinstance(camera, RemoteCamera) else None


@bp.post("/api/ingest/heartbeat")
def heartbeat():
    data = request.get_json(silent=True)
    reports = data.get("cameras") if isinstance(data, dict) and isinstance(data.get("cameras"), dict) else {}
    for cam_id, camera in workers.items():
        report = reports.get(cam_id)
        if isinstance(camera, RemoteCamera) and isinstance(report, dict):
            alerts = report.get("alerts") if isinstance(report.get("alerts"), list) else []
            camera.update(report, alerts)
    return jsonify({"ok": True, "frames": [c for c, cam in workers.items()
                                           if isinstance(cam, RemoteCamera) and cam.wants_frames()]})


@bp.post("/api/ingest/incident")
def incident():
    try:
        meta = json.loads(request.form.get("meta", ""))
        cam_id, event = meta["cam_id"], str(meta.get("event") or "Drowning Risk Detected")[:100]
        track_id, elapsed = int(meta["track_id"]), int(meta["elapsed_sec"])
        created_at, ref = str(meta["created_at"]), str(meta["client_ref"])
    except (ValueError, KeyError, TypeError):
        return fail("Invalid incident data.", 400)
    if cam_id not in cfg.CAMERAS or not TIMESTAMP_RE.match(created_at) or not 1 <= len(ref) <= 120:
        return fail("Invalid incident data.", 400)
    snapshot = request.files.get("snapshot")
    jpeg = snapshot.read(MAX_SNAPSHOT_BYTES + 1) if snapshot else None
    if jpeg is not None and (len(jpeg) > MAX_SNAPSHOT_BYTES or not jpeg.startswith(b"\xff\xd8")):
        return fail("Invalid snapshot.", 400)
    
    if cfg.is_cloud():
        incident_id, _ = db.create_incident(cam_id, track_id, elapsed, event, 
                                            created_at=created_at, client_ref=ref)
        if jpeg:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                tmp.write(jpeg)
                tmp_path = tmp.name
                
            try:
                dest_name = f"incidents/{cam_id}/{incident_id}.jpg"
                cloud_path = storage.upload_incident_snapshot(tmp_path, dest_name)
                if cloud_path:
                    db.set_incident_media(incident_id, snapshot_path=cloud_path)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
    else:
        incident_id = footage.record_incident(cam_id, track_id, elapsed, jpeg, event,
                                              created_at=created_at, client_ref=ref)
    
    return jsonify({"id": incident_id})


@bp.post("/api/ingest/incident/<int:incident_id>/video")
def video(incident_id):
    row = db.get_incident(incident_id)
    if not row:
        return fail("Unknown incident.", 404)
    data = request.get_data()
    if not data:
        return fail("Empty video.", 400)
    
    if cfg.is_cloud():
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            tmp.write(data)
            tmp_path = tmp.name
            
        try:
            dest_name = f"incidents/{row['cam_id']}/{incident_id}.mp4"
            cloud_path = storage.upload_incident_video(tmp_path, dest_name)
            if cloud_path:
                db.set_incident_media(incident_id, video_path=cloud_path, video_mime="video/mp4")
            else:
                return fail("Cloud storage upload failed.", 500)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    else:
        footage.store_video([incident_id], row["cam_id"], data, "video/mp4")
        
    return jsonify({"success": True})


@bp.post("/api/ingest/incident/<int:incident_id>/resolve")
def resolve(incident_id):
    if not db.get_incident(incident_id):
        return fail("Unknown incident.", 404)
    body = request.get_json(silent=True)
    when = body.get("resolved_at") if isinstance(body, dict) else None
    db.resolve_incident(incident_id, when if isinstance(when, str) and TIMESTAMP_RE.match(when) else None)
    return jsonify({"success": True})